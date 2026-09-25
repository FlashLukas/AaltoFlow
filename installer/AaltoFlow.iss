; AaltoFlow.iss -- the Setup.exe for the AaltoFlow suite (Inno Setup 6).
;
; Do not compile this by hand: run  installer\build_installer.ps1 , which
;   * stages a CLEAN copy of the committed code into installer\build\stage\,
;   * runs gen_components.py to turn the modules' module.toml manifests into
;     build\components.iss + build\components_code.iss (included below),
;   * passes the version on the command line.
; So the checkbox list is the SAME list the launcher discovers: adding a module
; to the suite is still just dropping in a folder with a module.toml.
;
; What Setup does:
;   1. Wizard: pick a folder, tick the modules you want, and (optionally) name
;      the SETUP this PC drives -- "TR-MOKE" -- which then leads every window
;      title and the shortcut names (set_setup_name.ps1 -> suite_local.json).
;   2. Copies those module folders, always-present suite-common + mission-control,
;      and a private uv.exe.
;   3. Runs postinstall.ps1 -> `uv sync` per module -> one .venv each (internet).
;   4. Start-menu entries: Mission Control, Measurement Suite, Rebuild envs.
;
; Per-user install (no admin rights): the lab PC account may not be an admin,
; and the suite writes .ini, calibration and suite_local.json files next to the
; code, which Program Files would forbid.

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#ifndef StageDir
  #define StageDir "build\stage"
#endif

[Setup]
; AppId identifies the product for upgrades/uninstall. NEVER change it, or a
; new Setup installs side by side instead of upgrading. It was kept through the
; 2026-09-24 rename TRMOKE -> AaltoFlow on purpose: this Setup UPGRADES a TRMOKE
; install in place, so its data and calibrations stay where they are (an
; existing install also keeps its folder name, UsePreviousAppDir below).
AppId={{6F1C2B7E-4E0B-4C57-9B1A-7D3E2A9C5F11}
AppName=AaltoFlow
AppVersion={#AppVersion}
AppVerName=AaltoFlow {#AppVersion}
AppPublisher=Aalto University - NanoSpin group
AppPublisherURL=https://github.com/FlashLukas/AaltoFlow
DefaultDirName={localappdata}\Programs\AaltoFlow
DefaultGroupName=AaltoFlow
DisableProgramGroupPage=yes
; not the previous group: an upgraded TRMOKE install would keep a "TRMOKE" menu
UsePreviousGroup=no
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=dist
OutputBaseFilename=AaltoFlow-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; The launcher's own icon, rendered from mission-control\icon.svg by
; make_icons.py -- so Setup, the Apps list and the shortcuts all match
; the card the launcher draws.
SetupIconFile=build\icons\mission-control.ico
UninstallDisplayIcon={app}\mission-control\icon.ico
LicenseFile={#StageDir}\LICENSE
; Re-run on the same folder to ADD modules ("Modify"): Setup pre-ticks what is
; already installed.
UsePreviousAppDir=yes
UsePreviousSetupType=yes
UninstallDisplayName=AaltoFlow

[Types]
Name: "full";   Description: "Full suite (every module + measurement suite)"
Name: "custom"; Description: "Custom -- choose modules"; Flags: iscustom

; [Components], [Files] and [UninstallDelete] for every module, generated from
; the module.toml manifests.
#include "build\components.iss"

[Tasks]
Name: "desktopicon"; Description: "Desktop shortcut for Mission Control"; GroupDescription: "Shortcuts:"; Flags: unchecked
Name: "buildenvs";   Description: "Build the Python environments now (needs internet; a few minutes)"; GroupDescription: "After copying:"

[InstallDelete]
; Shortcuts from before the rename, and ones named after a PREVIOUS setup name:
; the group folder is ours, so every link in it is rewritten below anyway.
Type: filesandordirs; Name: "{userprograms}\TRMOKE"
Type: files; Name: "{autodesktop}\TRMOKE Mission Control.lnk"
Type: files; Name: "{autodesktop}\{code:PreviousLabel} Mission Control.lnk"
Type: files; Name: "{group}\*.lnk"

[Files]
; Root docs + the private uv. uv is NOT put on PATH; postinstall.ps1 and the
; launcher look for it inside the installation.
Source: "{#StageDir}\README.md";                   DestDir: "{app}"; Flags: ignoreversion
Source: "{#StageDir}\LICENSE";                     DestDir: "{app}"; Flags: ignoreversion
Source: "{#StageDir}\INSTRUMENT_MODULE_GUIDE.md";  DestDir: "{app}"; Flags: ignoreversion
Source: "{#StageDir}\uv\*";                        DestDir: "{app}\uv"; Flags: ignoreversion
Source: "postinstall.ps1";                         DestDir: "{app}\installer"; Flags: ignoreversion
Source: "run_envs_task.ps1";                       DestDir: "{app}\installer"; Flags: ignoreversion
Source: "set_setup_name.ps1";                      DestDir: "{app}\installer"; Flags: ignoreversion
Source: "build\icons\mission-control.ico"; DestDir: "{app}\mission-control"; DestName: "icon.ico"; Components: core; Flags: ignoreversion
Source: "build\icons\scan-core.ico";       DestDir: "{app}\scan-core";       DestName: "icon.ico"; Components: scan; Flags: ignoreversion

[Icons]
; pythonw = no console window behind the GUI. The .venv is built by
; postinstall.ps1; until that has run these shortcuts have nothing to start.
;
; AppUserModelID must MATCH what the application claims at startup
; (apps/theme.py, apply_window_icon -> "Aalto.AaltoFlow.<folder>"). The taskbar
; keys a button on that string, and without a shortcut carrying the same one
; Windows has no icon registered for it -- which is how the measurement suite
; came to show the blank window icon on the lab PC on 2026-09-23 while its own
; window carried the right one. With the shortcut declaring it, the ID has an
; authoritative .ico and the running window can also be pinned.
; {code:ShortcutLabel} = the setup name ("TR-MOKE Mission Control"), or AaltoFlow.
Name: "{group}\{code:ShortcutLabel} Mission Control"; Filename: "{app}\mission-control\.venv\Scripts\pythonw.exe"; Parameters: "mission_control.py"; WorkingDir: "{app}\mission-control"; IconFilename: "{app}\mission-control\icon.ico"; AppUserModelID: "Aalto.AaltoFlow.mission-control"; Comment: "Start/stop module services and open their GUIs"
Name: "{group}\{code:ShortcutLabel} Measurement Suite"; Filename: "{app}\scan-core\.venv\Scripts\pythonw.exe"; Parameters: "apps\suite.py"; WorkingDir: "{app}\scan-core"; IconFilename: "{app}\scan-core\icon.ico"; AppUserModelID: "Aalto.AaltoFlow.scan-core"; Components: scan; Comment: "Control, scan and measure across the running modules"
Name: "{group}\Rebuild Python environments"; Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\installer\postinstall.ps1"" -Root ""{app}"""; WorkingDir: "{app}"; IconFilename: "{app}\mission-control\icon.ico"; Comment: "Re-run uv sync for every installed module (e.g. after installing with no internet)"
Name: "{group}\{code:ShortcutLabel} folder"; Filename: "{app}"
Name: "{group}\Uninstall AaltoFlow"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{code:ShortcutLabel} Mission Control"; Filename: "{app}\mission-control\.venv\Scripts\pythonw.exe"; Parameters: "mission_control.py"; WorkingDir: "{app}\mission-control"; IconFilename: "{app}\mission-control\icon.ico"; AppUserModelID: "Aalto.AaltoFlow.mission-control"; Tasks: desktopicon

[Run]
; FIRST: the setup name, into suite_local.json (merged, see the script). Plain
; PowerShell, no uv, so Setup may run it directly.
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\installer\set_setup_name.ps1"" -Root ""{app}""{code:SetupNameArg}"; StatusMsg: "Saving the setup name..."; Flags: runhidden waituntilterminated
; NOT postinstall.ps1 directly. A process Setup spawns may not read a junction
; ("untrusted mount point", os error 448), and uv enumerates its managed
; interpreter directory -- one junction per Python version -- before it uses
; anything, so every `uv sync` died here while the identical Start-menu entry
; below worked. run_envs_task.ps1 hands the work to a one-shot scheduled task,
; which the Task Scheduler starts rather than Setup, and follows its log in this
; window. See the header of that script.
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\installer\run_envs_task.ps1"" -Root ""{app}"" -Projects ""{code:SelectedProjects}""{code:PauseFlag}"; WorkingDir: "{app}"; StatusMsg: "Building Python environments (uv sync) -- see the console window..."; Tasks: buildenvs; Flags: waituntilterminated
; Launched through explorer.exe, NOT directly, and for the same reason as the
; line above. A .venv's pythonw.exe is a uv TRAMPOLINE: it spawns the real
; interpreter out of the managed directory, across the cpython-3.14 junction.
; Started by Setup that spawn is refused --
;   uv trampoline failed to spawn Python child process
;     Caused by: uncategorized error (os error 448)
; -- while the identical Start-menu shortcut works. Handing the shortcut to
; explorer.exe makes the already-running Explorer start it, so it carries
; Explorer's token and not Setup's. explorer.exe returns immediately, hence no
; "wait" flag is meaningful here.
Filename: "{sys}\explorer.exe"; Parameters: """{group}\{code:ShortcutLabel} Mission Control.lnk"""; Description: "Start Mission Control"; Flags: postinstall nowait skipifsilent; Check: FileExists(ExpandConstant('{app}\mission-control\.venv\Scripts\pythonw.exe'))

[UninstallDelete]
; An interpreter that postinstall.ps1 had uv install inside the suite --
; only done on a PC that had no managed Python of its own. The generated
; section above covers each module's .venv.
Type: filesandordirs; Name: "{app}\python"
; Packages that "Add module..." copied in from module packs (offline builds).
Type: filesandordirs; Name: "{app}\wheelhouse"

[UninstallRun]
; `uv sync` leaves build products next to the source: __pycache__, *.egg-info,
; .pytest_cache. Setup never installed them, so Inno would not remove them and
; the folder would survive as a shell of caches. [UninstallRun] runs before the
; files are deleted, so sweep them here and leave only real lab data behind.
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""Get-ChildItem -LiteralPath '{app}' -Recurse -Force -Directory -ErrorAction SilentlyContinue | Where-Object {{ $_.Name -eq '__pycache__' -or $_.Name -eq '.pytest_cache' -or $_.Name -like '*.egg-info' } | ForEach-Object {{ Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }"""; RunOnceId: "sweepcaches"; Flags: runhidden waituntilterminated

[Code]
{ SelectedProjects(): the folders to `uv sync`, from the ticked components. }
#include "build\components_code.iss"

{ ---- the setup name ------------------------------------------------------
  One optional text box after the module list. Remembered for the next run
  (Inno's "previous data"), overridable on the command line for a silent
  install:  /SETUPNAME="TR-MOKE" . }
var
  SetupPage: TInputQueryWizardPage;

procedure InitializeWizard;
begin
  SetupPage := CreateInputQueryPage(wpSelectComponents,
    'Setup name',
    'Which setup does this installation drive?',
    'Optional. The name leads every window title and the Start-menu shortcuts, ' +
    'e.g. "TR-MOKE ' + #183 + ' Mission Control", so on a lab with several rigs ' +
    'you can see at once which one a window belongs to.' + #13#10#13#10 +
    'Leave it empty to show just "AaltoFlow". Re-run Setup to change it.');
  SetupPage.Add('Setup name:', False);
  SetupPage.Values[0] := ExpandConstant('{param:SETUPNAME|' +
                                        GetPreviousData('SetupName', '') + '}');
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'SetupName', SetupPage.Values[0]);
end;

{ Strip what a file name or a command line cannot carry. }
function CleanName(Name: String): String;
var
  I: Integer;
begin
  Result := Trim(Name);
  for I := Length(Result) downto 1 do
    if Pos(Result[I], '\/:*?"<>|') > 0 then Delete(Result, I, 1);
end;

function SetupName: String;
begin
  Result := CleanName(SetupPage.Values[0]);
end;

{ For shortcut names: the setup, or the product. }
function ShortcutLabel(Param: String): String;
begin
  Result := SetupName;
  if Result = '' then Result := 'AaltoFlow';
end;

{ The label the LAST install used, so its desktop shortcut can be removed when
  the name changes (the Start-menu group is cleared wholesale). }
function PreviousLabel(Param: String): String;
begin
  Result := CleanName(GetPreviousData('SetupName', ''));
  if Result = '' then Result := 'AaltoFlow';
end;

{ -Name "..." -- or nothing at all: powershell.exe drops an empty -Name "" and
  then complains that the parameter has no argument. }
function SetupNameArg(Param: String): String;
begin
  if SetupName = '' then Result := ''
  else Result := ' -Name "' + SetupName + '"';
end;

{ A silent install has no one to press Enter, so postinstall.ps1 must not wait. }
function PauseFlag(Param: String): String;
begin
  if WizardSilent then Result := ' -NoPause' else Result := '';
end;

function UnderFolder(Path, Folder: String): Boolean;
begin
  Result := (Folder <> '') and
    (Pos(Lowercase(AddBackslash(Folder)), Lowercase(AddBackslash(Path))) = 1);
end;

{ Why a folder can be refused. Returns '' when it is fine.
  Used by BOTH the wizard and PrepareToInstall, because a silent install shows
  no pages at all and would otherwise skip the checks. }
function DirProblem(Dir: String): String;
begin
  Result := '';
  { OneDrive locks files inside .venv and `uv sync` then fails with
    "Access is denied" (docs/DEVELOPER_NOTES.md gotcha #8). Refuse now rather than
    fail halfway through. }
  if UnderFolder(Dir, GetEnv('OneDrive')) or
     UnderFolder(Dir, GetEnv('OneDriveCommercial')) or
     UnderFolder(Dir, GetEnv('OneDriveConsumer')) then
    Result := 'This folder is inside OneDrive.' + #13#10#13#10 +
              'OneDrive locks the files of the Python environments and the ' +
              'install would fail with "Access is denied". Please choose a ' +
              'folder on the local disk, e.g. the default.'
  { PySide6 has very deep paths; a long install path can push files past the
    260-character limit and break the environment build. }
  else if Length(Dir) > 80 then
    Result := 'That path is very long (' + IntToStr(Length(Dir)) + ' characters).' + #13#10#13#10 +
              'The Python packages have deeply nested files and can exceed ' +
              'Windows'' 260-character path limit. Please choose a shorter ' +
              'folder, e.g. the default, or C:\Users\<you>\AaltoFlow.';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Problem: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    Problem := DirProblem(WizardDirValue);
    if Problem <> '' then
    begin
      MsgBox(Problem, mbError, MB_OK);
      Result := False;
    end;
  end;
end;

{ Runs in silent installs too: returning a non-empty string aborts with it. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := DirProblem(ExpandConstant('{app}'));
end;

{ Deselecting a module on a later "Modify" run does NOT delete it: its folder
  may hold calibrations and settings. Say so instead of silently keeping it. }
procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpSelectComponents) and (WizardForm.PrevAppDir <> '') then
    WizardForm.ComponentsDiskSpaceLabel.Caption :=
      'Tick modules to ADD them. Unticking does not remove an installed module ' +
      '(it may hold calibrations) -- delete its folder by hand if you really want it gone.';
end;
