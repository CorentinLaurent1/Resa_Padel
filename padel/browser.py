"""Navigateur piloté par Playwright : détection de Chrome, capture de jetons, formulaires."""
import os, re, shutil, subprocess, sys, threading, time
from .config import API_HOST_MATCH, CDP_PORT, CLOUD, DOIN_HOST_MATCH, LETPADEL_PAGE_MATCH, PROFILE_DIR, SMASH_HOST_MATCH
from .providers.smash import TOKEN_RE
from .tokens import is_4padel_token, is_doinsport_token


JWT_RE = re.compile(r"eyJ[\w-]+\.eyJ[\w-]+\.[\w-]+")


LS_SCAN = "() => Object.keys(localStorage).map(k => localStorage.getItem(k) || '').join('\n') + '\n' + document.cookie"


FIND_FORM_JS = r"""() => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const pw = [...document.querySelectorAll('input[type=password]')].find(vis);
  if (!pw) return null;
  const cands = [...document.querySelectorAll('input')].filter(el => vis(el) &&
     ['email','text','tel',''].includes((el.getAttribute('type')||'').toLowerCase()) &&
     (pw.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_PRECEDING));
  const scope = pw.form ? cands.filter(el => el.form === pw.form) : cands;
  const pick = (scope.length ? scope : cands).pop();
  if (!pick) return null;
  pw.setAttribute('data-padel', 'pw'); pick.setAttribute('data-padel', 'email');
  return true;
}"""


REVEAL_JS = r"""() => {
  const re = /(e-?mail|adresse|mot de passe|identifi|se connecter|connexion|j'ai déjà un compte|déjà membre|log ?in|sign ?in|mon compte|mon profil)/i;
  const bad = /(google|apple|facebook|inscri|créer|newsletter|abonne)/i;
  const els = [...document.querySelectorAll('button, a, [role=button]')]
    .filter(el => el.offsetParent !== null && re.test(el.textContent || '') && !bad.test(el.textContent || ''));
  if (els.length) { els[0].click(); return (els[0].textContent || '').trim().slice(0, 40); }
  return null;
}"""


COOKIE_JS = r"""() => {
  const re = /(tout accepter|accepter|accept all|j'accepte|ok)/i;
  const b = [...document.querySelectorAll('button, a')].find(el => el.offsetParent !== null && re.test((el.textContent||'').trim()));
  if (b) b.click();
}"""


def find_chrome():
    cands = []
    if sys.platform == "darwin":
        cands = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                 "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"]
    elif os.name == "nt":
        for base in (os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                     os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                     os.environ.get("LOCALAPPDATA", "")):
            cands += [os.path.join(base, r"Google\Chrome\Application\chrome.exe"),
                      os.path.join(base, r"Microsoft\Edge\Application\msedge.exe"),
                      os.path.join(base, r"BraveSoftware\Brave-Browser\Application\brave.exe")]
    else:
        for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "brave-browser"):
            p = shutil.which(n)
            if p:
                cands.append(p)
    return next((c for c in cands if c and os.path.exists(c)), None)   # None => Playwright utilise son Chromium


def attach_capture(ctx, provider, found):
    check = is_4padel_token if provider == "4padel" else is_doinsport_token

    def keep(value):
        if value and "token" not in found and check(value):
            found["token"] = value.strip()
    found["_keep"] = keep

    def on_request(req):
        try:
            if provider == "4padel" and API_HOST_MATCH in req.url:
                try:
                    auth = req.all_headers().get("authorization", "")
                except Exception:
                    auth = req.headers.get("authorization", "")
                keep(auth)
            elif provider == "letpadel" and DOIN_HOST_MATCH in req.url:
                try:
                    auth = req.all_headers().get("authorization", "")
                except Exception:
                    auth = req.headers.get("authorization", "")
                if auth.lower().startswith("bearer "):
                    keep(auth)
            elif provider == "smash" and SMASH_HOST_MATCH in req.url and "token" not in found:
                m = TOKEN_RE.search(req.post_data or "")
                if m:
                    found["token"] = m.group(1)
        except Exception:
            pass

    def on_response(resp):
        if provider == "letpadel" and DOIN_HOST_MATCH in resp.url and re.search(r"login|token|auth", resp.url, re.I):
            try:
                if resp.ok:
                    j = resp.json()
                    keep(j.get("token") or j.get("access_token"))
                elif resp.status in (400, 401, 403):
                    found["refused"] = True
            except Exception:
                pass
            return
        if provider == "4padel" and API_HOST_MATCH in resp.url and "/login" in resp.url:
            try:
                if resp.ok:
                    keep(resp.json().get("access_token"))
                elif resp.status in (400, 401, 403):
                    found["refused"] = True
            except Exception:
                pass

    ctx.on("request", on_request)
    ctx.on("response", on_response)


def scan_session(ctx, provider, found):
    for pg in [p for p in ctx.pages if not p.is_closed()]:
        try:
            if provider == "4padel" and pg.url.startswith("https://www.4padel.fr"):
                for m in JWT_RE.findall(pg.evaluate(LS_SCAN) or ""):
                    found["_keep"](m)
            elif provider == "letpadel" and LETPADEL_PAGE_MATCH in pg.url:
                for m in JWT_RE.findall(pg.evaluate(LS_SCAN) or ""):
                    found["_keep"](m)
            elif provider == "smash" and SMASH_HOST_MATCH in pg.url:
                html = pg.content()
                if "logout.asp" in html:
                    found["logged"] = True
                    m = TOKEN_RE.search(html)
                    if m and "token" not in found:
                        found["token"] = m.group(1)
        except Exception:
            pass
    if provider in ("4padel", "letpadel"):
        return "token" in found
    return "token" in found and found.get("logged")


def visible_browser(pw, url):
    """Se connecte au Chrome dédié s'il tourne déjà, sinon le lance."""
    try:
        return pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}"), False
    except Exception:
        pass
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError("Chrome, Edge ou Brave introuvable sur cet ordinateur.")
    subprocess.Popen([chrome, f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={PROFILE_DIR}",
                      "--no-first-run", "--no-default-browser-check", url],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        try:
            return pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}"), True
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("Impossible d’ouvrir Chrome.")


def fill_login(pg, email, password):
    for _ in range(4):
        if pg.evaluate(FIND_FORM_JS):
            pg.fill('[data-padel="email"]', email)
            pg.fill('[data-padel="pw"]', password)
            pg.press('[data-padel="pw"]', "Enter")
            return True
        pg.evaluate(REVEAL_JS)
        pg.wait_for_timeout(1200)
    return False


VISIBLE_LOCK = threading.Lock()


BROWSERS = threading.BoundedSemaphore(int(os.environ.get("PADEL_BROWSERS", "2")))


def browser_slot(timeout=45):
    """Réserve un des rares navigateurs, avec attente bornée (pas de blocage infini)."""
    if not BROWSERS.acquire(timeout=timeout):
        raise RuntimeError("Le service est très sollicité en ce moment. Réessaie dans une minute.")
    try:
        yield
    finally:
        BROWSERS.release()


def launch_browser(pw, chrome):
    """Lance un navigateur headless : d'abord le Chrome du système, sinon le Chromium de Playwright."""
    last = None
    for kwargs in ([{"executable_path": chrome}] if chrome else []) + [{}]:
        try:
            return pw.chromium.launch(headless=True, **kwargs)
        except Exception as e:
            last = e
    hint = chromium_hint()
    raise RuntimeError("Le navigateur n’a pas pu démarrer. " + hint + (f" Détail : {last}" if last else ""))


def chromium_hint():
    """Message adapté : où Playwright cherche Chromium et pourquoi il ne le trouve pas."""
    path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    import importlib.util
    installed = importlib.util.find_spec("playwright") is not None
    if not installed:
        return "Le module playwright n’est pas installé : « pip install playwright »."
    if CLOUD and not path:
        return ("Sur un serveur, définis la variable PLAYWRIGHT_BROWSERS_PATH (par ex. "
                "/opt/render/project/src/ms-playwright) au build ET à l’exécution, puis "
                "« playwright install --with-deps chromium » avec la même variable.")
    return "Installe le navigateur : « playwright install --with-deps chromium »."
