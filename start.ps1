$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

python -m pip install -r backend/requirements.txt
if (-not (Test-Path "frontend/node_modules")) {
  npm --prefix frontend install
}

Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$root\backend'; python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8010"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$root\frontend'; npm run dev -- --port 5174"

Write-Host "Backend: http://127.0.0.1:8010"
Write-Host "Dashboard: http://127.0.0.1:5174"
