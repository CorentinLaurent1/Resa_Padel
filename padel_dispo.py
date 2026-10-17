#!/usr/bin/env python3
"""
Padel Orléans : créneaux libres en direct, servis en local.

Lancement :  python3 padel_dispo.py
Puis la page s'ouvre sur http://127.0.0.1:8765

Python 3.8+ et le module playwright (py -m pip install playwright).[cite: 1]
Rien d'autre à télécharger : le script utilise ton Chrome ou Edge.[cite: 1]
Tes identifiants sont dans identifiants.dat, chiffré par Windows (DPAPI).[cite: 1]
La clé de session 4PADEL reste dans le fichier .4padel_token à côté du script.[cite: 1]
"""
import base64, datetime, json, os, re, shutil, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request, webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", 8765))
HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, ".4padel_token")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")

# --- Configuration des clubs (relevée dans tes captures HAR) -----------------
DOINSPORT_PADEL_ACTIVITY = "ce8c306e-224a-4f24-aa9d-6500580924dc"   # « Padel », vu à Fleury
LETPADEL_CLUBS = {
    "letpadel-pryve": "6a99f5f6-32bd-4fe3-ba15-8d22b1f58c1f",
    "letpadel-fleury": "fc96f79e-ef94-4fa6-9b01-d494eccaa54f",
}
SMASH_BASE = "https://smashpadel.mymobileapp.fr"
SMASH_SPORT_PADEL = "2"
SMASH_FILE = os.path.join(HERE, ".smash_session")
FOURPADEL_CENTER_ID = 9        # « LE FIVE Orléans Fleury »
FOURPADEL_SPORT_PADEL = 3


class NeedsToken(Exception):
    pass


def http_json(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json, text/plain, */*")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def euros(value):
    if value is None:
        return None
    s = f"{value:.2f}".replace(".", ",")
    return (s[:-3] if s.endswith(",00") else s) + " €/joueur"


# --- Let Padel (Doinsport) : public, pas besoin de compte ---------------------
DOIN_HEADERS = {"Origin": "https://letpadel.doinsport.club",
                "Referer": "https://letpadel.doinsport.club/", "x-locale": "fr"}
_ACTIVITY_CACHE = {}


def padel_activity(club_id):
    """Chaque centre peut avoir son propre identifiant d'activité : on cherche « Padel »."""
    if club_id not in _ACTIVITY_CACHE:
        found = DOINSPORT_PADEL_ACTIVITY
        try:
            j = http_json("https://api-v3.doinsport.club/activities?" + urllib.parse.urlencode({"club.id": club_id}),
                          headers=DOIN_HEADERS)
            for a in j.get("hydra:member", []):
                if a.get("name", "").strip().lower() == "padel":
                    found = a["@id"].rsplit("/", 1)[-1]
                    break
        except Exception:
            pass
        _ACTIVITY_CACHE[club_id] = found
    return _ACTIVITY_CACHE[club_id]


def letpadel_slots(club_id, date, duration):
    activity = padel_activity(club_id)
    q = urllib.parse.urlencode({
        "club.id": club_id, "from": "08:00", "to": "23:59:00",
        "activities.id": activity, "bookingType": "unique",
    })
    j = http_json(f"https://api-v3.doinsport.club/clubs/playgrounds/plannings/{date}?{q}", headers=DOIN_HEADERS)
    return parse_doinsport(j, duration, activity)


def parse_doinsport(j, duration, activity=DOINSPORT_PADEL_ACTIVITY):
    agg = {}
    for pg in j.get("hydra:member", []):
        for act in pg.get("activities", []):
            if act.get("id") != activity:
                continue
            for s in act.get("slots", []):
                for p in s.get("prices", []):
                    if p.get("duration") == duration * 60 and p.get("bookable"):
                        a = agg.setdefault(s["startAt"], {"time": s["startAt"], "courts": 0,
                                                          "_p": p.get("pricePerParticipant")})
                        a["courts"] += 1
    out = []
    for a in sorted(agg.values(), key=lambda x: x["time"]):
        out.append({"time": a["time"], "courts": a["courts"],
                    "price": euros(a["_p"] / 100) if a["_p"] is not None else None})
    return out


# --- 4PADEL (API LE FIVE) : nécessite ta clé de session ------------------------
def read_token():
    env = os.environ.get("FOURPADEL_TOKEN")
    if env:
        return env.strip()
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def jwt_payload(token):
    payload = token.split(" ")[-1].split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def is_4padel_token(token):
    """Une clé 4PADEL contient « upn » et « roleId » (vu dans ta capture).[cite: 1]"""
    try:
        p = jwt_payload(token)
        return "upn" in p and "roleId" in p
    except Exception:
        return False


def save_token(token):
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(token)
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass


def token_expiry(token):
    try:
        payload = token.split(" ")[-1].split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return datetime.datetime.fromtimestamp(exp) if exp else None
    except Exception:
        return None


def zulu(d, hour, minute=0):
    local = datetime.datetime(d.year, d.month, d.day, hour, minute).astimezone()
    return local.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def fourpadel_slots(date, duration):
    token = read_token()
    if not token:
        raise NeedsToken("Connecte ton compte 4PADEL pour voir ses créneaux.")
    exp = token_expiry(token)
    if exp and exp < datetime.datetime.now():
        raise NeedsToken(f"Ta connexion 4PADEL a expiré le {exp:%d/%m}. Reconnecte-toi.")
    d = datetime.date.fromisoformat(date)
    auth = token if " " in token else f"Bearer {token}"
    headers = {"Authorization": auth, "Origin": "https://www.4padel.fr",
               "Referer": "https://www.4padel.fr/"}
    found = []
    for start in range(8, 23, 2):
        end = min(start + 2, 23)
        body = {"startingDateZuluTime": zulu(d, start), "endingDateZuluTime": zulu(d, end),
                "durations": "60,90,120", "capacity": 4, "center_id": FOURPADEL_CENTER_ID,
                "bookingType_id": "1", "sportType_id": FOURPADEL_SPORT_PADEL, "isChannelWeb": True,
                "computePriceWithDefaultCapaIfNoCapa": True}
        try:
            found += http_json("https://api2-front.lefive.fr/bookingrules/allFields?appId=2&isChannelWeb=true",
                               "POST", body, headers)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise NeedsToken("4PADEL a refusé la clé : reconnecte-toi.")
            raise
    return parse_fourpadel(found, duration)


def parse_fourpadel(items, duration):
    seen = {}
    for s in items:
        if s.get("duration") != duration:
            continue
        fields = [f for f in s.get("fields", []) if f.get("canBookOnline", True)]
        if not fields:
            continue
        t = s["startingDate"][11:16]   # heure locale du club
        price = fields[0].get("participationWebPrice")
        prev = seen.get(t)
        if not prev or len(fields) > prev["courts"]:
            seen[t] = {"time": t, "courts": len(fields), "price": euros(price) if price else None}
    return [seen[k] for k in sorted(seen)]


# --- Smash Padel (Live Xperience) : page HTML, nécessite d'être connecté -------
SLOT_RE = re.compile(r"""date-countDispo="(\d+)"\s+onclick="choosePop\('\d\d/\d\d/\d{4} (\d\d:\d\d):\d\d','[^']*','([^']*)'""")
TOKEN_RE = re.compile(r"livexperience_site_token=([\w-]+)")


def read_smash():
    try:
        with open(SMASH_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def save_smash(data):
    with open(SMASH_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)
    try:
        os.chmod(SMASH_FILE, 0o600)
    except OSError:
        pass


def smash_slots(date, duration):
    sess = read_smash()
    if not sess or not sess.get("token"):
        raise NeedsToken("Connecte ton compte Smash Padel pour voir ses créneaux.")
    d = datetime.date.fromisoformat(date)
    body = urllib.parse.urlencode({
        "myDate": d.strftime("%d/%m/%Y"), "duree": str(duration), "id_sport": SMASH_SPORT_PADEL,
        "livexperience_site_token": sess["token"], "dd": str(int(time.time() * 1000)),
    }).encode()
    req = urllib.request.Request(f"{SMASH_BASE}/loadcalendrier_capsule.asp", data=body, method="POST")
    for k, v in {"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                 "X-Requested-With": "XMLHttpRequest", "Origin": SMASH_BASE, "Referer": SMASH_BASE + "/",
                 "Cookie": sess.get("cookies", "")}.items():
        if v:
            req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=20) as r:
        html = r.read().decode("utf-8", "replace")
    return parse_smash(html)


def parse_smash(html):
    if "btn-horaires" not in html:
        if "logout.asp" not in html and ("login" in html.lower() or "connexion" in html.lower()):
            raise NeedsToken("Ta session Smash Padel a expiré. Reconnecte-toi.")
        return []
    out = []
    for count, t, prices in SLOT_RE.findall(html):
        vals = [float(p.replace(",", ".")) for p in prices.split(";")[1:] if p and p != "999"]
        out.append({"time": t, "courts": int(count),
                    "price": euros(min(vals) / 4) if vals else None})
    return sorted(out, key=lambda x: x["time"])


# --- Identifiants protégés ---------------------------------------------------
CREDS_FILE = os.path.join(HERE, "identifiants.dat")


def _dpapi(data, protect):
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    inp = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, None, None, None, 0x1, ctypes.byref(out)):
        raise OSError("Windows n’a pas pu " + ("chiffrer" if protect else "déchiffrer") + " les identifiants.")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def load_creds():
    try:
        with open(CREDS_FILE, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return {}
    kind, _, payload = raw.partition(b"\n")
    data = base64.b64decode(payload)
    if kind == b"DPAPI1":
        data = _dpapi(data, False)
    return json.loads(data.decode("utf-8"))


def save_creds(creds):
    data = json.dumps(creds).encode("utf-8")
    if os.name == "nt":
        blob = b"DPAPI1\n" + base64.b64encode(_dpapi(data, True))
    else:
        blob = b"PLAIN1\n" + base64.b64encode(data)
    tmp = CREDS_FILE + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, CREDS_FILE)


# --- Connexion aux clubs -----------------------------------------------------
SIGNIN_URL = "https://www.4padel.fr/signin?redirect=%2Freservations%2Fslots"
API_HOST_MATCH = "lefive.fr"
SMASH_HOST_MATCH = "smashpadel.mymobileapp.fr"
PROFILE_DIR = os.path.join(HERE, ".chrome-4padel")
CDP_PORT = 9223
LOGIN = {"state": "idle", "message": ""}
LOGIN_LOCK = threading.Lock()
BROWSER_LOCK = threading.Lock()
JWT_RE = re.compile(r"eyJ[\w-]+\.eyJ[\w-]+\.[\w-]+")
LS_SCAN = "() => Object.keys(localStorage).map(k => localStorage.getItem(k) || '').join('\n') + '\n' + document.cookie"

PROVIDERS = {
    "4padel": {"url": SIGNIN_URL, "name": "4PADEL",
               "hint": "Dans la fenêtre Chrome, connecte-toi à 4PADEL."},
    "smash": {"url": SMASH_BASE + "/", "name": "Smash Padel",
              "hint": "Dans la fenêtre Chrome, connecte-toi à Smash Padel puis ouvre « Nouvelle réservation »."},
}

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
  const re = /(e-?mail|adresse|mot de passe|identifi|se connecter|connexion|j'ai déjà un compte|déjà membre|log ?in|sign ?in)/i;
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
    return next((c for c in cands if c and os.path.exists(c)), None)


def set_login(state, message):
    LOGIN.update(state=state, message=message)


def _attach_capture(ctx, provider, found):
    def keep(value):
        if value and "token" not in found and is_4padel_token(value):
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
            elif provider == "smash" and SMASH_HOST_MATCH in req.url and "token" not in found:
                m = TOKEN_RE.search(req.post_data or "")
                if m:
                    found["token"] = m.group(1)
        except Exception:
            pass

    def on_response(resp):
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


def _scan(ctx, provider, found):
    for pg in [p for p in ctx.pages if not p.is_closed()]:
        try:
            if provider == "4padel" and pg.url.startswith("https://www.4padel.fr"):
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
    if provider == "4padel":
        return "token" in found
    return "token" in found and found.get("logged")


def _persist(ctx, provider, found):
    if provider == "4padel":
        save_token(found["token"])
        exp = token_expiry(found["token"])
        return "Connecté à 4PADEL" + (f" jusqu’au {exp:%d/%m}." if exp else ".")
    cookies = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies(SMASH_BASE + "/"))
    save_smash({"token": found["token"], "cookies": cookies})
    return "Connecté à Smash Padel."


def auto_login(provider, email, password, report=lambda m: None):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Il manque un module : lance « py -m pip install playwright » puis relance.")[cite: 1]
    chrome = find_chrome()
    with BROWSER_LOCK, sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=chrome, headless=True) if chrome else pw.chromium.launch(headless=True)
        except Exception:
            raise RuntimeError("Chrome ou Edge introuvable. Installe Chrome, ou lance « py -m playwright install chromium ».")
        try:
            ctx = browser.new_context(locale="fr-FR", user_agent=UA, viewport={"width": 1280, "height": 900})
            found = {}
            _attach_capture(ctx, provider, found)
            pg = ctx.new_page()
            report("Ouverture de la page de connexion…")
            pg.goto(PROVIDERS[provider]["url"], wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(1500)
            if _scan(ctx, provider, found) and provider == "smash":
                return _persist(ctx, provider, found)
            try:
                pg.evaluate(COOKIE_JS)
            except Exception:
                pass
            ok = False
            for _ in range(4):
                try:
                    ok = pg.evaluate(FIND_FORM_JS)
                except Exception:
                    ok = False
                if ok:
                    break
                try:
                    pg.evaluate(REVEAL_JS)
                except Exception:
                    pass
                pg.wait_for_timeout(1500)
            if not ok:
                raise RuntimeError("Formulaire de connexion introuvable sur le site du club.")
            report("Envoi de l’e-mail et du mot de passe…")
            pg.fill('[data-padel="email"]', email)
            pg.fill('[data-padel="pw"]', password)
            pg.press('[data-padel="pw"]', "Enter")
            deadline = time.time() + 40
            while time.time() < deadline:
                pg.wait_for_timeout(1000)
                if found.get("refused"):
                    break
                if _scan(ctx, provider, found):
                    return _persist(ctx, provider, found)
                if provider == "smash" and found.get("logged") and "token" not in found:
                    try:
                        pg.evaluate(r"""() => { const a = [...document.querySelectorAll('a,button')].find(e => /r[ée]serv/i.test(e.textContent||'')); if (a) a.click(); }""")
                    except Exception:
                        pass
            raise RuntimeError("Connexion refusée : vérifie l’e-mail et le mot de passe.")
        finally:
            browser.close()


def interactive_login(provider):
    cfg = PROVIDERS[provider]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return set_login("error", "Il manque un module : lance « py -m pip install playwright » puis relance.")[cite: 1]
    chrome = find_chrome()
    if not chrome:
        return set_login("error", "Chrome, Edge ou Brave introuvable sur cet ordinateur.")
    proc = subprocess.Popen([chrome, f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={PROFILE_DIR}",
                             "--no-first-run", "--no-default-browser-check", "--new-window", cfg["url"]],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with BROWSER_LOCK, sync_playwright() as pw:
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
            _attach_capture(ctx, provider, found)
            set_login("waiting", cfg["hint"])
            deadline = time.time() + 300
            while time.time() < deadline:
                pages = [p for p in ctx.pages if not p.is_closed()]
                if not pages:
                    raise RuntimeError("La fenêtre Chrome a été fermée avant la connexion.")
                if _scan(ctx, provider, found):
                    set_login("done", _persist(ctx, provider, found))
                    break
                pages[0].wait_for_timeout(800)
            else:
                raise RuntimeError("Pas de connexion détectée en 5 minutes.")
            try:
                browser.close()
            except Exception:
                pass
    except Exception as e:
        set_login("error", str(e) or type(e).__name__)
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


def credentials_worker(provider):
    try:
        c = load_creds().get(provider) or {}
        msg = auto_login(provider, c["email"], c["password"], lambda m: set_login("waiting", m))
        set_login("done", msg)
    except Exception as e:
        set_login("error", str(e) or type(e).__name__)


def start_login(provider, mode):
    with LOGIN_LOCK:
        if LOGIN["state"] == "waiting":
            return False
        set_login("waiting", "Connexion en cours…")
        target = credentials_worker if mode == "credentials" else interactive_login
        threading.Thread(target=target, args=(provider,), daemon=True).start()
        return True


def relogin_if_possible(provider):
    try:
        c = load_creds().get(provider)
    except Exception:
        return False
    if not c:
        return False
    try:
        auto_login(provider, c["email"], c["password"])
        return True
    except Exception:
        return False


# --- Agrégation --------------------------------------------------------------
def run(fn, *args, provider=None):
    try:
        return {"status": "ok", "slots": fn(*args)}
    except NeedsToken as e:
        if provider and relogin_if_possible(provider):
            try:
                return {"status": "ok", "slots": fn(*args)}
            except Exception:
                pass
        return {"status": "needs_token", "message": str(e)}
    except urllib.error.HTTPError as e:
        return {"status": "error", "message": f"Le club a répondu {e.code}."}
    except Exception as e:
        return {"status": "error", "message": f"Pas de réponse ({type(e).__name__})."}


def all_slots(date, duration):
    jobs = {"4padel": (fourpadel_slots, date, duration), "smash": (smash_slots, date, duration)}
    for key, cid in LETPADEL_CLUBS.items():
        if cid:
            jobs[key] = (letpadel_slots, cid, date, duration)
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {k: ex.submit(run, *v, provider=k if k in PROVIDERS else None) for k, v in jobs.items()}
        clubs = {k: f.result() for k, f in futs.items()}
    return {"date": date, "duration": duration, "clubs": clubs}


# --- Serveur local -----------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/":
            # Forçage fiable du mode LIVE pour l'affichage des clubs et des créneaux
            html = PAGE.replace("const LIVE = window.PADEL_LIVE === true;", "const LIVE = true;")
            return self.send(200, html.encode(), "text/html; charset=utf-8")
        if u.path == "/api/slots":
            q = urllib.parse.parse_qs(u.query)
            try:
                date = datetime.date.fromisoformat(q["date"][0]).isoformat()
                duration = int(q.get("duration", ["90"])[0])
                assert duration in (60, 90, 120)
            except Exception:
                return self.send(400, {"error": "Paramètres invalides."})
            return self.send(200, all_slots(date, duration))
        if u.path == "/api/login/status":
            return self.send(200, LOGIN)
        if u.path == "/api/accounts":
            try:
                c = load_creds()
            except Exception:
                c = {}
            return self.send(200, {k: {"email": v.get("email", "")} for k, v in c.items()})
        self.send(404, {"error": "Introuvable."})

    def do_POST(self):
        if self.headers.get("Origin", f"http://127.0.0.1:{PORT}") not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self.send(403, {"error": "Origine refusée."})
        if self.path.startswith("/api/login/start"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            provider = q.get("p", ["4padel"])[0]
            if provider not in PROVIDERS:
                return self.send(400, {"error": "Club inconnu."})
            start_login(provider, q.get("mode", ["window"])[0])
            return self.send(200, LOGIN)
        if self.path == "/api/credentials":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                j = json.loads(self.rfile.read(n))
                provider, email, password = j["provider"], j["email"].strip(), j["password"]
                assert provider in PROVIDERS and "@" in email and password and len(email) < 200 and len(password) < 200
            except Exception:
                return self.send(400, {"error": "E-mail ou mot de passe manquant."})
            try:
                creds = load_creds()
                creds[provider] = {"email": email, "password": password}
                save_creds(creds)
            except Exception as e:
                return self.send(500, {"error": str(e)})
            if not start_login(provider, "credentials"):
                return self.send(409, {"error": "Une connexion est déjà en cours, réessaie dans un instant."})
            return self.send(200, LOGIN)
        if self.path != "/api/token":
            return self.send(404, {"error": "Introuvable."})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            token = json.loads(self.rfile.read(n))["token"].strip()
            assert token.count(".") == 2 and len(token) < 4000
        except Exception:
            return self.send(400, {"error": "Clé invalide."})
        if not is_4padel_token(token):
            return self.send(400, {"error": "Cette clé ne vient pas de 4PADEL."})
        save_token(token)
        exp = token_expiry(token)
        self.send(200, {"ok": True, "expires": exp.isoformat() if exp else None})


def main():
    url = f"http://127.0.0.1:{PORT}"
    # Écouter sur 0.0.0.0 est obligatoire pour que Render détecte le port ouvert
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Padel Orléans tourne sur le port {PORT}  (Ctrl+C pour arrêter)")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêté.")


PAGE = r'''<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Padel à Orléans</title>
<style>
:root{
  --bg:#F2F2F7; --card:#FFFFFF; --ink:#000000; --muted:#8E8E93; --hair:#C6C6C8;
  --fill:#E5E5EA; --seg:#FFFFFF; --accent:#007AFF; --accent-ink:#FFFFFF;
  --green:#34C759; --green-bg:#E8F8ED; --grey-bg:#F1F1F3; --shadow:0 4px 20px rgba(0,0,0,0.06);
  color-scheme:light; box-sizing:border-box;
  padding-top:env(safe-area-inset-top,0px); padding-bottom:env(safe-area-inset-bottom,0px);
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#000000; --card:#1C1C1E; --ink:#F2F2F7; --muted:#98989D; --hair:#38383A;
  --fill:#2C2C2E; --seg:#636366; --accent:#0A84FF; --accent-ink:#FFFFFF;
  --green:#30D158; --green-bg:#0F2E18; --grey-bg:#2C2C2E; --shadow:0 4px 20px rgba(0,0,0,0.3); color-scheme:dark;}}
:root[data-theme="dark"]{
  --bg:#000000; --card:#1C1C1E; --ink:#F2F2F7; --muted:#98989D; --hair:#38383A;
  --fill:#2C2C2E; --seg:#636366; --accent:#0A84FF; --accent-ink:#FFFFFF;
  --green:#30D158; --green-bg:#0F2E18; --grey-bg:#2C2C2E; --shadow:0 4px 20px rgba(0,0,0,0.3); color-scheme:dark;}

*,*::before,*::after{box-sizing:inherit}
html{scroll-padding-top:env(safe-area-inset-top,0px)}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue","Segoe UI",Roboto,Arial,sans-serif;
  font-size:16px;line-height:1.5;letter-spacing:-.01em;-webkit-font-smoothing:antialiased}

.wrap{max-width:720px;margin:0 auto;padding:32px 20px 64px}
header{margin-bottom:28px}
h1{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Helvetica Neue","Segoe UI",sans-serif;
  font-size:clamp(2rem,6vw,2.8rem);font-weight:700;letter-spacing:-.03em;line-height:1.1;margin:0}
.lede{color:var(--muted);font-size:1.05rem;margin:8px 0 0}

.controls{background:var(--card);border-radius:20px;padding:20px;box-shadow:var(--shadow);margin-bottom:24px;display:grid;gap:18px}
.label{font-size:.8rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:0 0 8px 2px}

.seg{display:flex;background:var(--fill);border-radius:12px;padding:3px;gap:3px;overflow-x:auto;scrollbar-width:none}
.seg::-webkit-scrollbar{display:none}
.seg button{flex:1 0 auto;min-width:60px;border:0;background:transparent;color:var(--ink);font:inherit;font-size:.85rem;
  font-weight:500;padding:8px 10px;border-radius:9px;cursor:pointer;line-height:1.2;transition:all .2s ease}
.seg button small{display:block;font-size:.7rem;color:var(--muted);font-weight:400;margin-top:2px}
.seg button[aria-pressed="true"]{background:var(--seg);color:var(--ink);box-shadow:0 2px 8px rgba(0,0,0,.1),0 0 1px rgba(0,0,0,.15);font-weight:600}

.time-box{display:flex;flex-direction:column;gap:8px}
.time-head{display:flex;justify-content:space-between;align-items:center}
.time-val{font-size:1rem;font-weight:600}
.link-btn{border:0;background:none;color:var(--accent);font:inherit;font-size:.9rem;font-weight:500;cursor:pointer;padding:0}
.link-btn:hover{text-decoration:underline}

input[type=range]{width:100%;accent-color:var(--accent);height:6px;border-radius:3px;background:var(--fill);outline:none;cursor:pointer}

.summary{font-weight:500;color:var(--muted);font-size:.95rem;margin:0 4px 16px;padding-left:2px}

.cards{display:grid;gap:16px;list-style:none;margin:0;padding:0}
.card{background:var(--card);border-radius:20px;padding:22px;box-shadow:var(--shadow);transition:transform .2s ease, opacity .2s ease}
.card.dim{opacity:.65}
.head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.card h3{font-size:1.15rem;font-weight:600;letter-spacing:-.02em;margin:0}
.sub{color:var(--muted);font-size:.85rem;margin:3px 0 0}

.badge{flex:0 0 auto;font-size:.75rem;font-weight:600;padding:5px 12px;border-radius:999px;white-space:nowrap;letter-spacing:-.01em}
.badge.on{background:var(--green-bg);color:var(--green)}
.badge.off{background:var(--grey-bg);color:var(--muted)}

.slots{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0 6px}
.slot{display:inline-flex;flex-direction:column;align-items:center;text-decoration:none;color:var(--ink);
  background:var(--fill);border-radius:12px;padding:8px 12px;min-width:72px;font-size:.95rem;font-weight:600;transition:transform .15s, background .15s}
.slot small{font-size:.68rem;font-weight:400;color:var(--muted);margin-top:2px}
.slot:hover{background:var(--accent);color:var(--accent-ink);transform:translateY(-2px)}
.slot:hover small{color:rgba(255,255,255,.8)}

.msg{color:var(--muted);font-size:.9rem;margin:14px 0 4px}

.links{display:flex;align-items:center;gap:12px;margin-top:18px;padding-top:16px;border-top:1px solid var(--hair);flex-wrap:wrap}
.links a,.links button{white-space:nowrap;color:var(--muted);text-decoration:none;font:inherit;font-size:.88rem;font-weight:500;background:none;border:0;padding:6px 12px;border-radius:8px;cursor:pointer;transition:background .15s, color .15s}
.links a:hover,.links button:hover{background:var(--fill);color:var(--ink)}
.links .primary{background:var(--accent);color:var(--accent-ink);border-radius:999px;padding:8px 20px;font-weight:600;margin-right:auto}
.links .primary:hover{opacity:.9;background:var(--accent);color:var(--accent-ink)}

.foot{color:var(--muted);font-size:.8rem;margin:32px 4px 0;text-align:center;line-height:1.4}

a:focus-visible,button:focus-visible,input:focus-visible{outline:3px solid var(--accent);outline-offset:2px;border-radius:8px}

dialog{border:0;border-radius:24px;padding:0;max-width:460px;width:calc(100% - 32px);background:var(--card);color:var(--ink);box-shadow:0 20px 40px rgba(0,0,0,0.2)}
dialog::backdrop{background:rgba(0,0,0,.4);backdrop-filter:blur(8px)}
.sheet{padding:28px}
.sheet h2{font-size:1.3rem;font-weight:600;margin:0 0 6px;letter-spacing:-.02em}
.sheet textarea{width:100%;min-height:90px;border:1px solid var(--hair);border-radius:12px;background:var(--bg);color:var(--ink);
  font:13px ui-monospace,Menlo,Consolas,monospace;padding:12px;resize:vertical}
.sheet .row{display:flex;gap:10px;justify-content:flex-end;margin-top:16px}
.btn{border:0;border-radius:999px;padding:10px 20px;font:inherit;font-size:.9rem;font-weight:600;cursor:pointer;transition:opacity .15s}
.btn:hover{opacity:.9}
.btn.fill{background:var(--accent);color:var(--accent-ink)}
.btn.plain{background:var(--fill);color:var(--ink)}
.field{display:block;margin-top:16px}
.field span{display:block;font-size:.8rem;font-weight:600;color:var(--muted);margin:0 0 6px 4px;text-transform:uppercase;letter-spacing:.05em}
.field input{width:100%;font:inherit;font-size:1rem;color:var(--ink);background:var(--bg);border:1px solid var(--hair);border-radius:12px;padding:12px 14px;outline:none;transition:border-color .15s}
.field input:focus{border-color:var(--accent)}
.btn.google{display:block;width:100%;margin:20px 0 0;padding:14px;font-weight:600;background:var(--ink);color:var(--bg);border-radius:14px}
.btn.google:disabled{opacity:.4;cursor:default}
.status{font-size:.88rem;color:var(--muted);min-height:1.4em;margin:12px 2px 0;font-weight:500}
.status.ok{color:var(--green)} .status.bad{color:#FF3B30}
.manual{margin-top:16px;border-top:1px solid var(--hair);padding-top:14px;font-size:.9rem}
.manual summary{cursor:pointer;color:var(--accent);font-weight:500}
.err{color:#FF3B30;font-size:.85rem;min-height:1.2em;margin:8px 0 0}
@media (prefers-reduced-motion: reduce){*{transition:none!important}}
</style>
</head>
<body>
<main class="wrap">
  <header>
    <h1>Padel à Orléans</h1>
    <p class="lede" id="lede">Quatre clubs, un seul endroit pour trouver ton terrain.</p>
  </header>

  <div class="controls">
    <div><p class="label">Jour</p><div class="seg" id="days" role="group" aria-label="Jour"></div></div>
    <div id="durBox" hidden><p class="label">Durée</p>
      <div class="seg" id="durs" role="group" aria-label="Durée">
        <button type="button" data-d="60" aria-pressed="false">1 h</button>
        <button type="button" data-d="90" aria-pressed="true">1 h 30</button>
        <button type="button" data-d="120" aria-pressed="false">2 h</button>
      </div></div>
    <div class="time-box">
      <div class="time-head"><span class="label" style="margin:0">Heure de début</span><span class="time-val" id="timeLbl">À 19h00</span><button class="link-btn" id="nowBtn" type="button">Maintenant</button></div>
      <input type="range" id="time" min="8" max="23.5" step="0.5" value="19" aria-label="Heure">
    </div>
  </div>

  <p class="summary" id="summary" aria-live="polite"></p>
  <ul class="cards" id="cards"></ul>
  <p class="foot" id="foot"></p>
</main>

<dialog id="sheet">
  <form class="sheet" id="credForm" autocomplete="on">
    <h2 id="sheetTitle">Se connecter</h2>
    <p class="sub" id="sheetText"></p>
    <label class="field"><span>E-mail</span><input type="email" id="cEmail" autocomplete="username" required></label>
    <label class="field"><span>Mot de passe</span><input type="password" id="cPass" autocomplete="current-password" required></label>
    <button class="btn google" id="gBtn" type="submit">Se connecter</button>
    <p class="status" id="gMsg" aria-live="polite"></p>
    <details class="manual" id="manual">
      <summary>Autres façons de se connecter</summary>
      <p class="sub" style="margin-top:8px">Si ton compte n’a pas de mot de passe (créé avec Google), connecte-toi toi-même dans une fenêtre Chrome :</p>
      <button class="btn plain" id="winBtn" type="button" style="margin-top:10px">Ouvrir une fenêtre Chrome</button>
      <div id="keyBox">
        <p class="sub" style="margin-top:14px">Ou colle la valeur d’<code>authorization</code> copiée dans les outils du navigateur (F12, Réseau) :</p>
        <textarea id="tok" placeholder="eyJraWQiOi…" spellcheck="false" autocomplete="off"></textarea>
        <p class="err" id="tokErr"></p>
        <div class="row"><button class="btn plain" id="tokSave" type="button">Enregistrer la clé</button></div>
      </div>
    </details>
    <div class="row" style="margin-top:20px"><button class="btn plain" id="sheetClose" type="button">Fermer</button></div>
  </form>
</dialog>

<script>
const LIVE = window.PADEL_LIVE === true;
const H = (wk, we) => ({wk, we});
const CLUBS = [
  {id:"4padel", name:"4PADEL Orléans", city:"Fleury-les-Aubrais", courts:"6 pistes indoor",
   address:"113 rue de Curembourg, 45400 Fleury-les-Aubrais", phone:"02 38 58 99 37",
   hours:H([9,23.5],[9,21.5]), book:"https://www.4padel.fr/reservations/slots"},
  {id:"letpadel-pryve", name:"Let Padel Saint-Pryvé", city:"Saint-Pryvé-Saint-Mesmin", courts:"4 pistes",
   address:"10 rue des Moines, 45750 Saint-Pryvé-Saint-Mesmin", phone:"02 18 69 31 69",
   hours:H([10,24],[9,21]), book:"https://letpadel.doinsport.club/home"},
  {id:"letpadel-fleury", name:"Let Padel Fleury", city:"Fleury-les-Aubrais", courts:"4 pistes",
   address:"20 rue André Dessaux, 45400 Fleury-les-Aubrais", phone:"02 18 69 32 22",
   hours:H([10,24],[9,21]), book:"https://letpadel.doinsport.club/home"},
  {id:"smash", name:"Smash Padel", city:"Saint-Denis-en-Val", courts:"5 pistes + 2 singles",
   address:"2951 route de Sandillon, 45560 Saint-Denis-en-Val", phone:"02 19 23 11 08",
   hours:H([9,23],[9,23]), book:"https://smashpadel.mymobileapp.fr/"}
];
const DAYS = ["dim.","lun.","mar.","mer.","jeu.","ven.","sam."];
const S = {offset:0, t:19, dur:90, data:null, loading:false, err:null};
const $ = id => document.getElementById(id);
const fmt = t => `${Math.floor(t)%24}h${String(Math.round((t%1)*60)).padStart(2,"0")}`;
const dateFor = o => {const d=new Date(); d.setHours(12,0,0,0); d.setDate(d.getDate()+o); return d;};
const iso = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
const toH = s => {const [h,m]=s.split(":").map(Number); return h+m/60;};
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function press(box, i){ box.querySelectorAll("button").forEach((b,j)=>b.setAttribute("aria-pressed", j===i?"true":"false")); }

function buildDays(){
  const box=$("days");
  for(let i=0;i<7;i++){
    const d=dateFor(i), b=document.createElement("button");
    b.type="button";
    b.innerHTML=`${i===0?"Auj.":i===1?"Demain":DAYS[d.getDay()]}<small>${d.getDate()}/${d.getMonth()+1}</small>`;
    b.setAttribute("aria-pressed", i===0?"true":"false");
    b.onclick=()=>{S.offset=i; press(box,i); load();};
    box.appendChild(b);
  }
}

async function load(){
  if(!LIVE){ render(); return; }
  S.loading=true; S.err=null; render();
  try{
    const r=await fetch(`/api/slots?date=${iso(dateFor(S.offset))}&duration=${S.dur}`);
    if(!r.ok) throw new Error(`Le serveur local a répondu ${r.status}.`);
    S.data=await r.json();
  }catch(e){ S.data=null; S.err=e.message||"Le serveur local ne répond pas."; }
  S.loading=false; render();
}

function bookingURL(c, date, time, duration){
  const u = new URL(c.book);
  if(c.id === "4padel"){
    u.searchParams.set("center", "9");
    u.searchParams.set("date", date);
    u.searchParams.set("time", time);
    u.searchParams.set("duration", String(duration));
  } else if(c.id === "letpadel-pryve" || c.id === "letpadel-fleury"){
    u.searchParams.set("clubId", c.id === "letpadel-pryve" ? "6a99f5f6-32bd-4fe3-ba15-8d22b1f58c1f" : "fc96f79e-ef94-4fa6-9b01-d494eccaa54f");
    u.searchParams.set("date", date);
    u.searchParams.set("time", time);
    u.searchParams.set("duration", String(duration));
  } else if(c.id === "smash"){
    u.searchParams.set("myDate", date.split("-").reverse().join("/"));
    u.searchParams.set("heure", time);
    u.searchParams.set("duree", String(duration));
    u.searchParams.set("id_sport", "2");
  }
  return u.toString();
}

function linksHTML(c){
  const map=`https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(c.name+" "+c.address)}`;
  const booking=bookingURL(c, iso(dateFor(S.offset)), fmt(S.t).replace("h",":"), S.dur);
  return `<div class="links">
    <a class="primary" href="${booking}" target="_blank" rel="noopener">Réserver</a>
    <a href="${map}" target="_blank" rel="noopener">Itinéraire</a>
    <a href="tel:${c.phone.replace(/\s/g,"")}">Appeler</a></div>`;
}

function render(){
  const d=dateFor(S.offset), we=d.getDay()===0||d.getDay()===6;
  const now=new Date(), nowH=now.getHours()+now.getMinutes()/60;
  $("timeLbl").textContent = LIVE ? `À partir de ${fmt(S.t)}` : `À ${fmt(S.t)}`;
  const dayTxt = S.offset===0?"aujourd’hui":S.offset===1?"demain":`${DAYS[d.getDay()]} ${d.getDate()}/${d.getMonth()+1}`;

  let rows = CLUBS.map(c=>{
    const [o,cl]=we?c.hours.we:c.hours.wk;
    const r={c,o,cl,open:S.t>=o&&S.t<cl,slots:null,state:null,msg:null};
    if(LIVE && S.data){
      const x=(S.data.clubs||{})[c.id]||{status:"not_configured"};
      r.state=x.status; r.msg=x.message||null;
      if(x.status==="ok") r.slots=(x.slots||[]).filter(s=>toH(s.time)>=S.t && !(S.offset===0 && toH(s.time)<nowH));
    }
    return r;
  });

  if(LIVE && S.data){
    rows.sort((a,b)=>(b.slots?b.slots.length:-1)-(a.slots?a.slots.length:-1));
    const total=rows.reduce((n,r)=>n+(r.slots?r.slots.length:0),0);
    const clubsWith=rows.filter(r=>r.slots&&r.slots.length).length;
    $("summary").textContent = total
      ? `${total} créneau${total>1?"x":""} libre${total>1?"s":""} ${dayTxt} dans ${clubsWith} club${clubsWith>1?"s":""}.`
      : `Aucun créneau libre ${dayTxt} à partir de ${fmt(S.t)} dans les clubs connectés.`;
  } else if(LIVE){
    $("summary").textContent = S.loading ? "Recherche des créneaux en cours…" : (S.err||"");
  } else {
    rows.sort((a,b)=>b.open-a.open);
    const n=rows.filter(r=>r.open).length;
    $("summary").textContent = n ? `${n} club${n>1?"s":""} sur 4 ouvert${n>1?"s":""} ${dayTxt} à ${fmt(S.t)}.`
                                 : `Aucun club ouvert ${dayTxt} à ${fmt(S.t)}.`;
  }

  $("cards").innerHTML = rows.map(r=>{
    const c=r.c; let badge="", body="", dim=false;
    if(LIVE && S.data){
      if(r.state==="ok"){
        const n=r.slots.length;
        badge = n ? `<span class="badge on">${n} libre${n>1?"s":""}</span>` : `<span class="badge off">Complet</span>`;
        dim = !n;
        body = n ? `<div class="slots">${r.slots.map(s=>{const url=bookingURL(c, iso(dateFor(S.offset)), s.time, S.dur); return `<a class="slot" href="${url}" target="_blank" rel="noopener" title="Réserver le ${iso(dateFor(S.offset))} à ${s.time} — ${s.courts} piste${s.courts>1?"s":""}">${esc(s.time.replace(":","h"))}<small>${s.price?esc(s.price):s.courts+" piste"+(s.courts>1?"s":"")}</small></a>`;}).join("")}</div>` : "";
      } else if(r.state==="needs_token"){
        badge=`<span class="badge off">Non connecté</span>`;
        body=`<p class="msg">${esc(r.msg||"Connecte ton compte pour voir les créneaux.")}</p>`;
      } else if(r.state==="error"){
        badge=`<span class="badge off">Indisponible</span>`;
        body=`<p class="msg">${esc(r.msg||"Le club n’a pas répondu.")}</p>`;
      } else {
        badge=`<span class="badge off">Lien seulement</span>`;
        body=`<p class="msg">Créneaux pas encore branchés pour ce club : ouvre sa page de réservation.</p>`;
      }
    } else if(LIVE){
      badge=`<span class="badge off">Chargement…</span>`;
    } else {
      badge = r.open ? `<span class="badge on">Ouvert jusqu’à ${fmt(r.cl)}</span>`
                     : `<span class="badge off">${S.t<r.o?`Ouvre à ${fmt(r.o)}`:"Fermé"}</span>`;
      dim=!r.open;
    }
    const extra = (LIVE && (c.id==="4padel"||c.id==="smash")) ? `<button type="button" data-act="token" data-p="${c.id}">${r.state==="needs_token"?"Se connecter":"Compte"}</button>` : "";
    return `<li class="card${dim?" dim":""}">
      <div class="head"><div><h3>${c.name}</h3><p class="sub">${c.city}, ${c.courts}</p></div>${badge}</div>
      ${body}${linksHTML(c).replace("</div>", extra+"</div>")}</li>`;
  }).join("");
  document.querySelectorAll('[data-act="token"]').forEach(b=>b.onclick=()=>openSheet(b.dataset.p));
}

const SHEETS={
  "4padel":{title:"Se connecter à 4PADEL", key:true,
    text:"Ton e-mail et ton mot de passe 4PADEL sont chiffrés sur ce PC. La connexion se refait toute seule quand elle expire."},
  "smash":{title:"Se connecter à Smash Padel", key:false,
    text:"Ton e-mail et ton mot de passe Smash Padel sont chiffrés sur ce PC. La connexion se refait toute seule quand elle expire."}};
let PROVIDER="4padel";
function setG(msg, cls){ const m=$("gMsg"); m.textContent=msg; m.className="status"+(cls?" "+cls:""); }
async function openSheet(p){
  PROVIDER=p||"4padel"; const c=SHEETS[PROVIDER];
  $("sheetTitle").textContent=c.title; $("sheetText").textContent=c.text;
  $("keyBox").hidden=!c.key; $("manual").open=false;
  $("cPass").value=""; $("tok").value=""; $("tokErr").textContent=""; setG("",""); $("gBtn").disabled=false; $("winBtn").disabled=false;
  $("sheet").showModal();
  try{ const a=await (await fetch("/api/accounts")).json(); $("cEmail").value=(a[PROVIDER]||{}).email||""; }catch(e){}
  ($("cEmail").value?$("cPass"):$("cEmail")).focus();
}
async function follow(){
  for(;;){
    await new Promise(r=>setTimeout(r,1000));
    const st=await (await fetch("/api/login/status")).json();
    if(st.state==="done"){ setG(st.message,"ok"); setTimeout(()=>{ $("sheet").close(); load(); },900); return; }
    if(st.state==="error"){ setG(st.message,"bad"); $("gBtn").disabled=false; $("winBtn").disabled=false; return; }
    setG(st.message);
  }
}
$("sheetClose").onclick=()=>$("sheet").close();
$("credForm").onsubmit = async (e)=>{
  e.preventDefault();
  $("gBtn").disabled=true; setG("Connexion en cours…");
  try{
    const r=await fetch("/api/credentials",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({provider:PROVIDER,email:$("cEmail").value,password:$("cPass").value})});
    $("cPass").value="";
    if(!r.ok){ const j=await r.json(); setG(j.error||"Échec.","bad"); $("gBtn").disabled=false; return; }
    await follow();
  }catch(err){ setG("Le script local ne répond plus. Relance-le.","bad"); $("gBtn").disabled=false; }
};
$("winBtn").onclick = async ()=>{
  $("winBtn").disabled=true; setG("Ouverture de Chrome…");
  try{ await fetch(`/api/login/start?p=${PROVIDER}&mode=window`,{method:"POST"}); await follow(); }
  catch(err){ setG("Le script local ne répond plus. Relance-le.","bad"); $("winBtn").disabled=false; }
};
$("tokSave").onclick = async ()=>{
  const v=$("tok").value.trim().replace(/^Bearer\s+/i,"");
  if(v.split(".").length!==3){ $("tokErr").textContent="Ce n’est pas une clé valide : elle doit commencer par « eyJ » et contenir deux points."; return; }
  try{
    const r=await fetch("/api/token",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:v})});
    const j=await r.json();
    if(!r.ok) throw new Error(j.error||"Enregistrement impossible.");
    $("sheet").close(); load();
  }catch(e){ $("tokErr").textContent=e.message; }
};

$("time").addEventListener("input",e=>{S.t=parseFloat(e.target.value); render();});
$("nowBtn").onclick=()=>{
  const n=new Date(); let t=n.getHours()+(n.getMinutes()>=30?.5:0);
  S.t=Math.min(23.5,Math.max(8,t)); $("time").value=S.t;
  if(S.offset!==0){S.offset=0; press($("days"),0); load();} else render();
};
document.querySelectorAll("#durs button").forEach((b,i)=>b.onclick=()=>{S.dur=+b.dataset.d; press($("durs"),i); load();});

if(LIVE){
  $("durBox").hidden=false;
  $("lede").textContent="Créneaux libres en direct dans les clubs d’Orléans.";
  $("foot").textContent="Données lues à l’instant sur les sites des clubs. Clique sur un horaire pour ouvrir la réservation du club avec la date, l’heure et la durée transmises quand la plateforme le permet.";
} else {
  $("foot").textContent="Horaires indicatifs relevés en octobre 2026. Les créneaux libres s’affichent sur la page de réservation de chaque club.";
}
buildDays();
$("nowBtn").click();
if(LIVE) load(); else render();
</script>
</body>
</html>
'''

if __name__ == "__main__":
    main()