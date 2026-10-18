"""Let Padel (plateforme Doinsport) : créneaux publics et connexion."""
import urllib.error, urllib.parse, urllib.request
from ..config import DOINSPORT_PADEL_ACTIVITY, LETPADEL_CATEGORY, LETPADEL_LOGIN_CLUB, LETPADEL_LOGIN_URL, LETPADEL_WHITE_LABEL
from ..net import euros, http_json


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
