@echo off
rem Starts the protocol tool website. The first run sets up its Python packages in .venv (needs internet).
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Setting up the protocol tool for the first time. This downloads the Opentrons simulator and takes a few minutes...
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>nul
    if errorlevel 1 (
        echo.
        echo Python 3.11 or newer is needed. Install it from https://www.python.org/downloads/
        echo and tick "Add python.exe to PATH" during setup, then run this again.
        pause
        exit /b 1
    )
    python -m venv .venv || goto :failed
    ".venv\Scripts\python" -m pip install --upgrade pip || goto :failed
    ".venv\Scripts\python" -m pip install -e . || goto :failed
)

echo Starting the protocol tool at http://localhost:5500 - keep this window open; close it to stop.
start "" cmd /c "timeout /t 4 >nul & start http://localhost:5500"
".venv\Scripts\python" -m protocol_tool
pause
exit /b 0

:failed
echo.
echo Setup failed; see the messages above. Delete the .venv folder and run this again.
pause
exit /b 1
