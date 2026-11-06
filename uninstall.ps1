# Removes a Paperduck install made by install.ps1.
#   -Purge also deletes settings and state in %USERPROFILE%\.paperduck

param([switch]$Purge)

$Root = Join-Path $env:LOCALAPPDATA "Paperduck"

# Stop running copies from this install (graceful close first so state.json is saved)
$procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe' OR Name='Paperduck.exe'" |
    Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase) }
foreach ($p in $procs) { taskkill /PID $p.ProcessId 2>$null | Out-Null }
Start-Sleep -Seconds 2
foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }

$shell = New-Object -ComObject WScript.Shell
foreach ($dir in [Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("Desktop")) {
    foreach ($name in "Paperduck.lnk") {
        $lnk = Join-Path $dir $name
        # Leave shortcuts that point at a dev checkout alone
        if ((Test-Path $lnk) -and $shell.CreateShortcut($lnk).TargetPath.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item $lnk
        }
    }
}
# Only drop the autostart value when it points at this install, not at a dev checkout
$run = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$cmd = (Get-ItemProperty $run -Name Paperduck -ErrorAction SilentlyContinue).Paperduck
if ($cmd -and $cmd.Contains($Root)) { Remove-ItemProperty $run -Name Paperduck }
Remove-Item "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Paperduck" -Recurse -ErrorAction SilentlyContinue
Remove-Item $Root -Recurse -Force -ErrorAction SilentlyContinue
if ($Purge) { Remove-Item (Join-Path $env:USERPROFILE ".paperduck") -Recurse -Force -ErrorAction SilentlyContinue }

Write-Host "Paperduck removed." $(if (-not $Purge) { "Settings kept in $env:USERPROFILE\.paperduck" })
