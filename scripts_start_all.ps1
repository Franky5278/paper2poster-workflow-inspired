# Start everything Paper2Poster needs after a reboot.
#   1. the two local services the Dify workflow calls (44461 extraction, 44462 layout/render)
#   2. the Dify docker stack
# Usage:  pwsh -File start_all.ps1 [-SkipDocker]
param(
  [switch]$SkipDocker
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$py = Join-Path $root '.venv-p2p\Scripts\python.exe'
$logDir = Join-Path $root 'work'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$env:PYTHONIOENCODING = 'utf-8'
$env:P2P_PUBLIC_HOST = '10.91.235.33'
$env:HF_HOME = Join-Path $root 'work\hf-cache'
$env:HF_ENDPOINT = 'https://hf-mirror.com'
$env:HUGGINGFACE_HUB_CACHE = Join-Path $env:HF_HOME 'hub'

function Test-Port($port) {
  try { return (Test-NetConnection -ComputerName 127.0.0.1 -Port $port -InformationLevel Quiet -WarningAction SilentlyContinue) }
  catch { return $false }
}

if (-not (Test-Port 44461)) {
  Start-Process -FilePath $py -ArgumentList @('-m', 'uvicorn', 'extract_service:app', '--host', '0.0.0.0', '--port', '44461') `
    -WorkingDirectory (Join-Path $root 'services') -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir 'extract.out.log') -RedirectStandardError (Join-Path $logDir 'extract.err.log')
  Write-Host 'started extract service :44461'
} else { Write-Host 'extract service already listening on 44461' }

if (-not (Test-Port 44462)) {
  Start-Process -FilePath $py -ArgumentList @('-m', 'uvicorn', 'poster_service:app', '--host', '0.0.0.0', '--port', '44462') `
    -WorkingDirectory (Join-Path $root 'services') -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir 'poster.out.log') -RedirectStandardError (Join-Path $logDir 'poster.err.log')
  Write-Host 'started poster service  :44462'
} else { Write-Host 'poster service already listening on 44462' }

Start-Sleep -Seconds 8
foreach ($p in 44461, 44462) {
  $state = if (Test-Port $p) { 'UP' } else { 'DOWN' }
  "$p -> $state"
}

if (-not $SkipDocker) {
  Write-Host 'starting the Dify stack ...'
  Push-Location (Join-Path $root 'repos\dify\docker')
  & docker compose up -d
  Pop-Location
  Start-Sleep -Seconds 20
  & docker compose -f (Join-Path $root 'repos\dify\docker\docker-compose.yaml') ps
}
