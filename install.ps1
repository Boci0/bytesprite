# ByteSprite installer / updater for Windows 10 and 11 (per-user, no admin).
#
#   From a clone:   powershell -ExecutionPolicy Bypass -File .\install.ps1
#   One line:       irm https://raw.githubusercontent.com/Boci0/bytesprite/main/install.ps1 | iex
#
# Layout:  %LOCALAPPDATA%\ByteSprite\app   (src, assets, uninstall.ps1, .installed marker)
#          %LOCALAPPDATA%\ByteSprite\venv  (private Python environment)
#          %USERPROFILE%\.bytesprite        (settings and state, kept across updates)
# Re-running it updates an existing install in place.

param(
    [switch]$DesktopShortcut,
    [switch]$StartWithWindows,
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"
$Repo = "Boci0/bytesprite"
$Root = Join-Path $env:LOCALAPPDATA "ByteSprite"
$App = Join-Path $Root "app"
$Venv = Join-Path $Root "venv"

function Find-Python {
    $ErrorActionPreference = "Continue"  # a missing runtime writes to stderr, which 5.1 would make fatal
    foreach ($cmd in @(@("py", "-3.12"), @("py", "-3"), @("python"))) {
        $exe = Get-Command $cmd[0] -ErrorAction SilentlyContinue
        if (-not $exe) { continue }
        $pyArgs = @($cmd | Select-Object -Skip 1)
        $ver = & $exe.Source @pyArgs -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and [version]$ver -ge [version]"3.11") {
            return , (@($exe.Source) + $pyArgs)  # leading comma: never unroll to a bare string
        }
    }
    throw "Python 3.11+ not found. Install it with:  winget install Python.Python.3.12"
}

$py = Find-Python

# 1. Source: this folder when run from a clone, else the latest GitHub release
$Source = $PSScriptRoot
$Temp = $null
if (-not $Source -or -not (Test-Path (Join-Path $Source "src\main.py"))) {
    Write-Host "Downloading the latest ByteSprite release..."
    $release = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest"
    $Temp = Join-Path $env:TEMP ("bytesprite_" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $Temp | Out-Null
    $zip = Join-Path $Temp "src.zip"
    Invoke-WebRequest $release.zipball_url -OutFile $zip -UseBasicParsing
    Expand-Archive $zip -DestinationPath $Temp
    $Source = (Get-ChildItem $Temp -Directory | Select-Object -First 1).FullName
}

# 2. Copy the app (src is mirrored so deleted modules do not linger)
Write-Host "Installing to $Root"
New-Item -ItemType Directory -Force -Path $App | Out-Null
robocopy (Join-Path $Source "src") (Join-Path $App "src") /MIR /XD __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Copying src failed (robocopy $LASTEXITCODE)" }
robocopy (Join-Path $Source "assets") (Join-Path $App "assets") /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
foreach ($f in "requirements.txt", "uninstall.ps1", "README.md", "bytesprite.pyw") {
    Copy-Item (Join-Path $Source $f) $App -Force
}
Set-Content (Join-Path $App ".installed") "installed by install.ps1" -Encoding ascii

# 3. Private venv + dependencies
if (-not (Test-Path (Join-Path $Venv "Scripts\pythonw.exe"))) {
    Write-Host "Creating Python environment..."
    $pyArgs = @($py | Select-Object -Skip 1)
    & $py[0] @pyArgs -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}
$VenvPy = Join-Path $Venv "Scripts\python.exe"
$Pythonw = Join-Path $Venv "Scripts\pythonw.exe"
& $VenvPy -m pip install --disable-pip-version-check -q --require-hashes -r (Join-Path $App "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# 4. Shortcuts
function New-Shortcut([string]$Path) {
    $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut($Path)
    $lnk.TargetPath = $Pythonw
    $lnk.Arguments = "-m src.main"
    $lnk.WorkingDirectory = $App
    $lnk.IconLocation = Join-Path $App "assets\bytesprite.ico"
    $lnk.Description = "ByteSprite desktop companion"
    $lnk.Save()
}
New-Shortcut (Join-Path ([Environment]::GetFolderPath("Programs")) "ByteSprite.lnk")
if ($DesktopShortcut) { New-Shortcut (Join-Path ([Environment]::GetFolderPath("Desktop")) "ByteSprite.lnk") }

# 5. Apps & features entry (HKCU, so no admin)
$version = (& $VenvPy -c "import sys;sys.path.insert(0,r'$App');import src;print(src.__version__)").Trim()
$key = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\ByteSprite"
New-Item -Path $key -Force | Out-Null
$uninstall = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$App\uninstall.ps1`""
@{
    DisplayName = "ByteSprite"; DisplayVersion = $version; Publisher = "ByteSprite"
    DisplayIcon = Join-Path $App "assets\bytesprite.ico"; InstallLocation = $Root
    UninstallString = $uninstall; URLInfoAbout = "https://github.com/$Repo"
}.GetEnumerator() | ForEach-Object { Set-ItemProperty -Path $key -Name $_.Key -Value $_.Value }
Set-ItemProperty -Path $key -Name NoModify -Value 1 -Type DWord
Set-ItemProperty -Path $key -Name NoRepair -Value 1 -Type DWord

# 6. Optional Start with Windows (same Run value the in-app toggle writes)
if ($StartWithWindows) {
    Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name ByteSprite -Value "`"$Pythonw`" `"$App\src\autostart.pyw`""
}

if ($Temp) { Remove-Item $Temp -Recurse -Force -ErrorAction SilentlyContinue }
Write-Host "ByteSprite $version installed. Start Menu: ByteSprite"
if (-not $NoLaunch) { Start-Process $Pythonw -ArgumentList "-m", "src.main" -WorkingDirectory $App }
