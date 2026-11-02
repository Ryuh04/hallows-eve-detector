@echo off
rem Installs what the Hallows' Eve Detector needs and makes shortcuts to start it.
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo Python isn't installed. Get it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" in the installer, then run this again.
    pause
    exit /b 1
)
echo Installing the Python packages the app needs (a few minutes the first time)...
python -m pip install --upgrade -r requirements.txt
if errorlevel 1 (
    echo.
    echo Installing packages failed; see the messages above.
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_shortcut.ps1"
echo.
echo All set. Start the app with the "Hallows' Eve Detector" shortcut on your Desktop.
pause
