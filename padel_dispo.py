#!/usr/bin/env python3
"""
Padel Orléans : créneaux libres en direct, servis en local.

Sur ton PC :  py padel_dispo.py   (ou double-clic sur lancer_padel.bat)
Puis la page s'ouvre sur http://127.0.0.1:8765

Sur un hébergeur (Render…) : la variable PORT est lue automatiquement.
Définis aussi PADEL_ACCESS (mot de passe d'accès à la page), sinon
n'importe qui ayant l'adresse peut l'ouvrir. Identifiants des clubs
possibles par variables : PADEL_4PADEL_EMAIL / PADEL_4PADEL_PASSWORD,
PADEL_SMASH_EMAIL / PADEL_SMASH_PASSWORD.

Python 3.8+ et le module playwright (py -m pip install playwright).
Rien d'autre à télécharger : le script utilise ton Chrome ou Edge.
Tes identifiants sont dans identifiants.dat, chiffré par Windows (DPAPI).
La clé de session 4PADEL reste dans le fichier .4padel_token à côté du script.
"""
import base64, datetime, json, os, re, shutil, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request, webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", 8765))
CLOUD = "PORT" in os.environ                      # lancé par un hébergeur (Render…)
HOST = os.environ.get("HOST", "0.0.0.0" if CLOUD else "127.0.0.1")
ACCESS = os.environ.get("PADEL_ACCESS", "")       # mot de passe d'accès à la page
HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, ".4padel_token")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")

# --- Configuration des clubs (relevée dans tes captures HAR) -----------------
DOINSPORT_PADEL_ACTIVITY = "ce8c306e-224a-4f24-aa9d-6500580924dc"   # « Padel », vu à Fleury
LETPADEL_CATEGORY = {"letpadel-fleury": "be10916d-f273-4f52-ad9b-da6379dca5c9"}   # vu dans ta 1re capture
LETPADEL_CLUBS = {
    "letpadel-pryve": "6a99f5f6-32bd-4fe3-ba15-8d22b1f58c1f",
    "letpadel-fleury": "fc96f79e-ef94-4fa6-9b01-d494eccaa54f",
}
SMASH_BASE = "https://smashpadel.mymobileapp.fr"
SMASH_SPORT_PADEL = "2"
SMASH_FILE = os.path.join(HERE, ".smash_session")
LETPADEL_TOKEN_FILE = os.path.join(HERE, ".letpadel_token")
LETPADEL_HOME = "https://letpadel.doinsport.club/home"
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


def letpadel_url(key, club_id):
    url = (f"https://letpadel.doinsport.club/select-booking?guid=%22{club_id}%22&from=sport"
           f"&activitySelectedId=%22{padel_activity(club_id)}%22")
    if key in LETPADEL_CATEGORY:
        url += f"&categoryId=%22{LETPADEL_CATEGORY[key]}%22"
    return url


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
    """Une clé 4PADEL contient « upn » et « roleId » (vu dans ta capture)."""
    try:
        p = jwt_payload(token)
        return "upn" in p and "roleId" in p
    except Exception:
        return False


def is_doinsport_token(token):
    """Jeton de compte Doinsport : JWT dont le contenu désigne un utilisateur."""
    try:
        p = jwt_payload(token)
        return bool(p.get("username") or p.get("email") or p.get("roles") or p.get("user") or p.get("sub"))
    except Exception:
        return False


def read_letpadel_token():
    try:
        with open(LETPADEL_TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def save_letpadel_token(token):
    with open(LETPADEL_TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(token)
    try:
        os.chmod(LETPADEL_TOKEN_FILE, 0o600)
    except OSError:
        pass


def session_state(provider):
    """(connecté ?, date d'expiration ou None)"""
    if provider == "4padel":
        tok = read_token()
    elif provider == "letpadel":
        tok = read_letpadel_token()
    else:
        sm = read_smash()
        return bool(sm and sm.get("token")), None
    if not tok:
        return False, None
    exp = token_expiry(tok)
    return (exp is None or exp > datetime.datetime.now()), exp


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
    # Le site interroge par fenêtres de quelques heures : on fait pareil.
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
        t = s["startingDate"][11:16]   # heure locale du club, telle qu'affichée sur le site
        price = fields[0].get("participationWebPrice")
        prev = seen.get(t)
        if not prev or len(fields) > prev["courts"]:
            seen[t] = {"time": t, "courts": len(fields), "price": euros(price) if price else None}
    return [seen[k] for k in sorted(seen)]



# --- Smash Padel (Live Xperience) : page HTML, nécessite d'être connecté -------
SLOT_RE = re.compile(r"""date-countDispo="(\d+)"\s+onclick="choosePop\('(\d\d/\d\d/\d{4} (\d\d:\d\d):\d\d)','([^']*)','([^']*)'""")
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
        return []   # jour fermé ou aucune réservation possible
    out = []
    for count, stamp, t, courts, prices in SLOT_RE.findall(html):
        vals = [float(p.replace(",", ".")) for p in prices.split(";")[1:] if p and p != "999"]
        ids = [c for c in courts.split(";")[1:] if c.isdigit()]
        out.append({"time": t, "courts": int(count),
                    "price": euros(min(vals) / 4) if vals else None,   # prix affiché par terrain, 4 joueurs
                    "book": {"date": stamp, "courts": ids}})
    return sorted(out, key=lambda x: x["time"])


# --- Identifiants protégés ---------------------------------------------------
# Sous Windows, le fichier est chiffré avec DPAPI : seul ton compte Windows, sur
# ce PC, peut le déchiffrer. Ailleurs, il n'est lisible que par ton utilisateur.
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
    creds = {}
    try:
        with open(CREDS_FILE, "rb") as f:
            raw = f.read()
        kind, _, payload = raw.partition(b"\n")
        data = base64.b64decode(payload)
        if kind == b"DPAPI1":
            data = _dpapi(data, False)
        creds = json.loads(data.decode("utf-8"))
    except FileNotFoundError:
        pass
    for key, env in (("4padel", "4PADEL"), ("letpadel", "LETPADEL"), ("smash", "SMASH")):   # variables d'environnement prioritaires
        e, p = os.environ.get(f"PADEL_{env}_EMAIL"), os.environ.get(f"PADEL_{env}_PASSWORD")
        if e and p:
            creds[key] = {"email": e, "password": p}
    return creds


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
# Deux façons :
#  - automatique : un Chrome invisible remplit le vrai formulaire avec ton e-mail
#    et ton mot de passe, puis récupère la session (et recommence seul à expiration) ;
#  - manuelle : une fenêtre Chrome s'ouvre et tu te connectes toi-même (Google…).
SIGNIN_URL = "https://www.4padel.fr/signin?redirect=%2Freservations%2Fslots"
API_HOST_MATCH = "lefive.fr"
SMASH_HOST_MATCH = "smashpadel.mymobileapp.fr"
DOIN_HOST_MATCH = "doinsport.club"            # API et site Let Padel
LETPADEL_PAGE_MATCH = "letpadel.doinsport.club"
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
    "letpadel": {"url": LETPADEL_HOME, "name": "Let Padel",
                 "hint": "Dans la fenêtre Chrome, connecte-toi à Let Padel."},
    "smash": {"url": SMASH_BASE + "/", "name": "Smash Padel",
              "hint": "Dans la fenêtre Chrome, connecte-toi à Smash Padel puis ouvre « Nouvelle réservation »."},
}

# Repère le champ mot de passe visible et le champ e-mail juste avant lui (en
# ignorant par exemple le champ « newsletter » du pied de page).
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
    return next((c for c in cands if c and os.path.exists(c)), None)


def set_login(state, message):
    LOGIN.update(state=state, message=message)


def _attach_capture(ctx, provider, found):
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


def _scan(ctx, provider, found):
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


def _persist(ctx, provider, found):
    if provider == "letpadel":
        save_letpadel_token(found["token"])
        exp = token_expiry(found["token"])
        return "Connecté à Let Padel" + (f" jusqu’au {exp:%d/%m}." if exp else ".")
    if provider == "4padel":
        save_token(found["token"])
        exp = token_expiry(found["token"])
        return "Connecté à 4PADEL" + (f" jusqu’au {exp:%d/%m}." if exp else ".")
    cookies = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies(SMASH_BASE + "/"))
    save_smash({"token": found["token"], "cookies": cookies})
    return "Connecté à Smash Padel."


def auto_login(provider, email, password, report=lambda m: None):
    """Connexion invisible avec e-mail et mot de passe. Renvoie un message, ou lève une erreur."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Il manque un module : lance « py -m pip install playwright » puis relance.")
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
                return _persist(ctx, provider, found)      # déjà connecté
            try:
                pg.evaluate(COOKIE_JS)
            except Exception:
                pass
            # Trouver le formulaire, en cliquant au besoin sur « Se connecter / E-mail ».
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
                    # Connecté mais jeton pas encore visible : on ouvre la réservation.
                    try:
                        pg.evaluate(r"""() => { const a = [...document.querySelectorAll('a,button')].find(e => /r[ée]serv/i.test(e.textContent||'')); if (a) a.click(); }""")
                    except Exception:
                        pass
            raise RuntimeError("Connexion refusée : vérifie l’e-mail et le mot de passe.")
        finally:
            browser.close()


def interactive_login(provider):
    """Fenêtre Chrome visible : tu te connectes toi-même (Google, Apple…)."""
    cfg = PROVIDERS[provider]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return set_login("error", "Il manque un module : lance « py -m pip install playwright » puis relance.")
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
    """Appelé quand une session a expiré : reconnexion silencieuse si on a les identifiants."""
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


# --- Ouvrir la réservation d'un créneau --------------------------------------
# Smash : on ouvre Chrome (profil dédié, déjà connecté) et on fait exactement ce
# que fait le site quand tu cliques sur un terrain : choose(...) charge
# /resa-confirm.asp pour ce terrain, cette date et cette durée.
SMASH_PICK_JS = r"""([ids, stamp]) => {
  for (const id of ids) {
    const b = document.getElementById('terrain_' + id);
    if (b) { choose(id, b.getAttribute('data-nom'), stamp, '', b.getAttribute('data-type')); return b.getAttribute('data-nom'); }
  }
  return null;
}"""


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


def open_smash_booking(stamp, ids, duration):
    from playwright.sync_api import sync_playwright
    day = stamp.split(" ")[0]
    with BROWSER_LOCK, sync_playwright() as pw:
        browser, fresh = visible_browser(pw, SMASH_BASE + "/")
        ctx = browser.contexts[0]
        sess = read_smash() or {}
        cookies = []
        for part in (sess.get("cookies") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                cookies.append({"name": k, "value": v, "url": SMASH_BASE + "/"})
        if cookies:
            ctx.add_cookies(cookies)
        pg = next((p for p in ctx.pages if SMASH_HOST_MATCH in p.url), None) if fresh else None
        pg = pg or ctx.new_page()
        pg.goto(SMASH_BASE + "/", wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(1200)
        if "logout.asp" not in pg.content():
            c = (load_creds() or {}).get("smash")
            if not c or not fill_login(pg, c["email"], c["password"]):
                pg.bring_to_front()
                return "Connecte-toi dans la fenêtre Chrome, puis reclique sur l’horaire."
            pg.wait_for_timeout(3000)
            if "logout.asp" not in pg.content():
                pg.bring_to_front()
                return "Connexion à Smash refusée : vérifie ton e-mail et ton mot de passe."
        pg.evaluate("([d, du]) => myLoad('/reservation_capsule.asp?date=' + d + '&duree=' + du + '&id_sport=2')",
                    [day, str(duration)])
        pg.wait_for_selector("#terrain_" + ids[0], state="attached", timeout=20000)
        name = pg.evaluate(SMASH_PICK_JS, [ids, stamp])
        pg.bring_to_front()
        if not name:
            return "Ce créneau vient d’être pris. Choisis-en un autre."
        try:   # le site charge la confirmation une demi-seconde après le choix du terrain
            pg.wait_for_function("() => !document.getElementById('terrain_" + ids[0] + "')", timeout=10000)
        except Exception:
            return "La page de confirmation tarde à s’afficher : regarde la fenêtre Chrome."
        return f"Réservation ouverte : {name.strip()} à {stamp[11:16].replace(':', 'h')}."


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


def keep_sessions_alive():
    for provider in ("letpadel",):
        ok, _ = session_state(provider)
        if not ok and (load_creds_safe().get(provider)):
            threading.Thread(target=relogin_if_possible, args=(provider,), daemon=True).start()


def load_creds_safe():
    try:
        return load_creds()
    except Exception:
        return {}


def all_slots(date, duration):
    keep_sessions_alive()
    jobs = {"4padel": (fourpadel_slots, date, duration), "smash": (smash_slots, date, duration)}
    for key, cid in LETPADEL_CLUBS.items():
        if cid:
            jobs[key] = (letpadel_slots, cid, date, duration)
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {k: ex.submit(run, *v, provider=k if k in PROVIDERS else None) for k, v in jobs.items()}
        clubs = {k: f.result() for k, f in futs.items()}
    for key, cid in LETPADEL_CLUBS.items():
        if cid and key in clubs:
            clubs[key]["book_url"] = letpadel_url(key, cid)
    if CLOUD:   # sur un serveur, impossible d'ouvrir Chrome chez toi : lien vers le site du club
        for slot in clubs.get("smash", {}).get("slots", []) or []:
            slot.pop("book", None)
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

    def allowed(self):
        """Mot de passe d'accès (PADEL_ACCESS) demandé par le navigateur quand il est défini."""
        if not ACCESS:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                if base64.b64decode(auth[6:]).decode("utf-8").split(":", 1)[-1] == ACCESS:
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Padel Orleans", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def same_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return urllib.parse.urlparse(origin).netloc in (host, f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def do_GET(self):
        if not self.allowed():
            return
        u = urllib.parse.urlparse(self.path)
        if u.path == "/":
            # Forçage fiable du mode LIVE (créneaux en direct) sur la page servie par le script
            html = PAGE.replace("const LIVE = window.PADEL_LIVE === true;",
                                "const LIVE = true;" + (" window.PADEL_CLOUD = true;" if CLOUD else ""), 1)
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
            creds = load_creds_safe()
            out = {}
            for p in ("4padel", "letpadel", "smash"):
                ok, exp = session_state(p)
                out[p] = {"email": (creds.get(p) or {}).get("email", ""), "connected": ok,
                          "until": f"{exp:%d/%m}" if ok and exp else None, "saved": bool(creds.get(p))}
            return self.send(200, out)
        self.send(404, {"error": "Introuvable."})

    def do_POST(self):
        if not self.allowed():
            return
        if not self.same_origin():
            return self.send(403, {"error": "Origine refusée."})
        if self.path.startswith("/api/login/start"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            provider = q.get("p", ["4padel"])[0]
            if provider not in PROVIDERS:
                return self.send(400, {"error": "Club inconnu."})
            mode = q.get("mode", ["window"])[0]
            if CLOUD and mode == "window":
                return self.send(400, {"error": "Sur le serveur, utilise l’e-mail et le mot de passe."})
            start_login(provider, mode)
            return self.send(200, LOGIN)
        if self.path == "/api/open":
            if CLOUD:
                return self.send(400, {"error": "Ouverture directe possible seulement sur ton PC."})
            try:
                n = int(self.headers.get("Content-Length", "0"))
                j = json.loads(self.rfile.read(n))
                stamp, ids, duration = j["date"], [str(int(x)) for x in j["courts"]], int(j["duration"])
                assert re.fullmatch(r"\d\d/\d\d/\d{4} \d\d:\d\d:\d\d", stamp) and ids and duration in (60, 90, 120)
            except Exception:
                return self.send(400, {"error": "Créneau invalide."})
            try:
                return self.send(200, {"message": open_smash_booking(stamp, ids, duration)})
            except Exception as e:
                return self.send(500, {"error": f"Ouverture impossible ({str(e) or type(e).__name__})."})
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
            return self.send(400, {"error": "Cette clé ne vient pas de 4PADEL (c’est peut-être celle de Google)."})
        save_token(token)
        exp = token_expiry(token)
        self.send(200, {"ok": True, "expires": exp.isoformat() if exp else None})


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Padel Orléans tourne sur le port {PORT}  (Ctrl+C pour arrêter)")
    if HOST != "127.0.0.1" and not ACCESS:
        print("Attention : page accessible depuis l'extérieur sans mot de passe. Définis PADEL_ACCESS.")
    if not CLOUD:
        url = f"http://127.0.0.1:{PORT}"
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
<meta name="theme-color" content="#F2F2F7">
<title>Padel à Orléans</title>
<style>
:root{
  --bg:#F2F2F7; --card:#FFFFFF; --ink:#000000; --muted:#6C6C70; --hair:#E5E5EA; --fill:#E9E9EE; --seg:#FFFFFF;
  --blue:#007AFF; --green:#34C759; --orange:#FF9500; --red:#FF3B30;
  --accent:var(--blue); --on-accent:#FFFFFF; --tabbar:rgba(249,249,251,.86);
  color-scheme:light; box-sizing:border-box;
  padding-top:env(safe-area-inset-top,0px); padding-bottom:env(safe-area-inset-bottom,0px);
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#000000; --card:#1C1C1E; --ink:#FFFFFF; --muted:#98989F; --hair:#38383A; --fill:#2C2C2E; --seg:#636366;
  --blue:#0A84FF; --green:#30D158; --orange:#FF9F0A; --red:#FF453A; --tabbar:rgba(22,22,24,.86); color-scheme:dark;}}
:root[data-theme="dark"]{
  --bg:#000000; --card:#1C1C1E; --ink:#FFFFFF; --muted:#98989F; --hair:#38383A; --fill:#2C2C2E; --seg:#636366;
  --blue:#0A84FF; --green:#30D158; --orange:#FF9F0A; --red:#FF453A; --tabbar:rgba(22,22,24,.86); color-scheme:dark;}
:root[data-accent="green"]{--accent:var(--green)}
:root[data-accent="orange"]{--accent:var(--orange)}
*,*::before,*::after{box-sizing:inherit}
[hidden]{display:none!important}
html{scroll-padding-top:env(safe-area-inset-top,0px);height:100%}
body{margin:0;background:var(--bg);color:var(--ink);min-height:100%;
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue","Segoe UI",Roboto,Arial,sans-serif;
  font-size:17px;line-height:1.35;letter-spacing:-.015em;-webkit-font-smoothing:antialiased}
button{font:inherit;color:inherit;letter-spacing:inherit}
.screen{max-width:520px;margin:0 auto;padding:18px 16px calc(96px + env(safe-area-inset-bottom,0px))}
.kicker{margin:10px 4px 2px;font-size:.8rem;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.02em}
.titlerow{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 4px 16px}
h1{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Helvetica Neue","Segoe UI",sans-serif;
  font-size:2.15rem;font-weight:700;letter-spacing:-.035em;line-height:1.1;margin:0}
.pill{border:0;background:var(--card);color:var(--accent);font-weight:600;font-size:.95rem;padding:8px 16px;border-radius:999px;cursor:pointer}

/* Days */
.days{display:flex;gap:8px;overflow-x:auto;scrollbar-width:none;margin:0 -16px 14px;padding:2px 16px}
.days::-webkit-scrollbar{display:none}
.day{flex:0 0 auto;width:62px;border:0;background:var(--card);border-radius:16px;padding:9px 0 8px;cursor:pointer;text-align:center}
.day small{display:block;font-size:.76rem;color:var(--muted);font-weight:500}
.day b{display:block;font-size:1.35rem;font-weight:700;letter-spacing:-.02em;margin-top:2px}
.day[aria-pressed="true"]{background:var(--accent);color:var(--on-accent)}
.day[aria-pressed="true"] small{color:rgba(255,255,255,.85)}

/* Grouped block */
.group{background:var(--card);border-radius:16px;padding:4px 14px;margin-bottom:18px}
.grow{display:flex;align-items:center;gap:12px;padding:10px 0;min-width:0}
.grow + .grow{border-top:.5px solid var(--hair)}
.glabel{flex:0 0 58px;font-weight:600;font-size:.95rem}
.seg{flex:1;display:flex;background:var(--fill);border-radius:10px;padding:2px;gap:2px;min-width:0}
.seg button{flex:1;border:0;background:transparent;font-size:.92rem;font-weight:500;padding:7px 8px;border-radius:8px;cursor:pointer;white-space:nowrap}
.seg button[aria-pressed="true"]{background:var(--seg);font-weight:600;box-shadow:0 1px 3px rgba(0,0,0,.12),0 0 0 .5px rgba(0,0,0,.04)}
.hours{flex:1;display:flex;gap:6px;overflow-x:auto;scrollbar-width:none;min-width:0;padding:2px 0}
.hours::-webkit-scrollbar{display:none}
.hours button{flex:0 0 auto;border:0;background:var(--fill);border-radius:999px;padding:7px 12px;font-size:.92rem;font-weight:600;cursor:pointer}
.hours button[aria-pressed="true"]{background:var(--accent);color:var(--on-accent)}

/* Summary bar */
.bar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 4px 10px}
.summary{margin:0;color:var(--muted);font-size:.92rem;line-height:1.3}
.seg.small{flex:0 0 auto}
.seg.small button{font-size:.85rem;padding:6px 10px}

/* Banners */
.banner{display:flex;align-items:center;gap:12px;background:var(--card);border-radius:14px;padding:12px 14px;margin-bottom:10px}
.banner p{margin:0;flex:1;color:var(--muted);font-size:.92rem}
.btn-accent{border:0;background:var(--accent);color:var(--on-accent);font-weight:600;font-size:.92rem;padding:8px 15px;border-radius:999px;cursor:pointer;white-space:nowrap}

/* Time view */
.tgroup{margin-top:20px}
.tgroup h2{margin:0 4px 8px;font-size:1.45rem;font-weight:700;letter-spacing:-.03em}
.tgroup h2 span{font-size:.85rem;font-weight:400;color:var(--muted);letter-spacing:0;margin-left:4px}
.rows{background:var(--card);border-radius:14px;overflow:hidden}
.row{display:flex;align-items:center;gap:12px;width:100%;text-align:left;text-decoration:none;color:inherit;background:none;border:0;padding:12px 14px;cursor:pointer}
.row + .row{border-top:.5px solid var(--hair)}
.row:active{background:var(--fill)}
.row .who{flex:1;min-width:0}
.row strong{display:block;font-weight:600;font-size:1rem}
.row small{display:block;color:var(--muted);font-size:.84rem;margin-top:1px}
.price{color:var(--accent);font-weight:600;font-size:1rem;white-space:nowrap}
.row.busy{opacity:.5;cursor:progress}

/* Club view */
.club{background:var(--card);border-radius:16px;padding:14px 14px 12px;margin-top:12px}
.club-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.club h3{margin:0;font-size:1.08rem;font-weight:600;letter-spacing:-.02em}
.club .sub{margin:2px 0 0;color:var(--muted);font-size:.85rem}
.badge{flex:0 0 auto;font-size:.76rem;font-weight:600;padding:3px 9px;border-radius:999px;background:var(--fill);color:var(--muted);white-space:nowrap}
.badge.on{background:color-mix(in srgb,var(--green) 16%,transparent);color:var(--green)}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 2px}
.chip{display:inline-flex;flex-direction:column;align-items:center;min-width:66px;border:0;background:var(--fill);border-radius:10px;padding:7px 10px;
  font-size:.95rem;font-weight:600;color:var(--ink);text-decoration:none;cursor:pointer;line-height:1.25}
.chip small{font-size:.72rem;font-weight:500;color:var(--accent)}
.chip.busy{opacity:.5;cursor:progress}
.msg{color:var(--muted);font-size:.9rem;margin:10px 0 2px}
.links{display:flex;align-items:center;gap:16px;margin-top:12px;padding-top:10px;border-top:.5px solid var(--hair)}
.links a,.links button{color:var(--accent);text-decoration:none;font-size:.92rem;background:none;border:0;padding:0;cursor:pointer;white-space:nowrap}
.links .first{margin-right:auto;font-weight:600}
.empty{background:var(--card);border-radius:14px;padding:22px 16px;text-align:center;color:var(--muted);font-size:.95rem;margin-top:12px}
.foot{color:var(--muted);font-size:.8rem;margin:22px 4px 0;line-height:1.4}

/* Accounts */
.gtitle{margin:22px 4px 8px;font-size:.8rem;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.02em}
.acct{display:flex;align-items:center;gap:12px;padding:12px 0}
.acct + .acct{border-top:.5px solid var(--hair)}
.acct .who{flex:1;min-width:0}
.acct strong{display:block;font-weight:600;font-size:1rem}
.acct small{display:block;font-size:.84rem;color:var(--muted);margin-top:1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.acct small.ok{color:var(--green)}
.btn-ghost{border:0;background:var(--fill);color:var(--accent);font-weight:600;font-size:.88rem;padding:7px 13px;border-radius:999px;cursor:pointer;white-space:nowrap}

/* Tab bar */
.tabbar{position:fixed;left:0;right:0;bottom:0;z-index:10;display:flex;justify-content:center;gap:0;
  background:var(--tabbar);backdrop-filter:saturate(180%) blur(20px);-webkit-backdrop-filter:saturate(180%) blur(20px);
  border-top:.5px solid var(--hair);padding:6px 0 calc(6px + env(safe-area-inset-bottom,0px))}
.tab{flex:0 1 160px;border:0;background:none;display:flex;flex-direction:column;align-items:center;gap:2px;font-size:.68rem;font-weight:500;color:var(--muted);cursor:pointer;padding:4px 0}
.tab svg{width:26px;height:26px}
.tab[aria-selected="true"]{color:var(--accent)}

/* Sheet */
dialog{border:0;padding:0;background:var(--card);color:var(--ink);width:100%;max-width:520px;border-radius:18px;
  margin:auto auto 0;max-height:92vh}
@media (min-width:600px){dialog{margin:auto;width:calc(100% - 32px)}}
dialog::backdrop{background:rgba(0,0,0,.35)}
.sheet{padding:22px 18px calc(22px + env(safe-area-inset-bottom,0px))}
.sheet h2{font-size:1.3rem;margin:0 0 6px;letter-spacing:-.025em}
.sheet .sub{color:var(--muted);font-size:.9rem;margin:0}
.field{display:block;margin-top:14px}
.field span{display:block;font-size:.8rem;color:var(--muted);margin:0 0 6px 4px}
.field input{width:100%;font:inherit;font-size:1rem;color:var(--ink);background:var(--bg);border:0;border-radius:12px;padding:13px 14px}
.btn-wide{display:block;width:100%;margin-top:18px;border:0;border-radius:14px;padding:14px;font-weight:600;background:var(--accent);color:var(--on-accent);cursor:pointer}
.btn-wide:disabled{opacity:.5;cursor:default}
.status{font-size:.9rem;color:var(--muted);min-height:1.3em;margin:10px 2px 0}
.status.ok{color:var(--green)} .status.bad{color:var(--red)}
.manual{margin-top:14px;border-top:.5px solid var(--hair);padding-top:12px;font-size:.9rem}
.manual summary{cursor:pointer;color:var(--accent)}
.manual code{font-size:.85em;background:var(--fill);padding:1px 5px;border-radius:5px}
.manual textarea{width:100%;min-height:80px;border:0;border-radius:12px;background:var(--bg);color:var(--ink);font:13px ui-monospace,Menlo,Consolas,monospace;padding:10px;resize:vertical;margin-top:8px}
.rowend{display:flex;justify-content:flex-end;gap:10px;margin-top:12px}
.err{color:var(--red);font-size:.86rem;min-height:1.1em;margin:6px 0 0}

.toast{position:fixed;left:0;right:0;margin:0 auto;width:fit-content;max-width:min(92vw,440px);z-index:30;
  bottom:calc(76px + env(safe-area-inset-bottom,0px));transform:translateY(16px);opacity:0;pointer-events:none;
  background:var(--ink);color:var(--bg);padding:12px 16px;border-radius:14px;font-size:.9rem;transition:opacity .2s,transform .2s}
.toast.show{opacity:1;transform:translateY(0)}
.toast.bad{background:var(--red);color:#fff}
a:focus-visible,button:focus-visible,input:focus-visible,textarea:focus-visible,summary:focus-visible{outline:3px solid var(--accent);outline-offset:2px;border-radius:8px}
/* Accueil obligatoire */
.onb{max-width:560px;margin:0 auto;padding:28px 16px calc(40px + env(safe-area-inset-bottom,0px))}
.onb h1{font-size:2rem;margin:4px 4px 8px}
.onb .lead{color:var(--muted);margin:0 4px 22px;font-size:1rem;line-height:1.45}
.req{font-size:.72rem;font-weight:600;color:var(--muted);background:var(--fill);border-radius:6px;padding:1px 6px;margin-left:6px;vertical-align:2px}
.req.opt{background:none;border:1px solid var(--hair)}
.check{width:26px;height:26px;border-radius:50%;flex:0 0 auto;display:grid;place-items:center;background:var(--fill);color:transparent}
.check.on{background:var(--green);color:#fff}
.btn-wide.dim{background:var(--fill);color:var(--muted)}
.skel{height:64px;border-radius:14px;background:linear-gradient(90deg,var(--card) 0%,var(--fill) 50%,var(--card) 100%);background-size:200% 100%;animation:sk 1.2s linear infinite;margin-top:12px}
@keyframes sk{to{background-position:-200% 0}}
.refresh{border:0;background:none;color:var(--accent);font-size:.85rem;font-weight:600;cursor:pointer;padding:0;margin-left:6px}
.brand{display:none}

@media (min-width: 640px) and (max-width: 859px){
  .screen{max-width:720px;padding-left:24px;padding-right:24px}
  .days{margin:0 0 14px;padding:2px 0;display:grid;grid-template-columns:repeat(7,1fr)}
  .day{width:auto}
}
/* ---------- Grand écran : filtres à gauche, résultats en grille ---------- */
@media (min-width: 860px){
  body{font-size:16px}
  .tabbar{top:0;bottom:auto;justify-content:flex-start;align-items:center;gap:6px;padding:10px max(24px,calc((100vw - 1180px)/2 + 24px));
    padding-top:calc(10px + env(safe-area-inset-top,0px));border-top:0;border-bottom:.5px solid var(--hair)}
  .brand{display:block;font-weight:700;font-size:1.05rem;letter-spacing:-.02em;margin-right:auto}
  .tab{flex:0 0 auto;flex-direction:row;gap:7px;font-size:.92rem;padding:7px 14px;border-radius:999px}
  .tab svg{width:20px;height:20px}
  .tab[aria-selected="true"]{background:var(--fill)}
  .screen{max-width:1180px;padding:84px 24px 48px}
  #scr-slots{display:grid;grid-template-columns:340px minmax(0,1fr);grid-template-areas:"filters hdr" "filters results";column-gap:32px;align-items:start}
  .hdr{grid-area:hdr}
  .filters{grid-area:filters;position:sticky;top:84px}
  .results{grid-area:results;min-width:0}
  .filters .kicker{display:none}
  .days{display:grid;grid-template-columns:repeat(7,1fr);gap:6px;margin:0 0 14px;padding:0;overflow:visible}
  .day{width:auto;padding:8px 0 7px;border-radius:12px}
  .day b{font-size:1.15rem}
  .hdr .kicker{display:none}
  .filters .grow{flex-direction:column;align-items:stretch;gap:8px;padding:12px 0}
  .filters .glabel{flex:none;font-size:.85rem;color:var(--muted);font-weight:600}
  .filters .seg{flex:none}
  .hours{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;overflow:visible;flex:none}
  .hours button{padding:7px 0}
  #list.v-time{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:4px 20px}
  #list.v-time .tgroup{margin-top:16px}
  #list.v-club{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
  #list.v-club .club{margin-top:0}
  #scr-accounts{max-width:680px}
  dialog{margin:auto;border-radius:18px}
  .toast{bottom:28px}
  .onb{padding-top:72px}
}
@media (prefers-reduced-motion: reduce){*{transition:none!important;animation:none!important}}
</style>
</head>
<body>
<main class="screen" id="scr-onboarding" hidden>
  <div class="onb">
    <p class="kicker">Padel à Orléans</p>
    <h1>Connecte tes clubs</h1>
    <p class="lead">Pour voir les créneaux libres, connecte ton compte 4PADEL et ton compte Let Padel. Tes identifiants restent chiffrés sur ton ordinateur.</p>
    <section class="group" id="onbList"></section>
    <button class="btn-wide dim" id="onbGo" type="button" disabled>Voir les créneaux</button>
    <p class="foot" id="onbFoot">Encore 2 comptes à connecter.</p>
  </div>
</main>

<main class="screen" id="scr-slots">
  <header class="hdr">
    <p class="kicker">Padel à Orléans</p>
    <div class="titlerow"><h1>Créneaux</h1><button class="pill" id="nowBtn" type="button">Maintenant</button></div>
  </header>
  <section class="filters" aria-label="Filtres">
  <div class="days" id="days" role="group" aria-label="Jour"></div>
  <section class="group">
    <div class="grow" id="durRow"><span class="glabel">Durée</span>
      <div class="seg" id="durs" role="group" aria-label="Durée">
        <button type="button" data-d="60" aria-pressed="false">1 h</button>
        <button type="button" data-d="90" aria-pressed="true">1 h 30</button>
        <button type="button" data-d="120" aria-pressed="false">2 h</button>
      </div></div>
    <div class="grow"><span class="glabel" id="hoursLbl">Dès</span><div class="hours" id="hours" role="group" aria-label="Heure"></div></div>
  </section>
  </section>
  <section class="results">
  <div class="bar">
    <p class="summary" id="summary" aria-live="polite"></p>
    <div class="seg small" id="views" role="group" aria-label="Affichage">
      <button type="button" data-v="club" aria-pressed="false">Par club</button>
      <button type="button" data-v="time" aria-pressed="true">Par horaire</button>
    </div>
  </div>
  <div id="banners"></div>
  <div id="list"></div>
  <p class="foot" id="foot"></p>
  </section>
</main>

<main class="screen" id="scr-accounts" hidden>
  <p class="kicker">Padel à Orléans</p>
  <div class="titlerow"><h1>Comptes</h1></div>
  <section class="group" id="acctList"></section>
  <p class="foot" id="acctFoot"></p>
  <h3 class="gtitle">Apparence</h3>
  <section class="group">
    <div class="grow"><span class="glabel">Thème</span>
      <div class="seg" id="themeSeg"><button type="button" data-v="auto">Auto</button><button type="button" data-v="light">Clair</button><button type="button" data-v="dark">Sombre</button></div></div>
    <div class="grow"><span class="glabel">Couleur</span>
      <div class="seg" id="accentSeg"><button type="button" data-v="blue">Bleu</button><button type="button" data-v="green">Vert</button><button type="button" data-v="orange">Orange</button></div></div>
  </section>
  <h3 class="gtitle" id="viewTitle">Au démarrage</h3>
  <section class="group" id="viewGroup">
    <div class="grow"><span class="glabel">Vue</span>
      <div class="seg" id="defViewSeg"><button type="button" data-v="time">Par horaire</button><button type="button" data-v="club">Par club</button></div></div>
  </section>
</main>

<nav class="tabbar" id="tabbar" role="tablist" aria-label="Sections">
  <span class="brand">Padel à Orléans</span>
  <button class="tab" role="tab" id="tab-slots" aria-selected="true" type="button">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><rect x="3.5" y="5" width="17" height="15.5" rx="3"/><path d="M3.5 10h17M8 3v4M16 3v4"/><circle cx="8.5" cy="14.5" r="1" fill="currentColor"/><circle cx="12" cy="14.5" r="1" fill="currentColor"/></svg>
    Créneaux</button>
  <button class="tab" role="tab" id="tab-accounts" aria-selected="false" type="button">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="10" r="3"/><path d="M6.5 18.2c1.3-2 3.2-3 5.5-3s4.2 1 5.5 3"/></svg>
    Comptes</button>
</nav>

<dialog id="sheet">
  <form class="sheet" id="credForm" autocomplete="on">
    <h2 id="sheetTitle">Se connecter</h2>
    <p class="sub" id="sheetText"></p>
    <label class="field"><span>E-mail</span><input type="email" id="cEmail" autocomplete="username" required></label>
    <label class="field"><span>Mot de passe</span><input type="password" id="cPass" autocomplete="current-password" required></label>
    <button class="btn-wide" id="gBtn" type="submit">Se connecter</button>
    <p class="status" id="gMsg" aria-live="polite"></p>
    <details class="manual" id="manual">
      <summary>Autres façons de se connecter</summary>
      <p class="sub" style="margin-top:8px">Compte créé avec Google, sans mot de passe ? Connecte-toi toi-même dans une fenêtre Chrome :</p>
      <div class="rowend" style="justify-content:flex-start"><button class="btn-ghost" id="winBtn" type="button">Ouvrir une fenêtre Chrome</button></div>
      <div id="keyBox">
        <p class="sub" style="margin-top:14px">Ou colle la valeur d’<code>authorization</code> copiée dans les outils du navigateur (F12, Réseau) :</p>
        <textarea id="tok" placeholder="eyJraWQiOi…" spellcheck="false" autocomplete="off"></textarea>
        <p class="err" id="tokErr"></p>
        <div class="rowend"><button class="btn-ghost" id="tokSave" type="button">Enregistrer la clé</button></div>
      </div>
    </details>
    <div class="rowend"><button class="btn-ghost" id="sheetClose" type="button">Fermer</button></div>
  </form>
</dialog>
<div class="toast" id="toast" role="status" aria-live="polite"></div>

<script>
const LIVE = window.PADEL_LIVE === true;
const H = (wk, we) => ({wk, we});
const CLUBS = [
  {id:"4padel", name:"4PADEL Orléans", city:"Fleury-les-Aubrais", courts:"6 pistes indoor", account:true,
   address:"113 rue de Curembourg, 45400 Fleury-les-Aubrais", phone:"02 38 58 99 37",
   hours:H([9,23.5],[9,21.5]), book:"https://www.4padel.fr/reservations/slots"},
  {id:"letpadel-pryve", name:"Let Padel Saint-Pryvé", city:"Saint-Pryvé-Saint-Mesmin", courts:"4 pistes",
   address:"10 rue des Moines, 45750 Saint-Pryvé-Saint-Mesmin", phone:"02 18 69 31 69",
   hours:H([10,24],[9,21]), book:"https://letpadel.doinsport.club/home"},
  {id:"letpadel-fleury", name:"Let Padel Fleury", city:"Fleury-les-Aubrais", courts:"4 pistes",
   address:"20 rue André Dessaux, 45400 Fleury-les-Aubrais", phone:"02 18 69 32 22",
   hours:H([10,24],[9,21]), book:"https://letpadel.doinsport.club/home"},
  {id:"smash", name:"Smash Padel", city:"Saint-Denis-en-Val", courts:"5 pistes + 2 singles", account:true,
   address:"2951 route de Sandillon, 45560 Saint-Denis-en-Val", phone:"02 19 23 11 08",
   hours:H([9,23],[9,23]), book:"https://smashpadel.mymobileapp.fr/"}
];
const CLUB = Object.fromEntries(CLUBS.map(c=>[c.id,c]));
const DAYS = ["dim.","lun.","mar.","mer.","jeu.","ven.","sam."];
const HOURS = Array.from({length:15},(_,i)=>8+i);   // 8h → 22h

/* Réglages (thème, couleur, vue au démarrage) */
const PREF_KEY="padel-orleans-prefs";
let PREFS={theme:"auto", accent:"blue", view:"time"};
try{ Object.assign(PREFS, JSON.parse(localStorage.getItem(PREF_KEY)||"{}")); }catch(e){}
function savePrefs(){ try{ localStorage.setItem(PREF_KEY, JSON.stringify(PREFS)); }catch(e){} }
function applyPrefs(){
  const r=document.documentElement;
  if(PREFS.theme==="auto") r.removeAttribute("data-theme"); else r.setAttribute("data-theme", PREFS.theme);
  r.setAttribute("data-accent", PREFS.accent);
  [["themeSeg",PREFS.theme],["accentSeg",PREFS.accent],["defViewSeg",PREFS.view]].forEach(([id,v])=>
    document.querySelectorAll(`#${id} button`).forEach(b=>b.setAttribute("aria-pressed", b.dataset.v===v?"true":"false")));
}

const S = {offset:0, t:18, dur:90, view:LIVE?PREFS.view:"club", data:null, loading:false, err:null, accounts:null};
const $ = id => document.getElementById(id);
const fmt = t => `${Math.floor(t)%24}h${String(Math.round((t%1)*60)).padStart(2,"0")}`;
const dateFor = o => {const d=new Date(); d.setHours(12,0,0,0); d.setDate(d.getDate()+o); return d;};
const iso = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
const toH = s => {const [h,m]=s.split(":").map(Number); return h+m/60;};
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const plural = (n, w) => `${n} ${w}${n>1?"s":""}`;
const shortPrice = p => p ? p.replace(" €/joueur"," €") : "";
function press(box, match){ box.querySelectorAll("button").forEach(b=>b.setAttribute("aria-pressed", match(b)?"true":"false")); }
const mapUrl = c => `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(c.name+" "+c.address)}`;

/* Contrôles */
function buildDays(){
  const box=$("days");
  for(let i=0;i<7;i++){
    const d=dateFor(i), b=document.createElement("button");
    b.type="button"; b.className="day";
    b.innerHTML=`<small>${i===0?"Auj.":DAYS[d.getDay()]}</small><b>${d.getDate()}</b>`;
    b.setAttribute("aria-label", d.toLocaleDateString("fr-FR",{weekday:"long",day:"numeric",month:"long"}));
    b.onclick=()=>{S.offset=i; press(box,x=>x===b); load();};
    box.appendChild(b);
  }
  press(box,(x,)=>x===box.children[0]);
}
function buildHours(){
  const box=$("hours");
  box.innerHTML=HOURS.map(h=>`<button type="button" data-h="${h}">${h}h</button>`).join("");
  box.querySelectorAll("button").forEach(b=>b.onclick=()=>{S.t=+b.dataset.h; syncHours(); render();});
}
function syncHours(){
  const box=$("hours"); press(box,b=>+b.dataset.h===S.t);
  const on=box.querySelector('[aria-pressed="true"]');
  if(on) box.scrollTo({left:on.offsetLeft-box.offsetLeft-40, behavior:"smooth"});
}
function setNow(){
  const n=new Date(); S.t=Math.min(22,Math.max(8,n.getHours()));
  syncHours();
  if(S.offset!==0){ S.offset=0; press($("days"),b=>b===$("days").children[0]); load(); } else render();
}

/* Données */
async function load(){
  if(!LIVE){ render(); return; }
  S.loading=true; S.err=null; render();
  try{
    const r=await fetch(`/api/slots?date=${iso(dateFor(S.offset))}&duration=${S.dur}`);
    if(!r.ok) throw new Error(`L’outil a répondu ${r.status}.`);
    S.data=await r.json();
    const n=new Date(); S.at=`${n.getHours()}h${String(n.getMinutes()).padStart(2,"0")}`;
  }catch(e){ S.data=null; S.err="L’outil sur ton PC ne répond pas. Relance lancer_padel.bat."; }
  S.loading=false; render();
}

function clubState(c){
  const x=((S.data||{}).clubs||{})[c.id]||{status:"not_configured"};
  const now=new Date(), nowH=now.getHours()+now.getMinutes()/60;
  const slots = x.status==="ok" ? (x.slots||[]).filter(s=>toH(s.time)>=S.t && !(S.offset===0 && toH(s.time)<nowH)) : [];
  return {state:x.status, msg:x.message||"", url:x.book_url||c.book, slots};
}

/* Rendu */
function render(){
  const d=dateFor(S.offset), we=d.getDay()===0||d.getDay()===6;
  const dayTxt = S.offset===0?"aujourd’hui":S.offset===1?"demain":`${DAYS[d.getDay()]} ${d.getDate()}`;
  $("banners").innerHTML=""; 
  if(!LIVE) return renderStatic(d, we, dayTxt);
  $("list").className = "v-"+S.view;
  if(S.loading){ $("summary").textContent="Recherche des créneaux…"; $("list").className=""; $("list").innerHTML='<div class="skel"></div><div class="skel"></div><div class="skel"></div>'; return; }
  if(S.err){ $("summary").textContent=""; $("list").innerHTML=`<div class="empty">${esc(S.err)}</div>`; return; }

  const rows=CLUBS.map(c=>({c,...clubState(c)}));
  // Bandeaux : comptes à connecter, clubs injoignables
  $("banners").innerHTML = rows.filter(r=>r.state==="needs_token"||r.state==="error").map(r=>
    r.state==="needs_token"
      ? `<div class="banner"><p>${esc(r.c.name)} n’est pas connecté : ses créneaux manquent.</p><button class="btn-accent" type="button" data-connect="${r.c.id}">Connecter</button></div>`
      : `<div class="banner"><p>${esc(r.c.name)} ne répond pas : ${esc(r.msg||"réessaie plus tard")}</p></div>`).join("");

  const total=rows.reduce((n,r)=>n+r.slots.length,0), nClubs=rows.filter(r=>r.slots.length).length;
  $("summary").textContent = total
    ? `${plural(total,"créneau")} ${total>1?"libres":"libre"} ${dayTxt}, ${plural(nClubs,"club")}`.replace("créneaus","créneaux")
    : `Aucun créneau libre ${dayTxt} dès ${S.t}h`;
  if(S.at){ $("summary").insertAdjacentHTML("beforeend", `<br>À jour à ${S.at} <button class="refresh" type="button" id="refreshBtn">Actualiser</button>`); }

  $("list").innerHTML = S.view==="time" ? renderByTime(rows) : renderByClub(rows);
  wire();
}

function slotTarget(r, s){
  // Smash : réservation de la piste ouverte directement ; autres clubs : page de planning du club.
  return s.book ? {tag:"button", attrs:`type="button" data-book='${esc(JSON.stringify(s.book))}'`}
                : {tag:"a", attrs:`href="${esc(r.url)}" target="_blank" rel="noopener"`};
}

function renderByTime(rows){
  const byTime={};
  rows.forEach(r=>r.slots.forEach(s=>(byTime[s.time] ||= []).push({r,s})));
  const times=Object.keys(byTime).sort();
  if(!times.length) return `<div class="empty">Rien de libre à partir de ${S.t}h. Essaie une heure plus tôt, une autre durée ou un autre jour.</div>`;
  return times.map(t=>{
    const items=byTime[t];
    return `<div class="tgroup"><h2>${t.replace(":","h")}<span>${plural(items.length,"club")}</span></h2><div class="rows">${
      items.map(({r,s})=>{ const g=slotTarget(r,s);
        return `<${g.tag} class="row" ${g.attrs}><span class="who"><strong>${esc(r.c.name)}</strong><small>${esc(r.c.city)} · ${plural(s.courts,"piste")}</small></span><span class="price">${esc(shortPrice(s.price))}</span></${g.tag}>`;
      }).join("")}</div></div>`;
  }).join("");
}

function renderByClub(rows){
  const sorted=[...rows].sort((a,b)=>b.slots.length-a.slots.length);
  return sorted.map(r=>{
    const c=r.c, n=r.slots.length;
    let badge, body="";
    if(r.state==="ok"){
      badge = n ? `<span class="badge on">${plural(n,"libre")}</span>` : `<span class="badge">Complet</span>`;
      body = n ? `<div class="chips">${r.slots.map(s=>{const g=slotTarget(r,s);
        return `<${g.tag} class="chip" ${g.attrs}>${s.time.replace(":","h")}<small>${esc(shortPrice(s.price)||plural(s.courts,"piste"))}</small></${g.tag}>`;}).join("")}</div>` : "";
    } else if(r.state==="needs_token"){ badge=`<span class="badge">Non connecté</span>`; }
    else if(r.state==="error"){ badge=`<span class="badge">Indisponible</span>`; }
    else { badge=`<span class="badge">Lien seulement</span>`; body=`<p class="msg">Créneaux pas encore lus pour ce club.</p>`; }
    return `<article class="club"><div class="club-head"><div><h3>${esc(c.name)}</h3><p class="sub">${esc(c.city)}, ${esc(c.courts)}</p></div>${badge}</div>${body}
      <div class="links"><a class="first" href="${esc(r.url)}" target="_blank" rel="noopener">Réserver</a>
      <a href="${mapUrl(c)}" target="_blank" rel="noopener">Itinéraire</a><a href="tel:${c.phone.replace(/\s/g,"")}">Appeler</a></div></article>`;
  }).join("");
}

function renderStatic(d, we, dayTxt){
  const rows=CLUBS.map(c=>{const [o,cl]=we?c.hours.we:c.hours.wk; return {c,o,cl,open:S.t>=o&&S.t<cl};}).sort((a,b)=>b.open-a.open);
  const n=rows.filter(r=>r.open).length;
  $("summary").textContent = n ? `${plural(n,"club")} ouvert${n>1?"s":""} ${dayTxt} à ${S.t}h` : `Aucun club ouvert ${dayTxt} à ${S.t}h`;
  $("list").innerHTML = rows.map(({c,o,cl,open})=>`<article class="club"><div class="club-head"><div><h3>${esc(c.name)}</h3><p class="sub">${esc(c.city)}, ${esc(c.courts)}</p></div>
    <span class="badge${open?" on":""}">${open?`Ouvert jusqu’à ${fmt(cl)}`:(S.t<o?`Ouvre à ${fmt(o)}`:"Fermé")}</span></div>
    <div class="links"><a class="first" href="${c.book}" target="_blank" rel="noopener">Réserver</a>
    <a href="${mapUrl(c)}" target="_blank" rel="noopener">Itinéraire</a><a href="tel:${c.phone.replace(/\s/g,"")}">Appeler</a></div></article>`).join("");
}

function wire(){
  const rb=document.getElementById("refreshBtn"); if(rb) rb.onclick=load;
  document.querySelectorAll("[data-book]").forEach(el=>el.onclick=()=>openBooking(el, JSON.parse(el.dataset.book)));
  document.querySelectorAll("[data-connect]").forEach(el=>el.onclick=()=>openSheet(el.dataset.connect));
}

/* Ouvrir la réservation d'une piste (Smash) */
function toast(msg, bad){
  const t=$("toast"); t.textContent=msg; t.className="toast show"+(bad?" bad":"");
  clearTimeout(toast._t); toast._t=setTimeout(()=>t.className="toast", 6000);
}
async function openBooking(el, book){
  if(el.classList.contains("busy")) return;
  el.classList.add("busy"); toast("Ouverture de la réservation dans Chrome…");
  try{
    const r=await fetch("/api/open",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({...book, duration:S.dur})});
    const j=await r.json(); toast(j.message||j.error||"Terminé.", !r.ok);
  }catch(e){ toast("L’outil sur ton PC ne répond pas. Relance-le.", true); }
  el.classList.remove("busy");
}

/* Comptes */
const checkSVG='<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
function acctRow(x, st, onboarding){
  const until = st.until ? ` jusqu’au ${esc(st.until)}` : "";
  let status;
  if(onboarding){
    status = st.connected ? `<small class="ok">Connecté${until}</small>`
      : st.saved ? `<small class="ok">Identifiants enregistrés</small>`
      : `<small>${x.required ? "Obligatoire" : "Facultatif"}</small>`;
  } else {
    status = st.connected ? `<small class="ok">Connecté${until}${st.email?` · ${esc(st.email)}`:""}</small>`
      : st.saved ? `<small>Reconnexion automatique · ${esc(st.email)}</small>`
      : `<small>${x.required ? "Obligatoire · " : ""}Non connecté</small>`;
  }
  const check = onboarding ? `<span class="check${ready(st)?" on":""}">${checkSVG}</span>` : "";
  return `<div class="acct">${check}<span class="who"><strong>${esc(x.name)}</strong>${status}</span><button class="btn-ghost" type="button" data-connect="${x.id}">${ready(st)?"Modifier":"Connecter"}</button></div>`;
}

/* Accueil : 4PADEL et Let Padel obligatoires */
async function checkGate(){
  try{ S.accounts=await (await fetch("/api/accounts")).json(); }catch(e){ S.accounts=S.accounts||{}; }
  const a=S.accounts||{};
  const missing=ACCOUNTS.filter(x=>x.required && !ready(a[x.id]));
  $("onbList").innerHTML=ACCOUNTS.map(x=>acctRow(x, a[x.id]||{}, true)).join("");
  $("onbGo").disabled=!!missing.length; $("onbGo").classList.toggle("dim", !!missing.length);
  $("onbFoot").textContent = missing.length
    ? (missing.length===2 ? "Encore 2 comptes à connecter." : `Plus que ${missing[0].name} à connecter.`)
    : "Tout est prêt. Smash Padel peut se connecter plus tard depuis l’onglet Comptes.";
  wire();
  return !missing.length;
}
async function gate(){
  const ok=await checkGate();
  if(ok){ showTab("slots"); load(); }
  else showTab("onboarding");
}
async function refreshAccounts(){
  if(!LIVE){
    $("acctList").innerHTML=`<div class="acct"><span class="who"><strong>Comptes des clubs</strong><small>Ils se gèrent dans l’outil installé sur ton PC.</small></span></div>`;
    $("acctFoot").textContent="Cette page en ligne affiche les horaires d’ouverture. Les créneaux libres s’affichent dans l’outil lancé avec lancer_padel.bat.";
    $("viewTitle").hidden=true; $("viewGroup").hidden=true; return;
  }
  try{ S.accounts=await (await fetch("/api/accounts")).json(); }catch(e){ S.accounts={}; }
  const a=S.accounts||{};
  $("acctList").innerHTML = ACCOUNTS.map(x=>acctRow(x, a[x.id]||{})).join("");
  $("acctFoot").textContent="Tes identifiants sont chiffrés sur ce PC. La connexion se refait toute seule quand elle expire.";
  wire();
}

/* Fenêtre de connexion */
const SHEETS={
  "4padel":{title:"Connecter 4PADEL", key:true},
  "letpadel":{title:"Connecter Let Padel", key:false},
  "smash":{title:"Connecter Smash Padel", key:false}};
const ACCOUNTS=[
  {id:"4padel", name:"4PADEL Orléans", note:"Fleury-les-Aubrais", required:true},
  {id:"letpadel", name:"Let Padel", note:"Un seul compte pour Saint-Pryvé et Fleury", required:true},
  {id:"smash", name:"Smash Padel", note:"Saint-Denis-en-Val", required:false}];
const ready = a => !!a && (a.connected || a.saved);   // identifiants enregistrés : reconnexion automatique
let PROVIDER="4padel";
function setG(msg, cls){ const m=$("gMsg"); m.textContent=msg; m.className="status"+(cls?" "+cls:""); }
async function openSheet(p){
  PROVIDER=SHEETS[p]?p:"4padel"; const c=SHEETS[PROVIDER];
  $("sheetTitle").textContent=c.title;
  $("sheetText").textContent="Ton e-mail et ton mot de passe sont chiffrés sur ce PC. La connexion se refait toute seule quand elle expire.";
  $("keyBox").hidden=!c.key; $("manual").open=false; $("winBtn").parentElement.hidden=!!window.PADEL_CLOUD; $("winBtn").parentElement.previousElementSibling.hidden=!!window.PADEL_CLOUD;
  $("cPass").value=""; $("tok").value=""; $("tokErr").textContent=""; setG(""); $("gBtn").disabled=false; $("winBtn").disabled=false;
  $("cEmail").value=((S.accounts||{})[PROVIDER]||{}).email||"";
  $("sheet").showModal();
  if(!S.accounts){ try{ S.accounts=await (await fetch("/api/accounts")).json(); $("cEmail").value=(S.accounts[PROVIDER]||{}).email||""; }catch(e){} }
  ($("cEmail").value?$("cPass"):$("cEmail")).focus();
}
async function afterLogin(){
  $("sheet").close();
  if(!$("scr-onboarding").hidden){ await checkGate(); return; }
  load(); refreshAccounts();
}
async function follow(){
  for(;;){
    await new Promise(r=>setTimeout(r,1000));
    const st=await (await fetch("/api/login/status")).json();
    if(st.state==="done"){ setG(st.message,"ok"); setTimeout(afterLogin,900); return; }
    if(st.state==="error"){ setG(st.message,"bad"); $("gBtn").disabled=false; $("winBtn").disabled=false; return; }
    setG(st.message);
  }
}
$("sheetClose").onclick=()=>$("sheet").close();
$("credForm").onsubmit=async e=>{
  e.preventDefault(); $("gBtn").disabled=true; setG("Connexion en cours…");
  try{
    const r=await fetch("/api/credentials",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({provider:PROVIDER,email:$("cEmail").value,password:$("cPass").value})});
    $("cPass").value="";
    if(!r.ok){ const j=await r.json(); setG(j.error||"Échec.","bad"); $("gBtn").disabled=false; return; }
    await follow();
  }catch(err){ setG("L’outil sur ton PC ne répond pas. Relance-le.","bad"); $("gBtn").disabled=false; }
};
$("winBtn").onclick=async()=>{
  $("winBtn").disabled=true; setG("Ouverture de Chrome…");
  try{ await fetch(`/api/login/start?p=${PROVIDER}&mode=window`,{method:"POST"}); await follow(); }
  catch(err){ setG("L’outil sur ton PC ne répond pas. Relance-le.","bad"); $("winBtn").disabled=false; }
};
$("tokSave").onclick=async()=>{
  const v=$("tok").value.trim().replace(/^Bearer\s+/i,"");
  if(v.split(".").length!==3){ $("tokErr").textContent="Ce n’est pas une clé valide : elle commence par « eyJ » et contient deux points."; return; }
  try{
    const r=await fetch("/api/token",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:v})});
    const j=await r.json(); if(!r.ok) throw new Error(j.error||"Enregistrement impossible.");
    afterLogin();
  }catch(e){ $("tokErr").textContent=e.message; }
};

/* Onglets et réglages */
function showTab(name){
  $("scr-onboarding").hidden = name!=="onboarding";
  $("tabbar").hidden = name==="onboarding";
  $("scr-slots").hidden = name!=="slots"; $("scr-accounts").hidden = name!=="accounts";
  $("tab-slots").setAttribute("aria-selected", name==="slots"); $("tab-accounts").setAttribute("aria-selected", name==="accounts");
  if(name==="accounts") refreshAccounts();
  window.scrollTo(0,0);
}
$("tab-slots").onclick=()=>showTab("slots");
$("tab-accounts").onclick=()=>showTab("accounts");
document.querySelectorAll("#themeSeg button").forEach(b=>b.onclick=()=>{PREFS.theme=b.dataset.v; savePrefs(); applyPrefs();});
document.querySelectorAll("#accentSeg button").forEach(b=>b.onclick=()=>{PREFS.accent=b.dataset.v; savePrefs(); applyPrefs();});
document.querySelectorAll("#defViewSeg button").forEach(b=>b.onclick=()=>{PREFS.view=b.dataset.v; savePrefs(); applyPrefs();});
document.querySelectorAll("#views button").forEach(b=>b.onclick=()=>{S.view=b.dataset.v; press($("views"),x=>x===b); render();});
document.querySelectorAll("#durs button").forEach(b=>b.onclick=()=>{S.dur=+b.dataset.d; press($("durs"),x=>x===b); load();});
$("nowBtn").onclick=setNow;
$("onbGo").onclick=()=>{ showTab("slots"); load(); };

/* Démarrage */
applyPrefs();
buildDays(); buildHours();
press($("views"),b=>b.dataset.v===S.view);
if(!LIVE){
  $("durRow").hidden=true; $("views").hidden=true; $("hoursLbl").textContent="À";
  $("foot").textContent="Horaires d’ouverture indicatifs, relevés en octobre 2026. Les créneaux libres s’affichent dans l’outil installé sur ton PC.";
} else {
  $("foot").textContent="Prix par joueur. Smash : toucher un horaire ouvre directement la réservation de la piste. Let Padel et 4PADEL : le planning du club s’ouvre, choisis-y l’horaire.";
}
const n0=new Date(); S.t=Math.min(22,Math.max(8,n0.getHours())); syncHours();
if(LIVE) gate(); else load();
</script>
</body>
</html>
'''

if __name__ == "__main__":
    main()

