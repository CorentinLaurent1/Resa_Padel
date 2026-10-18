"""Agrégation des créneaux de tous les clubs."""
import urllib.error, urllib.parse, urllib.request
import threading, time
from concurrent.futures import ThreadPoolExecutor
from .auth import relogin_if_possible
from .config import CLOUD, LETPADEL_CLUBS, PROVIDER_IDS
from .errors import NeedsToken
from .providers.doinsport import letpadel_slots, letpadel_url
from .providers.fourpadel import fourpadel_slots
from .providers.smash import smash_slots
from .store import session_state


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
