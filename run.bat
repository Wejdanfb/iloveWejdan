@echo off
REM MCH PDF Organizer - run on this Windows PC for testing.
REM Needs: Python 3.10+ (tick "Add python.exe to PATH") and LibreOffice (libreoffice.org).
cd /d "%~dp0"
python -m pip install --quiet -r requirements.txt
if errorlevel 1 (
  echo Could not install the Python packages. Is Python installed and on PATH?
  pause
  exit /b 1
)
start "" http://127.0.0.1:5052
set HOST=127.0.0.1
python app.py
pause
