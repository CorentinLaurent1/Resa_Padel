"""Espace personnel chiffré de chaque visiteur.

Le cookie « padel_sid » contient un secret aléatoire dont on dérive l'identifiant
de rangement (empreinte), une clé de chiffrement et une clé d'authentification.
Chiffrement : flux HMAC-SHA256 en mode compteur + HMAC (encrypt-then-MAC), stdlib seule."""
import base64, datetime, hashlib, hmac, json, os, secrets, threading, time
from .config import DATA_DIR, STORE_FILE
from .tokens import token_expiry


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
