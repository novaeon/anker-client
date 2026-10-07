; Inno Setup script for AnkerClient (per-user install, no admin rights needed).
;
; Build (after PyInstaller):
;   iscc /DAppVersion=1.0.0 packaging\installer.iss
; Output: dist\AnkerClient-<version>-setup.exe

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "AnkerClient"
#define AppExe "AnkerClient.exe"
#define AppPublisher "AnkerClient contributors"
#define AppURL "https://github.com/novaeon/anker-client"

[Setup]
AppId={{6E3C1D6B-6A55-4E3B-9C7A-2D1E8B3B9A41}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename={#AppName}-{#AppVersion}-setup
SetupIconFile=..\anker_client\resources\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\AnkerClient\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Remove the "start with Windows" entry the app may have created.
Filename: "{sys}\reg.exe"; Parameters: "delete HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v AnkerClient /f"; Flags: runhidden; RunOnceId: "RemoveAutostart"

; User data (%APPDATA%\AnkerClient, %LOCALAPPDATA%\AnkerClient) and installed games are
; intentionally left in place on uninstall.
