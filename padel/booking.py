"""Ouverture de la réservation d'une piste Smash dans Chrome (sur le PC uniquement)."""
from .browser import VISIBLE_LOCK, fill_login, visible_browser
from .config import SMASH_BASE, SMASH_HOST_MATCH


SMASH_PICK_JS = r"""([ids, stamp]) => {
  for (const id of ids) {
    const b = document.getElementById('terrain_' + id);
    if (b) { choose(id, b.getAttribute('data-nom'), stamp, '', b.getAttribute('data-type')); return b.getAttribute('data-nom'); }
  }
  return null;
}"""


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
