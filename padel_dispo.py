#!/usr/bin/env python3
"""
Padel Orléans : créneaux libres des clubs d'Orléans, pour tout le monde.

Chaque visiteur connecte SES comptes (4PADEL, Let Padel, Smash Padel). Ses sessions
et, s'il le souhaite, ses identifiants sont chiffrés avec une clé qui ne vit que
dans son navigateur (cookie) : le serveur seul ne peut pas les lire.

Sur ton PC :       py padel_dispo.py      (ou double-clic sur lancer_padel.bat)
Sur un hébergeur : la variable PORT est lue automatiquement (Render…).
Options : PADEL_ACCESS (mot de passe d'accès au site, facultatif),
          PADEL_DATA_DIR (dossier où garder les données chiffrées).

Dépendances : Python 3.8+ et playwright (py -m pip install playwright),
plus Chrome/Edge sur ton PC, ou « playwright install chromium » sur un serveur.
"""
import base64, collections, datetime, hashlib, hmac, json, os, re, secrets, shutil, subprocess, sys, threading, time
import urllib.error, urllib.parse, urllib.request, webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", 8765))
CLOUD = "PORT" in os.environ                      # lancé par un hébergeur (Render…)
HOST = os.environ.get("HOST", "0.0.0.0" if CLOUD else "127.0.0.1")
ACCESS = os.environ.get("PADEL_ACCESS", "")       # mot de passe d'accès au site (facultatif)
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("PADEL_DATA_DIR", os.path.join(HERE, "donnees"))
STORE_FILE = os.path.join(DATA_DIR, "utilisateurs.json")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")

# --- Configuration des clubs (relevée dans les captures HAR) -----------------
DOINSPORT_PADEL_ACTIVITY = "ce8c306e-224a-4f24-aa9d-6500580924dc"
LETPADEL_CATEGORY = {"letpadel-fleury": "be10916d-f273-4f52-ad9b-da6379dca5c9"}
LETPADEL_CLUBS = {
    "letpadel-pryve": "6a99f5f6-32bd-4fe3-ba15-8d22b1f58c1f",
    "letpadel-fleury": "fc96f79e-ef94-4fa6-9b01-d494eccaa54f",
}
LETPADEL_HOME = "https://letpadel.doinsport.club/home"
LETPADEL_LOGIN_URL = "https://api-v3.doinsport.club/client_login_check"
LETPADEL_LOGIN_CLUB = "/clubs/6a99f5f6-32bd-4fe3-ba15-8d22b1f58c1f"
LETPADEL_WHITE_LABEL = "/clubs/white-labels/9547ffa3-fb4e-4f84-944f-b73d64c05934"
SMASH_BASE = "https://smashpadel.mymobileapp.fr"
SMASH_SPORT_PADEL = "2"
FOURPADEL_CENTER_ID = 9
FOURPADEL_SPORT_PADEL = 3
SIGNIN_URL = "https://www.4padel.fr/signin?redirect=%2Freservations%2Fslots"
API_HOST_MATCH = "lefive.fr"
SMASH_HOST_MATCH = "smashpadel.mymobileapp.fr"
DOIN_HOST_MATCH = "doinsport.club"
LETPADEL_PAGE_MATCH = "letpadel.doinsport.club"
PROFILE_DIR = os.path.join(HERE, ".chrome-padel")
CDP_PORT = 9223
PROVIDER_IDS = ("4padel", "letpadel", "smash")
PROVIDERS = {
    "4padel": {"url": SIGNIN_URL, "name": "4PADEL", "hint": "Dans la fenêtre Chrome, connecte-toi à 4PADEL."},
    "letpadel": {"url": LETPADEL_HOME, "name": "Let Padel", "hint": "Dans la fenêtre Chrome, connecte-toi à Let Padel."},
    "smash": {"url": SMASH_BASE + "/", "name": "Smash Padel",
              "hint": "Dans la fenêtre Chrome, connecte-toi à Smash Padel puis ouvre « Nouvelle réservation »."},
}

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


SLOT_RE = re.compile(r"""date-countDispo="(\d+)"\s+onclick="choosePop\('(\d\d/\d\d/\d{4} (\d\d:\d\d):\d\d)','([^']*)','([^']*)'""")


TOKEN_RE = re.compile(r"livexperience_site_token=([\w-]+)")


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

# =============================================================================
#  Espace personnel chiffré de chaque visiteur
# =============================================================================
# Le cookie « padel_sid » contient un secret aléatoire. On en dérive :
#   - l'identifiant de rangement (empreinte : le secret n'est jamais stocké),
#   - une clé de chiffrement et une clé d'authentification.
# Chiffrement : flux HMAC-SHA256 en mode compteur + HMAC (encrypt-then-MAC),
# uniquement avec la bibliothèque standard.
COOKIE = "padel_sid"
STORE_LOCK = threading.Lock()
_STORE = None


def _derive(secret):
    raw = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
    if len(raw) != 32:
        raise ValueError("cookie invalide")
    h = lambda label: hashlib.sha256(label + raw).digest()
    return h(b"padel-id").hex(), h(b"padel-enc"), h(b"padel-mac")


def _stream(key, nonce, n):
    out = bytearray()
    i = 0
    while len(out) < n:
        out += hmac.new(key, nonce + i.to_bytes(8, "big"), hashlib.sha256).digest()
        i += 1
    return bytes(out[:n])


def seal(enc, mac, obj):
    data = json.dumps(obj, separators=(",", ":")).encode()
    nonce = secrets.token_bytes(16)
    ct = bytes(a ^ b for a, b in zip(data, _stream(enc, nonce, len(data))))
    tag = hmac.new(mac, nonce + ct, hashlib.sha256).digest()
    return base64.b64encode(nonce + ct + tag).decode()


def unseal(enc, mac, blob):
    raw = base64.b64decode(blob)
    nonce, ct, tag = raw[:16], raw[16:-32], raw[-32:]
    if not hmac.compare_digest(tag, hmac.new(mac, nonce + ct, hashlib.sha256).digest()):
        raise ValueError("données altérées")
    return json.loads(bytes(a ^ b for a, b in zip(ct, _stream(enc, nonce, len(ct)))))


def _store():
    global _STORE
    if _STORE is None:
        try:
            with open(STORE_FILE, encoding="utf-8") as f:
                _STORE = json.load(f)
        except (FileNotFoundError, ValueError):
            _STORE = {}
    return _STORE


def _flush():
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STORE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_STORE, f)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, STORE_FILE)


class Session:
    """L'espace d'un visiteur. Toutes les données passent par load() / update()."""

    def __init__(self, secret):
        self.secret = secret
        self.id, self._enc, self._mac = _derive(secret)

    @classmethod
    def new(cls):
        return cls(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="))

    def load(self):
        with STORE_LOCK:
            blob = _store().get(self.id, {}).get("data")
        if not blob:
            return {}
        try:
            return unseal(self._enc, self._mac, blob)
        except Exception:
            return {}

    def update(self, fn):
        with STORE_LOCK:
            st = _store()
            rec = st.get(self.id, {})
            try:
                data = unseal(self._enc, self._mac, rec["data"]) if rec.get("data") else {}
            except Exception:
                data = {}
            fn(data)
            st[self.id] = {"data": seal(self._enc, self._mac, data), "seen": int(time.time())}
            _flush()
            return data

    def forget(self):
        with STORE_LOCK:
            if _store().pop(self.id, None) is not None:
                _flush()
        LOGINS.pop(self.id, None)


def purge_old(days=180):
    limit = time.time() - days * 86400
    with STORE_LOCK:
        st = _store()
        old = [k for k, v in st.items() if v.get("seen", 0) < limit]
        for k in old:
            st.pop(k)
        if old:
            _flush()


# --- Sessions des clubs dans l'espace du visiteur ------------------------------
def session_state(data, provider):
    """(connecté ?, date d'expiration ou None)"""
    if provider == "smash":
        sm = data.get("smash") or {}
        return bool(sm.get("token")), None
    tok = data.get(provider)
    if not tok:
        return False, None
    exp = token_expiry(tok)
    return (exp is None or exp > datetime.datetime.now()), exp


# =============================================================================
#  Lecture des créneaux
# =============================================================================
_SLOT_CACHE = {}
_CACHE_LOCK = threading.Lock()


def cached(key, ttl, fn):
    now = time.time()
    with _CACHE_LOCK:
        hit = _SLOT_CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _CACHE_LOCK:
        _SLOT_CACHE[key] = (now, val)
        if len(_SLOT_CACHE) > 500:
            for k in sorted(_SLOT_CACHE, key=lambda k: _SLOT_CACHE[k][0])[:250]:
                _SLOT_CACHE.pop(k, None)
    return val


def fourpadel_slots(data, date, duration):
    token = data.get("4padel")
    if not token:
        raise NeedsToken("Connecte ton compte 4PADEL pour voir ses créneaux.")
    exp = token_expiry(token)
    if exp and exp < datetime.datetime.now():
        raise NeedsToken(f"Ta connexion 4PADEL a expiré le {exp:%d/%m}. Reconnecte-toi.")
    d = datetime.date.fromisoformat(date)
    auth = token if " " in token else f"Bearer {token}"
    headers = {"Authorization": auth, "Origin": "https://www.4padel.fr", "Referer": "https://www.4padel.fr/"}
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
                raise NeedsToken("4PADEL a refusé la connexion : reconnecte-toi.")
            raise
    return parse_fourpadel(found, duration)


def smash_slots(data, date, duration):
    sess = data.get("smash") or {}
    if not sess.get("token"):
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


# =============================================================================
#  Connexion aux clubs (navigateur invisible)
# =============================================================================
LOGINS = {}                                   # état de connexion par visiteur
BROWSERS = threading.BoundedSemaphore(int(os.environ.get("PADEL_BROWSERS", "2")))
VISIBLE_LOCK = threading.Lock()
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


def letpadel_login(email, password):
    """Connexion Let Padel par appel direct à l'API Doinsport : pas de navigateur."""
    body = {"username": email, "password": password, "club": LETPADEL_LOGIN_CLUB,
            "clubWhiteLabel": LETPADEL_WHITE_LABEL, "origin": "white_label_app"}
    try:
        j = http_json(LETPADEL_LOGIN_URL, "POST", body,
                      {**DOIN_HEADERS, "Content-Type": "application/json"})
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            raise RuntimeError("Connexion refusée : vérifie l’e-mail et le mot de passe.")
        raise RuntimeError(f"Let Padel a répondu {e.code}.")
    token = j.get("token")
    if not token:
        raise RuntimeError("Réponse inattendue de Let Padel.")
    return token


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


def auto_login(provider, email, password, report=lambda m: None):
    """Connexion avec e-mail et mot de passe. Renvoie la session du club."""
    if provider == "letpadel":
        report("Connexion à Let Padel…")
        return letpadel_login(email, password)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Le module playwright n’est pas installé sur ce serveur.")
    chrome = find_chrome()                      # un vrai Chrome du système, s'il y en a un
    report("En attente d’un navigateur libre…")
    with BROWSERS, sync_playwright() as pw:
        browser = launch_browser(pw, chrome)
        try:
            ctx = browser.new_context(locale="fr-FR", user_agent=UA, viewport={"width": 1280, "height": 900})
            found = {}
            _attach_capture(ctx, provider, found)
            pg = ctx.new_page()
            report("Ouverture de la page de connexion…")
            pg.goto(PROVIDERS[provider]["url"], wait_until="domcontentloaded", timeout=45000)
            pg.wait_for_timeout(1500)
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
            _attach_capture(ctx, provider, found)
            set_login(sess.id, "waiting", cfg["hint"])
            deadline = time.time() + 300
            while time.time() < deadline:
                pages = [p for p in ctx.pages if not p.is_closed()]
                if not pages:
                    raise RuntimeError("La fenêtre Chrome a été fermée avant la connexion.")
                if _scan(ctx, provider, found):
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


# --- Ouvrir la réservation d'une piste Smash (sur ton PC uniquement) -----------
def open_smash_booking(sess, stamp, ids, duration):
    from playwright.sync_api import sync_playwright
    data = sess.load()
    day = stamp.split(" ")[0]
    with VISIBLE_LOCK, sync_playwright() as pw:
        browser, fresh = visible_browser(pw, SMASH_BASE + "/")
        ctx = browser.contexts[0]
        sm = data.get("smash") or {}
        cookies = []
        for part in (sm.get("cookies") or "").split(";"):
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
            c = (data.get("creds") or {}).get("smash")
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
        try:
            pg.wait_for_function("() => !document.getElementById('terrain_" + ids[0] + "')", timeout=10000)
        except Exception:
            return "La page de confirmation tarde à s’afficher : regarde la fenêtre Chrome."
        return f"Réservation ouverte : {name.strip()} à {stamp[11:16].replace(':', 'h')}."


# =============================================================================
#  Agrégation
# =============================================================================
def run(sess, data, provider, fn, *args):
    try:
        return {"status": "ok", "slots": fn(data, *args) if provider else fn(*args)}
    except NeedsToken as e:
        if provider:
            fresh = relogin_if_possible(sess, data, provider)
            if fresh:
                try:
                    return {"status": "ok", "slots": fn(fresh, *args)}
                except Exception:
                    pass
        return {"status": "needs_token", "message": str(e)}
    except urllib.error.HTTPError as e:
        return {"status": "error", "message": f"Le club a répondu {e.code}."}
    except Exception as e:
        return {"status": "error", "message": f"Pas de réponse ({type(e).__name__})."}


def keep_letpadel_alive(sess, data):
    ok, _ = session_state(data, "letpadel")
    if not ok and (data.get("creds") or {}).get("letpadel"):
        threading.Thread(target=relogin_if_possible, args=(sess, data, "letpadel"), daemon=True).start()


def all_slots(sess, date, duration):
    data = sess.load()
    keep_letpadel_alive(sess, data)
    jobs = {"4padel": ("4padel", fourpadel_slots, date, duration),
            "smash": ("smash", smash_slots, date, duration)}
    for key, cid in LETPADEL_CLUBS.items():
        # Créneaux Let Padel publics : partagés entre visiteurs pendant 60 s.
        jobs[key] = (None, lambda c=cid: cached(("lp", c, date, duration), 60, lambda: letpadel_slots(c, date, duration)))
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {k: ex.submit(run, sess, data, *v) for k, v in jobs.items()}
        clubs = {k: f.result() for k, f in futs.items()}
    for key, cid in LETPADEL_CLUBS.items():
        clubs[key]["book_url"] = letpadel_url(key, cid)
    if CLOUD:   # sur un serveur, impossible d'ouvrir Chrome chez le visiteur : lien vers le site du club
        for slot in clubs.get("smash", {}).get("slots", []) or []:
            slot.pop("book", None)
    return {"date": date, "duration": duration, "clubs": clubs}


def accounts(data):
    out = {}
    for p in PROVIDER_IDS:
        ok, exp = session_state(data, p)
        out[p] = {"email": (data.get("emails") or {}).get(p, ""), "connected": ok,
                  "until": f"{exp:%d/%m}" if ok and exp else None,
                  "saved": bool((data.get("creds") or {}).get(p))}
    return out


# =============================================================================
#  Serveur web
# =============================================================================
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    # --- outils
    def session(self):
        """Espace du visiteur, d'après son cookie (créé au besoin)."""
        self._new_cookie = None
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and v:
                try:
                    return Session(v)
                except Exception:
                    break
        s = Session.new()
        self._new_cookie = s.secret
        return s

    def https(self):
        return CLOUD or self.headers.get("X-Forwarded-Proto", "") == "https"

    def send(self, code, body, ctype="application/json; charset=utf-8", clear_cookie=False):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        flags = "; HttpOnly; SameSite=Lax; Path=/" + ("; Secure" if self.https() else "")
        if clear_cookie:
            self.send_header("Set-Cookie", f"{COOKIE}=; Max-Age=0" + flags)
        elif getattr(self, "_new_cookie", None):
            self.send_header("Set-Cookie", f"{COOKIE}={self._new_cookie}; Max-Age={180 * 86400}" + flags)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n > 20000:
            raise ValueError("trop gros")
        return json.loads(self.rfile.read(n) or b"{}")

    def client_ip(self):
        fwd = self.headers.get("X-Forwarded-For", "")
        return fwd.split(",")[0].strip() if fwd else self.client_address[0]

    def allowed(self):
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
        return urllib.parse.urlparse(origin).netloc in (self.headers.get("Host", ""), f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    # --- GET
    def do_GET(self):
        if not self.allowed():
            return
        u = urllib.parse.urlparse(self.path)
        sess = self.session()
        if u.path == "/":
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
            return self.send(200, all_slots(sess, date, duration))
        if u.path == "/api/login/status":
            return self.send(200, LOGINS.get(sess.id, {"state": "idle", "message": ""}))
        if u.path == "/api/accounts":
            return self.send(200, accounts(sess.load()))
        self.send(404, {"error": "Introuvable."})

    # --- POST
    def do_POST(self):
        if not self.allowed():
            return
        if not self.same_origin():
            return self.send(403, {"error": "Origine refusée."})
        sess = self.session()
        path = urllib.parse.urlparse(self.path).path
        try:
            j = self.body()
        except Exception:
            return self.send(400, {"error": "Requête invalide."})

        if path == "/api/credentials":
            provider = j.get("provider")
            email, password = str(j.get("email", "")).strip(), str(j.get("password", ""))
            if provider not in PROVIDERS or "@" not in email or not password or len(email) > 200 or len(password) > 200:
                return self.send(400, {"error": "E-mail ou mot de passe manquant."})
            if not (rate_ok("s:" + sess.id) and rate_ok("ip:" + self.client_ip(), limit=20)):
                return self.send(429, {"error": "Trop de tentatives. Réessaie dans quelques minutes."})
            if not start_login(sess, provider, credentials_worker, email, password, bool(j.get("remember", True))):
                return self.send(409, {"error": "Une connexion est déjà en cours, patiente un instant."})
            return self.send(200, LOGINS[sess.id])

        if path == "/api/login/window":
            if CLOUD:
                return self.send(400, {"error": "Sur le site en ligne, connecte-toi avec ton e-mail et ton mot de passe."})
            provider = j.get("provider")
            if provider not in PROVIDERS:
                return self.send(400, {"error": "Club inconnu."})
            start_login(sess, provider, interactive_login)
            return self.send(200, LOGINS[sess.id])

        if path == "/api/disconnect":
            provider = j.get("provider")
            if provider not in PROVIDERS:
                return self.send(400, {"error": "Club inconnu."})
            def fn(d):
                d.pop(provider, None)
                (d.get("creds") or {}).pop(provider, None)
            sess.update(fn)
            return self.send(200, {"ok": True})

        if path == "/api/forget":
            sess.forget()
            return self.send(200, {"ok": True}, clear_cookie=True)

        if path == "/api/open":
            if CLOUD:
                return self.send(400, {"error": "Ouverture directe possible seulement sur ton PC."})
            try:
                stamp, ids, duration = j["date"], [str(int(x)) for x in j["courts"]], int(j["duration"])
                assert re.fullmatch(r"\d\d/\d\d/\d{4} \d\d:\d\d:\d\d", stamp) and ids and duration in (60, 90, 120)
            except Exception:
                return self.send(400, {"error": "Créneau invalide."})
            try:
                return self.send(200, {"message": open_smash_booking(sess, stamp, ids, duration)})
            except Exception as e:
                return self.send(500, {"error": f"Ouverture impossible ({str(e) or type(e).__name__})."})

        self.send(404, {"error": "Introuvable."})


def main():
    purge_old()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Padel Orléans tourne sur le port {PORT}  (Ctrl+C pour arrêter)")
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
.remember{display:flex;gap:12px;align-items:flex-start;margin-top:16px;cursor:pointer}
.remember input{width:20px;height:20px;margin:2px 0 0;accent-color:var(--accent);flex:0 0 auto}
.remember b{display:block;font-weight:600;font-size:.95rem}
.remember small{display:block;color:var(--muted);font-size:.82rem;line-height:1.35;margin-top:2px}
.danger{border:0;background:none;color:var(--red);font-weight:600;font-size:.95rem;padding:14px;width:100%;border-radius:14px;background:var(--card);cursor:pointer;margin-top:22px}
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
    <p class="lead">Connecte tes comptes 4PADEL, Let Padel et Smash Padel pour voir leurs créneaux libres. Pas encore de compte dans un club ? Crée-le sur son site, c’est gratuit.</p>
    <section class="group" id="onbList"></section>
    <button class="btn-wide dim" id="onbGo" type="button" disabled>Voir les créneaux</button>
    <p class="foot" id="onbFoot">Encore 3 comptes à connecter.</p>
    <p class="foot">Tes connexions sont chiffrées avec une clé gardée dans ton navigateur. Tu peux tout supprimer depuis l’onglet Comptes.</p>
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
  <button class="danger" id="forgetBtn" type="button">Supprimer mes données</button>
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
    <label class="remember"><input type="checkbox" id="cRemember" checked><span><b>Rester connecté</b><small>Reconnexion automatique quand la session du club expire. Ton mot de passe est chiffré avec une clé gardée dans ton navigateur.</small></span></label>
    <button class="btn-wide" id="gBtn" type="submit">Se connecter</button>
    <p class="status" id="gMsg" aria-live="polite"></p>
    <details class="manual" id="manual">
      <summary>Compte créé avec Google, sans mot de passe ?</summary>
      <p class="sub" style="margin-top:8px" id="noPwText"></p>
      <div class="rowend" style="justify-content:flex-start"><button class="btn-ghost" id="winBtn" type="button">Ouvrir une fenêtre Chrome</button></div>
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
    ? (missing.length===1 ? `Plus que ${missing[0].name} à connecter.` : `Encore ${missing.length} comptes à connecter.`)
    : "Tout est prêt. Tu peux voir les créneaux.";
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
    $("viewTitle").hidden=true; $("viewGroup").hidden=true; $("forgetBtn").hidden=true; return;
  }
  try{ S.accounts=await (await fetch("/api/accounts")).json(); }catch(e){ S.accounts={}; }
  const a=S.accounts||{};
  $("acctList").innerHTML = ACCOUNTS.map(x=>acctRow(x, a[x.id]||{})).join("");
  $("acctFoot").textContent="Tes connexions sont chiffrées avec une clé gardée dans ce navigateur : personne d’autre, pas même le serveur seul, ne peut les lire. Sur un autre appareil, reconnecte tes comptes.";
  wire();
}

/* Fenêtre de connexion */
const SHEETS={
  "4padel":{title:"Connecter 4PADEL", club:"4PADEL"},
  "letpadel":{title:"Connecter Let Padel", club:"Let Padel"},
  "smash":{title:"Connecter Smash Padel", club:"Smash Padel"}};
const ACCOUNTS=[
  {id:"4padel", name:"4PADEL Orléans", note:"Fleury-les-Aubrais", required:true},
  {id:"letpadel", name:"Let Padel", note:"Un seul compte pour Saint-Pryvé et Fleury", required:true},
  {id:"smash", name:"Smash Padel", note:"Saint-Denis-en-Val", required:true}];
const ready = a => !!a && (a.connected || a.saved);   // identifiants enregistrés : reconnexion automatique
let PROVIDER="4padel";
function setG(msg, cls){ const m=$("gMsg"); m.textContent=msg; m.className="status"+(cls?" "+cls:""); }
async function openSheet(p){
  PROVIDER=SHEETS[p]?p:"4padel"; const c=SHEETS[PROVIDER];
  $("sheetTitle").textContent=c.title;
  $("sheetText").textContent=`Utilise l’e-mail et le mot de passe de ton compte ${c.club}, ceux que tu tapes sur son site.`;
  $("manual").open=false;
  $("noPwText").textContent = window.PADEL_CLOUD
    ? `Crée un mot de passe avec « Mot de passe oublié » sur le site ${c.club}, puis reviens ici.`
    : "Crée un mot de passe avec « Mot de passe oublié » sur le site du club, ou connecte-toi toi-même dans une fenêtre Chrome :";
  $("winBtn").parentElement.hidden=!!window.PADEL_CLOUD;
  const saved=((S.accounts||{})[PROVIDER]||{});
  $("cRemember").checked = saved.email ? !!saved.saved : true;
  $("cPass").value=""; setG(""); $("gBtn").disabled=false; $("winBtn").disabled=false;
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
  const deadline=Date.now()+360000;   // au-delà de 6 min, on cesse d'attendre
  let fails=0;
  function stop(){ $("gBtn").disabled=false; $("winBtn").disabled=false; }
  while(Date.now()<deadline){
    await new Promise(r=>setTimeout(r,1000));
    let st;
    try{ st=await (await fetch("/api/login/status")).json(); fails=0; }
    catch(e){ if(++fails>=8){ setG("Connexion perdue avec le serveur. Réessaie.","bad"); stop(); return; } continue; }
    if(st.state==="done"){ setG(st.message,"ok"); setTimeout(afterLogin,900); return; }
    if(st.state==="error"){ setG(st.message,"bad"); stop(); return; }
    setG(st.message||"Connexion en cours…");
  }
  setG("La connexion prend trop de temps. Réessaie.","bad"); stop();
}
$("sheetClose").onclick=()=>$("sheet").close();
$("credForm").onsubmit=async e=>{
  e.preventDefault(); $("gBtn").disabled=true; setG("Connexion en cours…");
  try{
    const r=await fetch("/api/credentials",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({provider:PROVIDER,email:$("cEmail").value,password:$("cPass").value,remember:$("cRemember").checked})});
    $("cPass").value="";
    if(!r.ok){ const j=await r.json(); setG(j.error||"Échec.","bad"); $("gBtn").disabled=false; return; }
    await follow();
  }catch(err){ setG("Le serveur ne répond pas. Réessaie dans un instant.","bad"); $("gBtn").disabled=false; }
};
$("winBtn").onclick=async()=>{
  $("winBtn").disabled=true; setG("Ouverture de Chrome…");
  try{ await fetch("/api/login/window",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({provider:PROVIDER})}); await follow(); }
  catch(err){ setG("L’outil sur ton PC ne répond pas. Relance-le.","bad"); $("winBtn").disabled=false; }
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
$("forgetBtn").onclick=async()=>{
  if(!confirm("Supprimer tes comptes et identifiants enregistrés ici ? Tu devras reconnecter tes clubs.")) return;
  try{ await fetch("/api/forget",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"}); }catch(e){}
  S.accounts=null; S.data=null; gate();
};

/* Démarrage */
applyPrefs();
buildDays(); buildHours();
press($("views"),b=>b.dataset.v===S.view);
if(!LIVE){
  $("durRow").hidden=true; $("views").hidden=true; $("hoursLbl").textContent="À";
  $("foot").textContent="Horaires d’ouverture indicatifs, relevés en octobre 2026. Les créneaux libres s’affichent dans l’outil installé sur ton PC.";
} else {
  $("foot").textContent = window.PADEL_CLOUD
    ? "Prix par joueur. Toucher un horaire ouvre le site du club : choisis-y le même horaire pour réserver."
    : "Prix par joueur. Smash : toucher un horaire ouvre directement la réservation de la piste. Let Padel et 4PADEL : le planning du club s’ouvre, choisis-y l’horaire.";
}
const n0=new Date(); S.t=Math.min(22,Math.max(8,n0.getHours())); syncHours();
if(LIVE) gate(); else load();
</script>
</body>
</html>
'''

if __name__ == "__main__":
    main()