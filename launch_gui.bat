@echo off
setlocal
cd /d "%~dp0"

if not exist "config.ini" (
    copy /Y "config.ini.example" "config.ini" >nul
)

if not exist ".venv\Scripts\pythonw.exe" (
    py -3 -m venv .venv
)

if not exist ".venv\Scripts\pythonw.exe" exit /b 1

call ".venv\Scripts\activate.bat"
python -m pip install --disable-pip-version-check --no-cache-dir -r requirements.txt >nul
start "" ".venv\Scripts\pythonw.exe" "telegram_downloader_gui.py"
exit /b 0

