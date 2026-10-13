# Fleet bot watchdog (Windows server). Scheduled Task "fleet_bot_watchdog" runs this every minute and at boot:
# if venv\Scripts\python.exe (main.py) is not running, it starts the bot in the background with logs to bot.log.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root 'venv\Scripts\python.exe'
$log = Join-Path $root 'bot.log'

$running = Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $py }
if ($running) { exit 0 }

if ((Test-Path $log) -and (Get-Item $log).Length -gt 20MB) {
    Move-Item $log "$log.1" -Force
}
Add-Content -Path $log -Value "=== watchdog start $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" -Encoding utf8
Start-Process -FilePath cmd.exe -ArgumentList '/c', 'set PYTHONUTF8=1&& venv\Scripts\python.exe main.py >> bot.log 2>&1' `
    -WorkingDirectory $root -WindowStyle Hidden
