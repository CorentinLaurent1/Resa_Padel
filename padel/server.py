"""Serveur web HTTP (routes, cookies, en-têtes de sécurité)."""
import urllib.error, urllib.parse, urllib.request
import base64, datetime, json, os, re, threading, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .auth import LOGINS, credentials_worker, interactive_login, rate_ok, start_login
from .booking import open_smash_booking
from .config import ACCESS, CLOUD, HOST, PORT, PROVIDERS, ROOT
from .service import accounts, all_slots
from .store import COOKIE, Session, purge_old


with open(os.path.join(ROOT, "padel", "web", "index.html"), encoding="utf-8") as _f:
    PAGE = _f.read()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    # --- outils
    def cookies(self):
        out = {}
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k and v:
                out[k] = v
        return out

    def session(self):
        """Espace de données : compte connecté si présent, sinon espace par navigateur."""
        self._new_cookie = None
        self._renew_cookie = None
        ck = self.cookies()
        v = ck.get(COOKIE)
        if v:
            try:
                sess = Session(v)
                self._renew_cookie = v
                return sess
            except Exception:
                pass
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
        # En-têtes de sécurité (signalés par PageSpeed), servis pour chaque réponse.
        self.send_header("X-Frame-Options", "DENY")                 # pas d'intégration en iframe (anti-clickjacking)
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")  # isole la fenêtre des autres documents
        if self.https():
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")   # force HTTPS
        if ctype.startswith("text/html"):
            # CSP adaptée à cette page : styles/polices Google, images intégrées, scripts internes seulement.
            self.send_header("Content-Security-Policy",
                "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
                "img-src 'self' data:; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src https://fonts.gstatic.com; "
                "script-src 'self' 'unsafe-inline'; "
                "connect-src 'self'")
        flags = "; HttpOnly; SameSite=Lax; Path=/" + ("; Secure" if self.https() else "")
        exp = (datetime.datetime.utcnow() + datetime.timedelta(days=180)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        if clear_cookie:
            self.send_header("Set-Cookie", f"{COOKIE}=; Max-Age=0" + flags)
        else:
            value = getattr(self, "_new_cookie", None) or getattr(self, "_renew_cookie", None)
            if value:
                self.send_header("Set-Cookie", f"{COOKIE}={value}; Max-Age={180 * 86400}; Expires={exp}" + flags)
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
            LOGINS.pop(sess.id, None)
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
