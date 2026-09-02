#ifndef AppVersion
  #error AppVersion must be supplied by build_release.ps1
#endif

#define AppName "Fysio"
#define AppExeName "fysio.exe"

[Setup]
AppId={{FD94956B-02B1-4B33-B045-0756CF7AFC1A}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppName}
DefaultDirName={localappdata}\Fysio
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\release
OutputBaseFilename=Fysio_Setup
SetupIconFile=..\assets\fysio.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
UsePreviousAppDir=yes
VersionInfoVersion={#AppVersion}

[Dirs]
Name: "{app}\data"
Name: "{app}\backups"
Name: "{app}\archive"
Name: "{app}\logs"

[Files]
Source: "..\dist\Fysio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Fysio"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"
Name: "{autodesktop}\Fysio"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Εκκίνηση του Fysio"; Flags: nowait postinstall skipifsilent
