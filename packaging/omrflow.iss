; Inno Setup script for the OMRFlow Windows installer.
;
; Compiled by scripts/release/Build-Installer.ps1, which passes the version
; in with /D so it is never written down here. Compiling this file directly
; requires the same definitions:
;
;   ISCC.exe packaging\omrflow.iss /DAppVersion=0.1.0-alpha.1 /DSourceDir=..\dist\OMRFlow
;
; Design notes, each of which is a requirement rather than a preference:
;
;   * Per-user install by default (PrivilegesRequiredOverridesAllowed).
;     OMRFlow needs no machine-wide state, and an examination office
;     operator often cannot elevate. An administrator may still choose an
;     all-users install.
;
;   * The application directory holds the application only. Projects,
;     configuration and logs live under the user's profile, so an upgrade or
;     an uninstall cannot take examination data with it - see the
;     UninstallDelete section, which deliberately removes nothing outside
;     the install directory.
;
;   * Unsigned. Alpha builds have no code-signing certificate, so Windows
;     SmartScreen will warn. Documented in docs/wiki/Installation.md rather
;     than hidden; signing is a Phase 11C hardening item.

#ifndef AppVersion
  #error AppVersion must be passed in with /DAppVersion=...
#endif
#ifndef AppNumericVersion
  #error AppNumericVersion must be passed in with /DAppNumericVersion=... (four integers)
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\OMRFlow"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist\installer"
#endif

#define AppName        "OMRFlow"
#define AppPublisher   "Dr. Sajid Muhaimin Choudhury"
#define AppURL         "https://github.com/sajidbuet/OMRFlow"
#define AppExeName     "OMRFlow.exe"

[Setup]
; A stable GUID identifies the application across versions: it is what lets
; an upgrade replace the previous install instead of adding a second copy.
; Never change it for a new version.
AppId={{6F3A2C41-9B7D-4E58-8C2A-0D5E1B9F47A3}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
; Four integers, because Inno Setup rejects a prerelease suffix here. The
; readable version is carried by AppVersion above and by VersionInfoTextVersion
; below; this one exists so Windows can compare builds.
VersionInfoVersion={#AppNumericVersion}
VersionInfoTextVersion={#AppVersion}
VersionInfoProductVersion={#AppNumericVersion}
VersionInfoProductTextVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir={#OutputDir}
OutputBaseFilename={#AppName}-{#AppVersion}-Setup-x64
SetupIconFile=..\src\omr_scanner\gui\resources\branding\icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName} {#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 64-bit only: the bundled Python, Qt and OpenCV are all x64.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Windows 10 1809 is the floor the bundled Qt 6 runtime supports.
MinVersion=10.0.17763
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Only what the installer itself created inside the application directory.
; Nothing here refers to the user's projects, configuration or logs: an
; uninstall must never remove examination data, and there is no "also delete
; my data" option precisely because it is too easy to click by accident.
Type: filesandordirs; Name: "{app}\_internal"

[Messages]
; Stated during installation, not only in the documentation, because this is
; where someone is deciding whether to trust the build.
WelcomeLabel2=This will install [name/ver] on your computer.%n%nThis is an ALPHA release for evaluation and testing. Real examination-data qualification is still in progress - independently verify any generated results before operational use.%n%nYour projects, settings and logs are stored in your user profile and are not affected by installing, upgrading or removing OMRFlow.
