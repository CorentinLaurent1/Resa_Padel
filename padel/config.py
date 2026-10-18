"""Configuration : variables d'environnement et constantes des clubs."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PORT = int(os.environ.get("PORT", 8765))
CLOUD = "PORT" in os.environ                      # lancé par un hébergeur (Render…)
HOST = os.environ.get("HOST", "0.0.0.0" if CLOUD else "127.0.0.1")
ACCESS = os.environ.get("PADEL_ACCESS", "")       # mot de passe d'accès au site (facultatif)
DATA_DIR = os.environ.get("PADEL_DATA_DIR", os.path.join(ROOT, "donnees"))
STORE_FILE = os.path.join(DATA_DIR, "utilisateurs.json")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")

# --- Configuration des clubs (relevée dans les captures HAR) -----------------
DOINSPORT_PADEL_ACTIVITY = "ce8c306e-224a-4f24-aa9d-6500580924dc"
# Deux centres Let Padel, confirmés par les captures (les pistes diffèrent).
LETPADEL_CLUBS = {
    "letpadel-fleury": "6a99f5f6-32bd-4fe3-ba15-8d22b1f58c1f",   # CAR ONLINE, NVL, PM FILTRATION, VIRTUO
    "letpadel-pryve": "fc96f79e-ef94-4fa6-9b01-d494eccaa54f",    # PISTE AUTOVISION, Piste, VIRTUO, NDA
}
# categoryId vue dans l'URL select-booking de chaque centre (vide si non connue).
LETPADEL_CATEGORY = {"letpadel-pryve": "be10916d-f273-4f52-ad9b-da6379dca5c9"}
LETPADEL_HOME = "https://letpadel.doinsport.club/home"
LETPADEL_LOGIN_URL = "https://api-v3.doinsport.club/client_login_check"
LETPADEL_LOGIN_CLUB = "/clubs/6a99f5f6-32bd-4fe3-ba15-8d22b1f58c1f"   # un seul compte Let Padel pour les deux centres
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
PROFILE_DIR = os.path.join(ROOT, ".chrome-padel")
CDP_PORT = 9223
PROVIDER_IDS = ("4padel", "letpadel", "smash")
PROVIDERS = {
    "4padel": {"url": SIGNIN_URL, "name": "4PADEL", "hint": "Dans la fenêtre Chrome, connecte-toi à 4PADEL."},
    "letpadel": {"url": LETPADEL_HOME, "name": "Let Padel", "hint": "Dans la fenêtre Chrome, connecte-toi à Let Padel."},
    "smash": {"url": SMASH_BASE + "/", "name": "Smash Padel",
              "hint": "Dans la fenêtre Chrome, connecte-toi à Smash Padel puis ouvre « Nouvelle réservation »."},
}
