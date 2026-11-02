# Removes a ByteSprite install made by install.ps1.
#   -Purge also deletes settings and state in %USERPROFILE%\.bytesprite

param([switch]$Purge)

$Root = Join-Path $env:LOCALAPPDATA "ByteSprite"

# Stop running copies from this install (graceful close first so state.json is saved)
$procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe' OR Name='ByteSprite.exe'" |
    Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase) }
foreach ($p in $procs) { taskkill /PID $p.ProcessId 2>$null | Out-Null }
Start-Sleep -Seconds 2
foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }

$shell = New-Object -ComObject WScript.Shell
foreach ($dir in [Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("Desktop")) {
    $lnk = Join-Path $dir "ByteSprite.lnk"
    # Leave shortcuts that point at a dev checkout alone
    if ((Test-Path $lnk) -and $shell.CreateShortcut($lnk).TargetPath.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item $lnk
    }
}
# Only drop the autostart value when it points at this install, not at a dev checkout
$run = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$cmd = (Get-ItemProperty $run -Name ByteSprite -ErrorAction SilentlyContinue).ByteSprite
if ($cmd -and $cmd.Contains($Root)) { Remove-ItemProperty $run -Name ByteSprite }
Remove-Item "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\ByteSprite" -Recurse -ErrorAction SilentlyContinue
Remove-Item $Root -Recurse -Force -ErrorAction SilentlyContinue
if ($Purge) { Remove-Item (Join-Path $env:USERPROFILE ".bytesprite") -Recurse -Force -ErrorAction SilentlyContinue }

Write-Host "ByteSprite removed." $(if (-not $Purge) { "Settings kept in $env:USERPROFILE\.bytesprite" })
