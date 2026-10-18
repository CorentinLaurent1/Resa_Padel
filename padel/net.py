"""Petits utilitaires réseau et d'affichage."""
import urllib.error, urllib.parse, urllib.request
import json
from .config import UA


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
