@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=
py --version >nul 2>&1 && set PY=py
if not defined PY python --version >nul 2>&1 && set PY=python
if not defined PY (
  echo.
  echo Python n'est pas encore installé sur ce PC.
  echo La page de téléchargement va s'ouvrir : installe Python en cochant
  echo "Add python.exe to PATH", puis relance ce fichier.
  start "" https://www.python.org/downloads/
  pause
  exit /b
)
%PY% -c "import playwright" >nul 2>&1 || (
  echo Installation du module de connexion, une seule fois...
  %PY% -m pip install --user playwright
)
%PY% padel_dispo.py
pause
