$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$envFile = Join-Path $PSScriptRoot ".env"
$python = Join-Path $PSScriptRoot ".venv312\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $envFile)) {
    throw "Missing .env. Copy .env.example to .env and configure BOT_TOKEN."
}
if (-not (Test-Path -LiteralPath $python)) {
    throw "Missing .venv312. Follow the Windows installation steps in README.md first."
}
if (-not (Get-Content -LiteralPath $envFile | Where-Object { $_ -match '^BOT_TOKEN\s*=\s*[^#\s]' })) {
    throw "BOT_TOKEN is empty in .env. Add the token from BotFather without sharing it."
}

$pythonVersion = & $python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0) {
    throw "Could not read the Python version from .venv."
}
$versionParts = $pythonVersion.Trim().Split(".")
if ([int]$versionParts[0] -lt 3 -or ([int]$versionParts[0] -eq 3 -and [int]$versionParts[1] -lt 12)) {
    throw "Python 3.12 or newer is required. Recreate .venv with Python 3.12+."
}

Write-Host "Starting the bot. Keep this window open; press Ctrl+C to stop it."
& $python -m telegram_bot.main
exit $LASTEXITCODE
