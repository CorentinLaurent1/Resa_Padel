#!/usr/bin/env python3
"""
Padel Orléans : créneaux libres des clubs d'Orléans, pour tout le monde.

Chaque visiteur connecte SES comptes (4PADEL, Let Padel, Smash Padel). Ses sessions
et, s'il le souhaite, ses identifiants sont chiffrés avec une clé qui ne vit que
dans son navigateur (cookie) : le serveur seul ne peut pas les lire.

Sur ton PC :       py padel_dispo.py      (ou double-clic sur lancer_padel.bat)
Sur un hébergeur : la variable PORT est lue automatiquement (Render…).
Options : PADEL_ACCESS (mot de passe d'accès au site, facultatif),
          PADEL_DATA_DIR (dossier où garder les données chiffrées).

Dépendances : Python 3.8+ et playwright (py -m pip install playwright),
plus Chrome/Edge sur ton PC, ou « playwright install chromium » sur un serveur.

Architecture (package padel/) :
  config      constantes et variables d'environnement
  providers/  un module par club (créneaux + connexion directe)
  store       espace chiffré par visiteur (cookie)
  browser     Playwright / Chrome
  auth        flux de connexion, reconnexion silencieuse
  booking     ouverture d'une réservation Smash (PC)
  service     agrégation des créneaux
  server      HTTP ; l'interface est dans padel/web/index.html
"""
from padel.server import main

if __name__ == "__main__":
    main()
