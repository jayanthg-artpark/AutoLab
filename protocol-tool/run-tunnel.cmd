@echo off
rem Gives the protocol tool (running on this PC) a public https link through a Cloudflare
rem quick tunnel, so people on other networks can use it. The link changes every time this
rem starts. Keep this window open; closing it takes the link down.
setlocal
cd /d "%~dp0"
set "CLOUDFLARED=%~dp0..\tools\cloudflared.exe"

if not exist ".venv\Scripts\python.exe" (
    echo Run run-protocol-tool.cmd once first.
    pause
    exit /b 1
)
".venv\Scripts\python" -c "from protocol_tool.config import load_settings; import sys; sys.exit(0 if load_settings().server.access_password else 1)"
if errorlevel 1 (
    echo.
    echo WARNING: no password is set, so anyone with the link can use your AI key and send
    echo runs to the lab. Share the link privately, or set access_password under [server]
    echo in config.toml and restart the protocol tool.
    echo.
)

if not exist "%CLOUDFLARED%" (
    echo Downloading cloudflared from Cloudflare's GitHub releases...
    powershell -NoProfile -Command "New-Item -ItemType Directory -Force '%~dp0..\tools' | Out-Null; Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile '%CLOUDFLARED%'"
    if errorlevel 1 goto :failed
)

del /q tunnel.log 2>nul
echo The protocol tool must be running too (run-protocol-tool.cmd).
echo Share the https://....trycloudflare.com link printed below, plus the password from config.toml.
"%CLOUDFLARED%" tunnel --url http://localhost:5500 --no-autoupdate --logfile tunnel.log
pause
exit /b 0

:failed
echo Could not download cloudflared; check the internet connection.
pause
exit /b 1
