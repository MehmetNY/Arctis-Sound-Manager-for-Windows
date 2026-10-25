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
; the virtual-microphone driver (VB-CABLE) can only be installed by an administrator
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
english.InstallingCable=Installing the virtual microphone driver (VB-CABLE)...
turkish.InstallingCable=Sanal mikrofon sürücüsü (VB-CABLE) kuruluyor...
english.LaunchApp=Open {#AppName}
turkish.LaunchApp={#AppName} uygulamasını aç
english.RemoveCable=Also remove the VB-CABLE virtual microphone driver? (Other programs may use it.)
turkish.RemoveCable=VB-CABLE sanal mikrofon sürücüsü de kaldırılsın mı? (Başka programlar kullanıyor olabilir.)

[Tasks]
Name: "autostart"; Description: "{cm:AutostartTask}"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--show"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--show"; Tasks: desktopicon

[Run]
; the virtual microphone: VB-Audio's own silent installation (-i -h), only when it is not there yet
Filename: "{app}\vbcable\VBCABLE_Setup_x64.exe"; Parameters: "-i -h"; WorkingDir: "{app}\vbcable"; StatusMsg: "{cm:InstallingCable}"; Flags: waituntilterminated runhidden; Check: CableMissing
; Start with Windows, for the person who is installing (not for the administrator account that may have been used)
Filename: "{app}\{#AppExe}"; Parameters: "--enable-autostart"; Flags: runasoriginaluser runhidden waituntilterminated; Tasks: autostart
Filename: "{app}\{#AppExe}"; Parameters: "--show"; Description: "{cm:LaunchApp}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{app}\{#AppExe}"; Parameters: "--quit-all"; Flags: runhidden waituntilterminated; RunOnceId: "QuitAll"
Filename: "{app}\{#AppExe}"; Parameters: "--disable-autostart"; Flags: runhidden waituntilterminated; RunOnceId: "NoAutostart"
Filename: "{app}\asm-audiotap.exe"; Parameters: "--restore"; Flags: runhidden waituntilterminated; RunOnceId: "RestoreVolumes"

[Code]
function CableMissing(): Boolean;
begin
  { the driver files VB-CABLE puts in System32\drivers (names differ between pack versions) }
  Result := not (FileExists(ExpandConstant('{sys}\drivers\vbaudio_cable64_win10.sys')) or
                 FileExists(ExpandConstant('{sys}\drivers\vbaudio_cable64_win7.sys')) or
                 FileExists(ExpandConstant('{sys}\drivers\vbaudio_cable64.sys')));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  { an earlier version may be running: let it put the programs' volumes back and go away }
  if FileExists(ExpandConstant('{app}\{#AppExe}')) then
    Exec(ExpandConstant('{app}\{#AppExe}'), '--quit-all', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Code: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    if (not UninstallSilent) and (MsgBox(CustomMessage('RemoveCable'), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then
      Exec(ExpandConstant('{app}\vbcable\VBCABLE_Setup_x64.exe'), '-u -h', ExpandConstant('{app}\vbcable'), SW_HIDE, ewWaitUntilTerminated, Code);
  end;
end;
