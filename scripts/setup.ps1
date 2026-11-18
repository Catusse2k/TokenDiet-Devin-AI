#Requires -Version 5.1
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Warn([string]$Message) { Write-Warning $Message }
function Get-DotEnvValue([string]$Name, [string]$Default) {
    if (Test-Path ".env") {
        $line = Get-Content ".env" | Where-Object { $_ -match "^$([regex]::Escape($Name))=" } | Select-Object -First 1
        if ($line) {
            $value = ($line -split "=", 2)[1].Trim().Trim('"').Trim("'")
            if ($value) { return $value }
        }
    }
    return $Default
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    $python = Get-Command py -ErrorAction SilentlyContinue
}
if (-not $python) { throw "Python not found. Install Python 3.10+ and retry." }

$versionOutput = & $python.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$major, $minor = $versionOutput.Split('.') | ForEach-Object { [int]$_ }
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) {
    throw "Python 3.10 or newer is required (found $versionOutput)."
}

if (-not (Test-Path ".venv")) {
    Write-Step "Creating virtualenv in .venv"
    & $python.Source -m venv .venv
}

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$VenvPip = Join-Path $Root ".venv\Scripts\pip.exe"
$VenvUvicorn = Join-Path $Root ".venv\Scripts\uvicorn.exe"

Write-Step "Upgrading pip and installing dependencies"
& $VenvPython -m pip install --upgrade pip
& $VenvPip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

if (-not (Test-Path ".env")) {
    Write-Step "Creating .env from .env.example"
    Copy-Item ".env.example" ".env"
    Write-Warn "Configure API keys in .env before using economy/flagship tiers."
}

$docker = Get-Command docker -ErrorAction SilentlyContinue
if ($docker) {
    try {
        & docker compose version *> $null
        & docker info *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Step "Starting Redis via Docker Compose"
            & docker compose up -d redis
        } else {
            Write-Warn "Docker not running: the router will automatically use the local JSON cache."
        }
    } catch {
        Write-Warn "Docker unavailable or not running: the router will automatically use the local JSON cache."
    }
} else {
    Write-Warn "Docker not found: the router will automatically use the local JSON cache."
}

$HostName = Get-DotEnvValue "ROUTER_HOST" "127.0.0.1"
$Port = Get-DotEnvValue "ROUTER_PORT" "8000"
Write-Step "Starting router on http://${HostName}:$Port"
Write-Step "Endpoint: POST /v1/chat/completions - GET /health - GET /stats"
& $VenvUvicorn router.main:app --host $HostName --port $Port
