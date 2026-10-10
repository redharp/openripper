@echo off
setlocal
cd /d "%~dp0"
if not exist "data" mkdir "data"
set "OPENRIPPER_SIMULATE=true"
set "OPENRIPPER_UDEV_DISCOVERY=false"
set "OPENRIPPER_LIBRARY_ROOT=%~dp0data\demo-library"
set "OPENRIPPER_DATABASE_URL=sqlite:///%~dp0data\demo.db"
set "OPENRIPPER_PREFERENCES_PATH=%~dp0data\demo-settings.json"
set "OPENRIPPER_POLL_INTERVAL=2"
echo OpenRipper demo: simulated drives and placeholder files. Open http://localhost:8080
.venv\Scripts\python.exe -m openripper.main
