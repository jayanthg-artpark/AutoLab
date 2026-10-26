@echo off
rem Starts the manual copy of the protocol tool at http://localhost:5501. Its runs go to the manual
rem master node (port 8001, run-master-node-manual.cmd), where a person moves the plate, not Thor.
rem Settings: config.manual.toml. Runs alongside run-protocol-tool.cmd.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Run run-protocol-tool.cmd once first; it sets up the Python packages.
    pause
    exit /b 1
)

set "PROTOCOL_TOOL_CONFIG=config.manual.toml"
echo Starting the manual protocol tool at http://localhost:5501 - keep this window open; close it to stop.
start "" cmd /c "timeout /t 4 >nul & start http://localhost:5501"
".venv\Scripts\python" -m protocol_tool
pause
