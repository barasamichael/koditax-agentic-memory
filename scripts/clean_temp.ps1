Write-Host "Cleaning temporary test artifacts..."

Remove-Item ".tmp" -Recurse -Force -ErrorAction SilentlyContinue
