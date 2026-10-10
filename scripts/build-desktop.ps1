param([switch]$Installer, [switch]$CheckWindow)
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
function Assert-Success { if ($LASTEXITCODE -ne 0) { throw "Build command failed: $LASTEXITCODE" } }
if (-not (Test-Path 'build/desktop-venv/Scripts/python.exe')) {
    py -3.13 -m venv build/desktop-venv
    Assert-Success
}
$buildPython = (Resolve-Path 'build/desktop-venv/Scripts/python.exe').Path
& $buildPython -m pip install '.[desktop,packaging]'
Assert-Success
& $buildPython -m PyInstaller --noconfirm --clean --windowed --name OpenRipper `
    --distpath dist/desktop --workpath build/desktop --specpath build `
    --collect-data openripper --collect-all webview --collect-submodules uvicorn `
    --hidden-import sqlalchemy.dialects.sqlite --hidden-import websockets.legacy.server `
    --exclude-module pytest --exclude-module tkinter packaging/desktop_entry.py
Assert-Success
Copy-Item LICENSE, THIRD_PARTY_NOTICES.md -Destination dist/desktop/OpenRipper
Copy-Item docs/DESKTOP.md -Destination dist/desktop/OpenRipper/README.md
& $buildPython scripts/collect_desktop_licenses.py
Assert-Success
$smokeDirectory = Join-Path (Resolve-Path 'build') ('desktop-smoke-' + [guid]::NewGuid().ToString('N'))
$smokeArguments = @('--smoke-test', '--data-dir', ('"' + $smokeDirectory + '"'))
if ($CheckWindow) { $smokeArguments += '--smoke-window' }
$process = Start-Process -FilePath 'dist/desktop/OpenRipper/OpenRipper.exe' `
    -ArgumentList $smokeArguments `
    -WindowStyle Hidden -PassThru
if (-not $process.WaitForExit(60000)) { $process.Kill(); throw 'Packaged smoke check timed out' }
if ($process.ExitCode -ne 0) { throw "Packaged smoke check failed. See $smokeDirectory" }
$result = Get-Content (Join-Path $smokeDirectory 'smoke-result.json') | ConvertFrom-Json
if ($result.status -ne 'passed') { throw 'Missing packaged smoke check success' }
$version = (& $buildPython -c 'import openripper; print(openripper.__version__)').Trim()
Compress-Archive -Path dist/desktop/OpenRipper -DestinationPath "dist/OpenRipper-$version-windows-x64.zip" -Force
if ($Installer) {
    $compiler = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if (-not $compiler) { throw 'Install Inno Setup 6 and add ISCC.exe to PATH to build the installer.' }
    & $compiler.Source "/DAppVersion=$version" packaging/windows.iss
    Assert-Success
}
Write-Host "Desktop package built and smoke-tested: dist/OpenRipper-$version-windows-x64.zip"
