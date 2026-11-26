; Astro Rig Monitor for Windows: the installer (Inno Setup 6). Built by windows/build.ps1, which
; passes /DAppVersion=... /DStage=<folder with python\, app\, icon.ico, VERSION, BUILD>.
;
; Installs for the current user only (no admin rights), into %LOCALAPPDATA%\Programs. The tray
; icon installs and starts the scheduled task that runs the monitor; uninstalling removes both and
; keeps the settings and recordings in %LOCALAPPDATA%\Astro Rig Monitor.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef Stage
  #define Stage "build\stage"
#endif

[Setup]
AppId={{BF215F6B-58F0-4DFB-836B-4767BD2EAA9D}
AppName=Astro Rig Monitor
AppVersion={#AppVersion}
AppVerName=Astro Rig Monitor {#AppVersion}
AppPublisher=Astro Rig Monitor
AppPublisherURL=https://github.com/dgsgc/astro-rig-monitor
AppSupportURL=https://github.com/dgsgc/astro-rig-monitor/issues
AppUpdatesURL=https://github.com/dgsgc/astro-rig-monitor/releases
DefaultDirName={localappdata}\Programs\Astro Rig Monitor
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=Astro-Rig-Monitor-Setup
SetupIconFile={#Stage}\icon.ico
UninstallDisplayIcon={app}\icon.ico
UninstallDisplayName=Astro Rig Monitor
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
RestartApplications=no

[Files]
Source: "{#Stage}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion createallsubdirs

[Icons]
Name: "{autoprograms}\Astro Rig Monitor"; Filename: "{app}\python\pythonw.exe"; Parameters: "-s -B -m armwin.tray"; WorkingDir: "{app}"; IconFilename: "{app}\icon.ico"; Comment: "Watches your guiding"

[Registry]
; The tray icon at login (the monitor itself runs from the scheduled task either way).
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Astro Rig Monitor"; ValueData: """{app}\python\pythonw.exe"" -s -B -m armwin.tray"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: "-s -B -m armwin.tray"; WorkingDir: "{app}"; Description: "Start Astro Rig Monitor"; Flags: postinstall nowait skipifsilent
; A silent update doesn't start the tray icon, so start the monitor's task again here (a monitor
; that was turned off has a disabled task, which this leaves alone; no task yet: nothing happens).
Filename: "{sys}\schtasks.exe"; Parameters: "/Run /TN ""Astro Rig Monitor"""; Flags: runhidden; Check: WizardSilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
// Stop the monitor's task and any of this app's Python processes, so their files can be replaced
// (update) or deleted (uninstall). Only processes running from this app's folder are stopped.
procedure StopApp(RemoveTask: Boolean);
var
  R: Integer;
  Dir: String;
begin
  Exec(ExpandConstant('{sys}\schtasks.exe'), '/End /TN "Astro Rig Monitor"', '', SW_HIDE, ewWaitUntilTerminated, R);
  if RemoveTask then
    Exec(ExpandConstant('{sys}\schtasks.exe'), '/Delete /TN "Astro Rig Monitor" /F', '', SW_HIDE, ewWaitUntilTerminated, R);
  Dir := ExpandConstant('{app}');
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -NonInteractive -Command "Get-Process python,pythonw -ErrorAction SilentlyContinue | ' +
    'Where-Object { $_.Path -and $_.Path.StartsWith(''' + Dir + '\'', [StringComparison]::OrdinalIgnoreCase) } | Stop-Process -Force"',
    '', SW_HIDE, ewWaitUntilTerminated, R);
  Sleep(500);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  if DirExists(ExpandConstant('{app}')) then
    StopApp(False);
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    StopApp(True);
end;
