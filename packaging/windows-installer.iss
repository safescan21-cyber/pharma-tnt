; Inno Setup script - builds PharmaTnT-Setup-<version>.exe from dist\PharmaTnT
#define AppVersion GetEnv("APP_VERSION")
#if AppVersion == ""
  #define AppVersion "2.1.0"
#endif

[Setup]
AppId={{6B0E8D4C-6F2B-4C1E-9A43-2D5C7F1A9E10}
AppName=Pharma Track & Trace
AppVersion={#AppVersion}
AppPublisher=Pharma Track & Trace
DefaultDirName={autopf}\Pharma Track & Trace
DefaultGroupName=Pharma Track & Trace
OutputDir=..\dist
OutputBaseFilename=PharmaTnT-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\dist\PharmaTnT\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Pharma Track & Trace"; Filename: "{app}\PharmaTnT.exe"
Name: "{group}\Change connection"; Filename: "{app}\PharmaTnT.exe"; Parameters: "--reset"
Name: "{autodesktop}\Pharma Track & Trace"; Filename: "{app}\PharmaTnT.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\PharmaTnT.exe"; Description: "Start Pharma Track & Trace"; Flags: nowait postinstall skipifsilent
