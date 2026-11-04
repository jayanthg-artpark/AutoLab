@echo off
rem Starts the protocol tool website. The first run sets up its Python packages in .venv (needs internet).
rem Every start also upgrades the Opentrons simulator: the lab always runs the newest Opentrons software.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python" -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)" 2>nul
    if errorlevel 1 (
        echo This setup was made with a Python other than 3.12, which the newest Opentrons simulator needs.
        echo Setting it up again with Python 3.12...
        rmdir /s /q .venv
    )
)
if exist ".venv\Scripts\python.exe" goto :update

echo Setting up the protocol tool for the first time. This downloads the Opentrons simulator and takes a few minutes...
rem The newest Opentrons simulator needs Python 3.12: it fails on 3.11, and its numpy 1.x has no 3.13 build.
set "PY="
py -3.12 -c "pass" >nul 2>&1 && set "PY=py -3.12"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1 && set "PY=python"
if not defined PY (
    echo.
    echo Python 3.12 is needed for the newest Opentrons simulator ^(it doesn't work on 3.11 or 3.13^).
    echo Install Python 3.12 from https://www.python.org/downloads/ ^(it can sit next to a newer one^),
    echo then run this again.
    pause
    exit /b 1
)
%PY% -m venv .venv || goto :failed
".venv\Scripts\python" -m pip install --upgrade pip || goto :failed
".venv\Scripts\python" -m pip install -e . || goto :failed

:update
echo Checking for a newer Opentrons simulator...
".venv\Scripts\python" -m pip install --quiet --upgrade --disable-pip-version-check opentrons
if errorlevel 1 echo Could not check for Opentrons updates ^(no internet?^); starting with the installed version.
".venv\Scripts\python" -c "import opentrons; print('Opentrons simulator', opentrons.__version__)"
rem The assistant looks up labware in knowledge\reference\opentrons-labware; keep it in step with the simulator.
".venv\Scripts\python" -m protocol_tool.knowledge_import opentrons-labware

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
