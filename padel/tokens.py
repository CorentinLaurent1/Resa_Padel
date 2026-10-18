"""Lecture des jetons JWT des clubs."""
import base64, datetime, json


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
