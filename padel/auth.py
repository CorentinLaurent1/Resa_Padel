"""Connexion aux comptes des clubs : appels directs, navigateur, reconnexion silencieuse."""
import collections, subprocess, threading, time
from .browser import COOKIE_JS, FIND_FORM_JS, REVEAL_JS, VISIBLE_LOCK, attach_capture, browser_slot, find_chrome, launch_browser, scan_session
from .config import CDP_PORT, PROFILE_DIR, PROVIDERS, SMASH_BASE, UA
from .providers.doinsport import letpadel_login
from .providers.fourpadel import fourpadel_login
from .providers.smash import smash_login
from .tokens import token_expiry


LOGINS = {}                                   # état de connexion par visiteur


_ATTEMPTS = collections.defaultdict(collections.deque)


def rate_ok(key, limit=8, window=600):
    """Limite les tentatives de connexion (par visiteur et par adresse IP)."""
    q = _ATTEMPTS[key]
    now = time.time()
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


def set_login(sid, state, message):
    LOGINS[sid] = {"state": state, "message": message}


def _extract(ctx, provider, found):
    """Ce qu'on garde de la connexion : jamais le mot de passe, seulement la session du club."""
    if provider == "smash":
        cookies = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies(SMASH_BASE + "/"))
        return {"token": found["token"], "cookies": cookies}
    return found["token"]


def _done_message(provider, value):
    name = PROVIDERS[provider]["name"]
    exp = token_expiry(value) if isinstance(value, str) else None
    return f"Connecté à {name}" + (f" jusqu’au {exp:%d/%m}." if exp else ".")


def auto_login(provider, email, password, report=lambda m: None):
    """Connexion avec e-mail et mot de passe. Renvoie la session du club.
    Les trois clubs se connectent par appel direct : aucun navigateur nécessaire."""
    report(f"Connexion à {PROVIDERS[provider]['name']}…")
    if provider == "letpadel":
        return letpadel_login(email, password)
    if provider == "4padel":
        return fourpadel_login(email, password)
    if provider == "smash":
        return smash_login(email, password)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Le module playwright n’est pas installé sur ce serveur.")
    chrome = find_chrome()                      # un vrai Chrome du système, s'il y en a un
    report("En attente d’un navigateur libre…")
    with browser_slot(), sync_playwright() as pw:
        browser = launch_browser(pw, chrome)
        try:
            ctx = browser.new_context(locale="fr-FR", user_agent=UA, viewport={"width": 1280, "height": 900})
            found = {}
            attach_capture(ctx, provider, found)
            pg = ctx.new_page()
            report("Ouverture de la page de connexion…")
            pg.goto(PROVIDERS[provider]["url"], wait_until="domcontentloaded", timeout=45000)
            try:
                pg.evaluate(COOKIE_JS)
            except Exception:
                pass
            # On attend que le formulaire apparaisse, en vérifiant souvent et en
            # cliquant « Se connecter » au besoin, plutôt que des pauses fixes.
            ok = False
            form_deadline = time.time() + 20
            next_reveal = 0
            while time.time() < form_deadline:
                try:
                    ok = pg.evaluate(FIND_FORM_JS)
                except Exception:
                    ok = False
                if ok:
                    break
                if time.time() >= next_reveal:
                    try:
                        pg.evaluate(REVEAL_JS)
                    except Exception:
                        pass
                    next_reveal = time.time() + 1.2
                pg.wait_for_timeout(150)
            if not ok:
                raise RuntimeError("Formulaire de connexion introuvable sur le site du club.")
            report("Envoi de l’e-mail et du mot de passe…")
            pg.fill('[data-padel="email"]', email)
            pg.fill('[data-padel="pw"]', password)
            pg.press('[data-padel="pw"]', "Enter")
            report("Vérification de tes identifiants…")
            deadline = time.time() + 40
            while time.time() < deadline:
                pg.wait_for_timeout(250)
                if found.get("refused"):
                    break
                if scan_session(ctx, provider, found):
                    return _extract(ctx, provider, found)
                if provider == "smash" and found.get("logged") and "token" not in found:
                    try:
                        pg.evaluate(r"""() => { const a = [...document.querySelectorAll('a,button')].find(e => /r[ée]serv/i.test(e.textContent||'')); if (a) a.click(); }""")
                    except Exception:
                        pass
            raise RuntimeError("Connexion refusée : vérifie l’e-mail et le mot de passe.")
        finally:
            browser.close()


def store_login(sess, provider, value, email, password, remember):
    def fn(d):
        d[provider] = value
        d.setdefault("emails", {})[provider] = email
        creds = d.setdefault("creds", {})
        if remember and password:
            creds[provider] = {"email": email, "password": password}
        else:
            creds.pop(provider, None)
    sess.update(fn)


def credentials_worker(sess, provider, email, password, remember):
    try:
        value = auto_login(provider, email, password, lambda m: set_login(sess.id, "waiting", m))
        store_login(sess, provider, value, email, password, remember)
        set_login(sess.id, "done", _done_message(provider, value))
    except Exception as e:
        set_login(sess.id, "error", str(e) or type(e).__name__)


def interactive_login(sess, provider):
    """Sur ton PC uniquement : fenêtre Chrome visible, tu te connectes toi-même (Google…)."""
    cfg = PROVIDERS[provider]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return set_login(sess.id, "error", "Il manque un module : lance « py -m pip install playwright » puis relance.")
    chrome = find_chrome()
    if not chrome:
        return set_login(sess.id, "error", "Chrome, Edge ou Brave introuvable sur cet ordinateur.")
    proc = subprocess.Popen([chrome, f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={PROFILE_DIR}",
                             "--no-first-run", "--no-default-browser-check", "--new-window", cfg["url"]],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with VISIBLE_LOCK, sync_playwright() as pw:
            browser = None
            for _ in range(40):
                try:
                    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
                    break
                except Exception:
                    time.sleep(0.5)
            if not browser:
                raise RuntimeError("Impossible de piloter Chrome. Ferme toutes ses fenêtres et réessaie.")
            ctx = browser.contexts[0]
            found = {}
            attach_capture(ctx, provider, found)
            set_login(sess.id, "waiting", cfg["hint"])
            deadline = time.time() + 300
            while time.time() < deadline:
                pages = [p for p in ctx.pages if not p.is_closed()]
                if not pages:
                    raise RuntimeError("La fenêtre Chrome a été fermée avant la connexion.")
                if scan_session(ctx, provider, found):
                    value = _extract(ctx, provider, found)
                    sess.update(lambda d: d.__setitem__(provider, value))
                    set_login(sess.id, "done", _done_message(provider, value))
                    break
                pages[0].wait_for_timeout(800)
            else:
                raise RuntimeError("Pas de connexion détectée en 5 minutes.")
            try:
                browser.close()
            except Exception:
                pass
    except Exception as e:
        set_login(sess.id, "error", str(e) or type(e).__name__)
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


def start_login(sess, provider, target, *args):
    cur = LOGINS.get(sess.id, {})
    if cur.get("state") == "waiting":
        return False
    set_login(sess.id, "waiting", "Connexion en cours…")
    threading.Thread(target=target, args=(sess, provider) + args, daemon=True).start()
    return True


def relogin_if_possible(sess, data, provider):
    """Session expirée : reconnexion silencieuse si le visiteur a choisi « Rester connecté »."""
    c = (data.get("creds") or {}).get(provider)
    if not c:
        return None
    try:
        value = auto_login(provider, c["email"], c["password"])
    except Exception:
        return None
    store_login(sess, provider, value, c["email"], c["password"], True)
    return sess.load()
