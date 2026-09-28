$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# Hosts and ports come from dev.config.json, the same file frontend/vite.config.js reads, so
# the launcher and the dev proxy cannot disagree. They did once: both files held their own
# copy of the port, one was changed, and the dashboard silently proxied to nothing.
# Environment variables override the file for a one-off run.
$dev = Get-Content (Join-Path $root "dev.config.json") -Raw | ConvertFrom-Json
$backendHost = if ($env:BACKEND_HOST) { $env:BACKEND_HOST } else { $dev.backendHost }
$backendPort = if ($env:BACKEND_PORT) { $env:BACKEND_PORT } else { $dev.backendPort }
$frontendPort = if ($env:FRONTEND_PORT) { $env:FRONTEND_PORT } else { $dev.frontendPort }

python -m pip install -r backend/requirements.txt

# The Playwright package is a driver; the browser it drives is a separate ~150MB download that
# pip does not fetch. Without it the JavaScript-rendering half of the crawl reports "could not
# launch a browser" and every scan silently falls back to raw HTML -- which still works, but
# leaves TECH-09 unscored and under-reports content on client-rendered sites. Failure here is
# not fatal for the same reason: a machine that cannot install the browser can still scan.
python -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
  Write-Host "Playwright browser install failed; scans will run without JavaScript rendering." -ForegroundColor Yellow
}

if (-not (Test-Path "frontend/node_modules")) {
  npm --prefix frontend install
}

# --reload-dir app, not a bare --reload.
#
# A bare --reload watches the whole backend/ directory, which contains backend/data/ -- where
# every scan writes its own output. The crawl finishes, save_crawl_output() drops a ~50MB JSON
# next to the database, uvicorn sees files change and restarts the server, and the restart
# kills the scan that was writing them. Every scan died at the same 18% progress mark (the
# crawl/scoring handover) and the dashboard showed "Waiting for the server to reconnect...".
# Watching only the source directory leaves the reloader useful for development without
# letting a scan destroy itself.
Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$root\backend'; python -m uvicorn app.main:app --reload --reload-dir app --host $backendHost --port $backendPort"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "`$env:BACKEND_HOST='$backendHost'; `$env:BACKEND_PORT='$backendPort'; `$env:FRONTEND_PORT='$frontendPort'; Set-Location '$root\frontend'; npm run dev"

Write-Host "Backend:   http://$backendHost`:$backendPort"
# Vite binds IPv6 loopback, so the numeric host refuses the connection even when the server
# is healthy. localhost resolves to ::1 and works.
Write-Host "Dashboard: http://localhost:$frontendPort"
