@echo off
setlocal
cd /d "%~dp0"
rem Uses native MakeMKV and SQLite. Saved dashboard settings take precedence.
if not exist ".venv\Scripts\python.exe" (
  echo Install Python 3.12+, then run: py -m venv .venv
  echo Then run: .venv\Scripts\python.exe -m pip install -e ".[dev]"
  exit /b 1
)
if not exist "data" mkdir "data"
set "OPENRIPPER_SIMULATE=false"
set "OPENRIPPER_UDEV_DISCOVERY=false"
if not defined OPENRIPPER_MAKEMKV_BIN set "OPENRIPPER_MAKEMKV_BIN=C:\Program Files (x86)\MakeMKV\makemkvcon64.exe"
if not exist "%OPENRIPPER_MAKEMKV_BIN%" (
  echo MakeMKV was not found. Install MakeMKV or set OPENRIPPER_MAKEMKV_BIN to its executable.
  exit /b 1
)
if not defined OPENRIPPER_LIBRARY_ROOT set "OPENRIPPER_LIBRARY_ROOT=%~dp0library"
if not defined OPENRIPPER_DATABASE_URL (
  set "OPENRIPPER_DATABASE_URL=sqlite:///%~dp0data\openripper.db"
  rem Keep existing local job history when upgrading.
  if exist "data\disc-goblin.db" if not exist "data\openripper.db" set "OPENRIPPER_DATABASE_URL=sqlite:///%~dp0data\disc-goblin.db"
)
if not defined OPENRIPPER_POLL_INTERVAL set "OPENRIPPER_POLL_INTERVAL=5"
echo Starting OpenRipper. Open http://localhost:8080 and choose Configure to set the destination.
.venv\Scripts\python.exe -m openripper.main
