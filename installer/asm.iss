; Inno Setup script for Arctis Sound Manager (Windows port). Built by installer\build.ps1:
;   iscc /DAppVersion=1.6.4 /DSourceDir=<PyInstaller folder> /DOutDir=<where> installer\asm.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\build\dist\ArctisSoundManager"
#endif
#ifndef OutDir
  #define OutDir "..\build"
#endif
#define AppName "Arctis Sound Manager"
#define AppExe "ArctisSoundManager.exe"

[Setup]
AppId={{8F3B2A52-6C1E-4E43-9B6A-7B4F0C5E21A9}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Arctis Sound Manager (Windows port)
AppPublisherURL=https://github.com/MehmetNY/Arctis-Sound-Manager-for-Windows
AppSupportURL=https://github.com/MehmetNY/Arctis-Sound-Manager-for-Windows/issues
DefaultDirName={autopf}\Arctis Sound Manager
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
; an administrator registers the microphone effect with Windows' audio engine and creates the scheduled tasks that let
; the program keep it attached later without asking again
OutputDir={#OutDir}
OutputBaseFilename=ASM-Setup-{#AppVersion}
SetupIconFile=asm.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
InfoBeforeFile=THIRD-PARTY-NOTICES.txt
UsePreviousTasks=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"

[CustomMessages]
english.AutostartTask=Start {#AppName} with Windows (quietly, in the background)
turkish.AutostartTask={#AppName} Windows ile birlikte (sessizce, arka planda) başlasın
english.SettingUpMicrophone=Setting up the microphone effect...
turkish.SettingUpMicrophone=Mikrofon efekti hazırlanıyor...
english.RemovingOldCable=Removing the virtual cable an earlier version installed...
turkish.RemovingOldCable=Önceki sürümün kurduğu sanal kablo kaldırılıyor...
english.LaunchApp=Open {#AppName}
turkish.LaunchApp={#AppName} uygulamasını aç

[Tasks]
Name: "autostart"; Description: "{cm:AutostartTask}"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--show"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--show"; Tasks: desktopicon

[Run]
; Everything below runs elevated (the installer is). Nothing is downloaded and no third-party driver is installed.
; 1. an earlier version of this program installed VB-CABLE (a virtual audio cable); the sound panel should show the headset only.
;    Only that one is removed (the folder it left here proves it was ours); /KEEPCABLE=1 keeps it.
Filename: "{app}\{#AppExe}"; Parameters: "--fx remove-old-cable"; StatusMsg: "{cm:RemovingOldCable}"; Flags: waituntilterminated runhidden; Check: RemoveOldCable
; 2. the three scheduled tasks through which the program (running as the person) asks for the following steps again later
Filename: "{app}\{#AppExe}"; Parameters: "--fx tasks-install"; StatusMsg: "{cm:SettingUpMicrophone}"; Flags: waituntilterminated runhidden
; 3. register the microphone effect and attach it to the headset's microphone (Windows' audio service restarts once)
Filename: "{app}\{#AppExe}"; Parameters: "--fx attach"; StatusMsg: "{cm:SettingUpMicrophone}"; Flags: waituntilterminated runhidden
; the audio service must be running in the end, whatever the steps above found (it is a no-op when it is)
Filename: "{sys}\net.exe"; Parameters: "start audiosrv"; Flags: waituntilterminated runhidden
; Start with Windows, for the person who is installing (not for the administrator account that may have been used)
Filename: "{app}\{#AppExe}"; Parameters: "--enable-autostart"; Flags: runasoriginaluser runhidden waituntilterminated; Tasks: autostart
Filename: "{app}\{#AppExe}"; Parameters: "--show"; Description: "{cm:LaunchApp}"; Flags: nowait postinstall skipifsilent runasoriginaluser
; an update started from inside the program (/RELAUNCH=1): open it again, for the person who was using it
Filename: "{app}\{#AppExe}"; Parameters: "--show"; Flags: nowait runasoriginaluser; Check: Relaunch

[UninstallRun]
Filename: "{app}\{#AppExe}"; Parameters: "--quit-all"; Flags: runhidden waituntilterminated; RunOnceId: "QuitAll"
; Windows goes back to how it was: the microphone without the effect, the audio protection setting as it was, the effect unregistered
Filename: "{app}\{#AppExe}"; Parameters: "--fx detach"; Flags: runhidden waituntilterminated; RunOnceId: "DetachMicrophoneEffect"
Filename: "{app}\{#AppExe}"; Parameters: "--fx tasks-remove"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveTasks"
Filename: "{app}\{#AppExe}"; Parameters: "--disable-autostart"; Flags: runhidden waituntilterminated; RunOnceId: "NoAutostart"
Filename: "{app}\asm-audiotap.exe"; Parameters: "--restore"; Flags: runhidden waituntilterminated; RunOnceId: "RestoreVolumes"
Filename: "{sys}\net.exe"; Parameters: "start audiosrv"; Flags: runhidden waituntilterminated; RunOnceId: "AudioServiceUp"

[UninstallDelete]
Type: filesandordirs; Name: "{commonappdata}\ArctisSoundManager"

[Code]
function Relaunch(): Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;

function RemoveOldCable(): Boolean;
begin
  { only the cable that an earlier version of this installer brought (its pack folder is still here) }
  Result := DirExists(ExpandConstant('{app}\vbcable')) and (ExpandConstant('{param:KEEPCABLE|0}') <> '1');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  { an earlier version may be running: let it put the programs' volumes back and go away }
  if FileExists(ExpandConstant('{app}\{#AppExe}')) then
    Exec(ExpandConstant('{app}\{#AppExe}'), '--quit-all', '', SW_HIDE, ewWaitUntilTerminated, Code);
  { and its microphone effect must leave Windows' audio engine (which has the DLL open) before the new files can replace it;
    the installation attaches the new one in a moment }
  if FileExists(ExpandConstant('{app}\fx\asm_micfx.dll')) then
    Exec(ExpandConstant('{app}\{#AppExe}'), '--fx detach', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;
