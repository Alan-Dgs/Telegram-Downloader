@echo off
setlocal
cd /d "%~dp0"

if not exist "config.ini" (
    copy /Y "config.ini.example" "config.ini" >nul
    echo A new config.ini file was created.
    echo Fill in api_id, api_hash, and source, then launch again.
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
)

call ".venv\Scripts\activate.bat"
python -m pip install --disable-pip-version-check --no-cache-dir -r requirements.txt
python telegram_channel_downloader.py --config config.ini
exit /b %errorlevel%

