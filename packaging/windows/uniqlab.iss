; Inno Setup script for UniqLab (per-user install, no administrator rights).
; Compile from the repository root after PyInstaller:  iscc /DAppVersion=1.0.0 packaging\windows\uniqlab.iss
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6F3C2B8E-5D7A-4C1E-9B2F-0A8D4E6C1B37}
AppName=UniqLab
AppVersion={#AppVersion}
AppVerName=UniqLab {#AppVersion}
AppPublisher=onyx.x (i275k)
AppPublisherURL=https://github.com/i275k
AppSupportURL=https://github.com/i275k
AppCopyright=Copyright (c) 2026 onyx.x (i275k)
DefaultDirName={localappdata}\Programs\UniqLab
DefaultGroupName=UniqLab
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=UniqLab-{#AppVersion}-windows-x64
SetupIconFile=UniqLab.ico
UninstallDisplayIcon={app}\UniqLab.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
LicenseFile=..\..\LICENSE
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\..\dist\UniqLab\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\UniqLab"; Filename: "{app}\UniqLab.exe"
Name: "{autodesktop}\UniqLab"; Filename: "{app}\UniqLab.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\UniqLab.exe"; Description: "{cm:LaunchProgram,UniqLab}"; Flags: nowait postinstall skipifsilent

; Settings and components downloaded by the app live in %LOCALAPPDATA%\UniqLab and are kept on
; uninstall, so a reinstall works offline; delete that folder to remove them.
