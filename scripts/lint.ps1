# Lint/format checks (Black + Flake8)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$python = Join-Path $PSScriptRoot "..\\.venv\\Scripts\\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Expected venv python not found at: $python"
}

Write-Host "Running black --check ."
& $python -m black --check .
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Running flake8"
& $python -m flake8 --jobs 1
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
