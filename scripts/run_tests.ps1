# Windows-safe pytest runner

$rootTemp = if ($env:TEMP) {
    Join-Path $env:TEMP 'kodi_pytest'
} else {
    Join-Path $PWD 'pytest_tmp'
}
New-Item -ItemType Directory -Force -Path $rootTemp | Out-Null

$guid = [guid]::NewGuid().ToString('N')
$baseTemp = Join-Path $rootTemp "pytest_$guid"

Write-Host "Running pytest with isolated temp dir: $baseTemp"

pytest -q --basetemp $baseTemp
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
