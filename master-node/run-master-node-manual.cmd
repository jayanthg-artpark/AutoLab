@echo off
rem Starts the manual copy of the master node (port 8001): a person moves the plate, not Thor.
rem Settings: config.manual.toml and instruments.manual.toml. Runs alongside run-master-node.cmd.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Run run-master-node.cmd once first; it sets up the Python packages.
    pause
    exit /b 1
)

set "MASTER_NODE_CONFIG=config.manual.toml"
echo Starting the manual master node on port 8001. Keep this window open; close it to stop it.
".venv\Scripts\python" -m master_node
pause
