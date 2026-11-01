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
# Must match RELEASE_PUBLIC_KEY in src/updater.py and sha256(src/signing.py); tests/test_updater.py checks both
$ReleasePublicKey = "f0cc212a80d53fe877827253c020e2c01d023c4280eebf47423c6032a0259316"
$SigningPySha256 = "B415F26BF5BA35C3BA626DCB6D22FED740714BBBC5E185B5A99B766B7B73ED15"

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
# Any failure below still removes the download folder; break re-throws the error
trap { if ($Temp) { Remove-Item $Temp -Recurse -Force -ErrorAction SilentlyContinue }; break }
if (-not $Source -or -not (Test-Path (Join-Path $Source "src\main.py"))) {
    Write-Host "Downloading the latest ByteSprite release..."
    $release = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest"
    $Temp = Join-Path $env:TEMP ("bytesprite_" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $Temp | Out-Null
    $zip = Join-Path $Temp "bytesprite.zip"
    $sig = "$zip.sig"
    $verifier = Join-Path $Temp "signing.py"
    foreach ($pair in @(@("bytesprite.zip", $zip), @("bytesprite.zip.sig", $sig))) {
        $asset = $release.assets | Where-Object name -eq $pair[0]
        if (-not $asset) { throw "Release $($release.tag_name) has no $($pair[0]); refusing an unsigned install" }
        Invoke-WebRequest $asset.browser_download_url -OutFile $pair[1] -UseBasicParsing
    }
    # The verifier comes from the repo, pinned by hash so it is exactly as trusted as this script
    Invoke-WebRequest "https://raw.githubusercontent.com/$Repo/main/src/signing.py" -OutFile $verifier -UseBasicParsing
    if ((Get-FileHash $verifier -Algorithm SHA256).Hash -ne $SigningPySha256) { throw "signing.py does not match the pinned hash" }
    $check = "import importlib.util as u,sys;s=u.spec_from_file_location('signing',sys.argv[1]);m=u.module_from_spec(s);s.loader.exec_module(m);" +
             "sys.exit(0 if m.verify(bytes.fromhex(sys.argv[4]),open(sys.argv[2],'rb').read(),bytes.fromhex(open(sys.argv[3]).read().strip())) else 1)"
    $pyArgs = @($py | Select-Object -Skip 1)
    & $py[0] @pyArgs -I -c $check $verifier $zip $sig $ReleasePublicKey
    if ($LASTEXITCODE -ne 0) { throw "Release signature check failed; nothing was installed" }
    Expand-Archive $zip -DestinationPath $Temp
    $Source = Join-Path $Temp "bytesprite"
}

# 2. Copy the app (src is mirrored so deleted modules do not linger)
Write-Host "Installing to $Root"
New-Item -ItemType Directory -Force -Path $App | Out-Null
robocopy (Join-Path $Source "src") (Join-Path $App "src") /MIR /XD __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Copying src failed (robocopy $LASTEXITCODE)" }
robocopy (Join-Path $Source "assets") (Join-Path $App "assets") /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
foreach ($f in "requirements.txt", "uninstall.ps1", "README.md", "bytesprite.pyw") {
    $p = Join-Path $Source $f
    if (Test-Path $p) { Copy-Item $p $App -Force }  # signed bundles omit the legacy bytesprite.pyw
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
# No --require-hashes: pip checks hashes whenever the file has them, and older signed releases have none
& $VenvPy -m pip install --disable-pip-version-check -q -r (Join-Path $App "requirements.txt")
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
