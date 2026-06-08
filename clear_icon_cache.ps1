Stop-Process -Name explorer -Force
Start-Sleep -Milliseconds 800
$cache = $env:LOCALAPPDATA
Remove-Item "$cache\IconCache.db" -Force -ErrorAction SilentlyContinue
Get-ChildItem "$cache\Microsoft\Windows\Explorer" -Filter "iconcache_*.db" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
Start-Process explorer
Write-Host "Icon cache cleared. Icons should refresh shortly."
