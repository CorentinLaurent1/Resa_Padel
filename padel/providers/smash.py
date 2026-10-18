"""Smash Padel : créneaux (HTML) et connexion."""
import urllib.error, urllib.parse, urllib.request
import datetime, re, time
from ..config import SMASH_BASE, SMASH_SPORT_PADEL, UA
from ..errors import NeedsToken
from ..net import euros


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


def smash_login(email, password):
    """Connexion Smash par appel direct : le jeton revient dans l'URL de redirection (setToken)."""
    body = urllib.parse.urlencode({"token": "", "redirect": "", "email": email,
                                   "mot_de_passe": password, "nn": str(int(time.time() * 1000)),
                                   "livexperience_site_token": "null"}).encode()
    req = urllib.request.Request(f"{SMASH_BASE}/master/scriptLogin.asp", data=body, method="POST")
    for k, v in {"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded",
                 "Origin": SMASH_BASE, "Referer": SMASH_BASE + "/"}.items():
        req.add_header(k, v)
    cookies = {}

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            self._location = newurl
            return None    # on ne suit pas : on veut lire le setToken de l'URL

    opener = urllib.request.build_opener(NoRedirect)
    location = ""
    try:
        resp = opener.open(req, timeout=20)
        location = resp.headers.get("Location", "")
        for c in resp.headers.get_all("Set-Cookie") or []:
            kv = c.split(";", 1)[0]
            if "=" in kv:
                cookies[kv.split("=", 1)[0].strip()] = kv.split("=", 1)[1].strip()
    except urllib.error.HTTPError as e:
        location = e.headers.get("Location", "") if e.headers else ""
        for c in (e.headers.get_all("Set-Cookie") if e.headers else None) or []:
            kv = c.split(";", 1)[0]
            if "=" in kv:
                cookies[kv.split("=", 1)[0].strip()] = kv.split("=", 1)[1].strip()
    token = ""
    m = re.search(r"setToken=([^&]+)", location)
    if m:
        token = urllib.parse.unquote(m.group(1))
    if not token:
        raise RuntimeError("Connexion refusée : vérifie l’e-mail et le mot de passe.")
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return {"token": token, "cookies": cookie_str}
