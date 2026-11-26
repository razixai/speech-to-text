; Inno Setup script for Speech to Text (STT)

#define MyAppName "Speech to Text"
#define MyAppShortName "STT"
#define MyAppVersion "1.0.0"
#define MyAppExeName "STT.exe"
#define MyAppPublisher "Razi"

[Setup]
AppId={{8D6E4A7B-4C7A-4F6B-9E8C-1B5D9D2C8E4A}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoCompany={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppShortName}
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
DisableWelcomePage=no
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=STT-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
AppMutex=Local\GeminiDictate
CloseApplications=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=STT.ico
ChangesAssociations=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
BeveledLabel=Author: {#MyAppPublisher}
WelcomeLabel2=This will install [name/ver] by {#MyAppPublisher} on your computer.%n%nIt is recommended that you close all other applications before continuing.

[Tasks]
Name: "startup"; Description: "Launch {#MyAppName} automatically when Windows starts"; Flags: checkedonce
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[Files]
Source: "dist\STT\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "STT.ico"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
Type: files; Name: "{userstartup}\{#MyAppShortName}.lnk"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#MyAppShortName}"; ValueData: """{app}\{#MyAppExeName}"""; Tasks: startup
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "{#MyAppShortName}"; Flags: dontcreatekey uninsdeletevalue

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\STT.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\STT.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
