#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
[Setup]
AppId={{730EAA8D-6E49-41A8-A025-B2B0878BB1F7}
AppName=OpenRipper
AppVersion={#AppVersion}
AppPublisher=OpenRipper contributors
AppPublisherURL=https://github.com/redharp/openripper
DefaultDirName={localappdata}\Programs\OpenRipper
DefaultGroupName=OpenRipper
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=OpenRipper-{#AppVersion}-windows-x64-setup
LicenseFile=..\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\OpenRipper.exe
CloseApplications=yes
[Files]
Source: "..\dist\desktop\OpenRipper\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\OpenRipper"; Filename: "{app}\OpenRipper.exe"
Name: "{autodesktop}\OpenRipper"; Filename: "{app}\OpenRipper.exe"; Tasks: desktopicon
[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
[Run]
Filename: "{app}\OpenRipper.exe"; Description: "Open OpenRipper"; Flags: nowait postinstall skipifsilent
