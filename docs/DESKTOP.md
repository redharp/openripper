# OpenRipper desktop companion

OpenRipper can run as a Windows desktop app or as a self-hosted Linux Docker
service. Both use the same Python engine, dashboard and settings. The desktop
app is a separate companion to trev.bot flash; it does not install or call the
flasher, and contains no firmware flashing functionality.

## Windows app

Extract the entire `OpenRipper-<version>-windows-x64.zip` and open
`OpenRipper/OpenRipper.exe`. Keep the `_internal` folder beside the executable.
The optional setup executable installs the same app for your Windows account
and adds a Start menu shortcut. Python does not need to be installed.

Requirements: Windows 10/11 x64, Microsoft Edge WebView2 Runtime, and a separately
installed/activated MakeMKV for real discs. Optional encoding also requires
HandBrakeCLI. These media tools are not included in the Windows package.

On a new installation, automatic ripping is **off**. Set your destination in
Settings, check the detected drives, then enable automatic ripping when ready.
The desktop app detects MakeMKV in its normal installation folder or PATH.
Use `OPENRIPPER_MAKEMKV_BIN` for a custom installation.

Preferences, history, logs and browser storage live in
`%LOCALAPPDATA%\OpenRipper`, outside the installation directory. Upgrading or
uninstalling the app leaves this data and your ripped files intact. Existing
command-line environment settings are respected. Source-checkout preferences
and databases are not imported automatically.

The app first checks for an existing OpenRipper service on `127.0.0.1:8080`.
If one is running, it opens that service and uses its settings/history. Closing
this window leaves the existing service running. Otherwise, the app starts its
own service on that loopback address. A conflicting application on port 8080
produces a startup error instead of starting a second server on another port.

When the desktop app owns the service, closing the window asks for confirmation
and stops the service, interrupting active work. **Minimize the app to keep
ripping.** This version does not run in the system tray or start at login.
Startup failures are recorded in `%LOCALAPPDATA%\OpenRipper\desktop.log`.

## Connect to Docker or another server

Start the server using the [Docker deployment guide](DEPLOYMENT.md), then use a
Windows shortcut with a target such as:

```text
"C:\path\to\OpenRipper.exe" --connect http://192.168.1.20:8080
```

This mode only opens the remote dashboard. It does not start a local ripper, and
closing it does not stop the remote service. The drives and output paths belong
to the server, not the computer displaying the window. Real optical-drive Docker
passthrough is supported on a Linux host; use the native Windows app for drives
attached to a Windows computer. Do not expose the unauthenticated server directly
to the internet; see [SECURITY.md](../SECURITY.md).

## Build from source

Install Python 3.13 x64 and run from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build-desktop.ps1
```

The script creates an isolated build environment, bundles Python and application
dependencies with PyInstaller, tests the bundled API/dashboard in simulation
with automatic ripping disabled, and writes a portable zip under `dist/`.
The smoke test uses its own database and library and never reads real drives.
Pass `-CheckWindow` to also load the dashboard in a hidden native WebView2 window
and verify the desktop runtime. This requires WebView2 on the build machine.

To also produce a per-user installer, install Inno Setup 6, put `ISCC.exe` on
PATH, and pass `-Installer`. Local builds are unsigned; Windows may display a
publisher warning. No automatic update service is configured.

For development without freezing:

```powershell
.venv\Scripts\python.exe -m pip install -e ".[desktop]"
.venv\Scripts\python.exe -m openripper.desktop
```

The desktop shell uses [pywebview](https://pywebview.flowrl.com/guide/usage.html)
and its [PyInstaller packaging support](https://pywebview.flowrl.com/guide/freezing).
Docker does not install the optional desktop dependencies.
