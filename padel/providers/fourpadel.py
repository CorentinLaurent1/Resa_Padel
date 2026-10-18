"""4PADEL (API LE FIVE) : créneaux et connexion."""
import urllib.error, urllib.parse, urllib.request
import datetime, json
from ..config import FOURPADEL_CENTER_ID, FOURPADEL_SPORT_PADEL, UA
from ..errors import NeedsToken
from ..net import euros, http_json
from ..tokens import token_expiry


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


def fourpadel_login(email, password):
    """Connexion 4PADEL par appel direct à l'API LE FIVE : pas de navigateur."""
    body = urllib.parse.urlencode({"email": email, "password": password}).encode()
    req = urllib.request.Request("https://api2-front.lefive.fr/login/client?appId=2&isChannelWeb=true",
                                 data=body, method="POST")
    for k, v in {"User-Agent": UA, "Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded",
                 "Origin": "https://www.4padel.fr", "Referer": "https://www.4padel.fr/"}.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            raise RuntimeError("Connexion refusée : vérifie l’e-mail et le mot de passe.")
        raise RuntimeError(f"4PADEL a répondu {e.code}.")
    token = j.get("access_token")
    if not token:
        raise RuntimeError("Réponse inattendue de 4PADEL.")
    return token
