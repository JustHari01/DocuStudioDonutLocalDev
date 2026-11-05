#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\release\DocuRandomStudio"
#endif
#ifndef BuildOutputDir
  #define BuildOutputDir "..\release"
#endif
#ifndef RuntimeFolder
  #define RuntimeFolder "DocuRandomStudio"
#endif

[Setup]
AppId={{D2BBA786-E5AE-4BD8-89AA-22150F4D6AF2}
AppName=DocuRandom Studio
AppVersion={#AppVersion}
AppPublisher=DocuRandom Studio
DefaultDirName={code:GetDefaultBaseDir}
DisableStartupPrompt=yes
DisableWelcomePage=yes
DisableDirPage=no
DisableProgramGroupPage=yes
DisableReadyPage=yes
DisableFinishedPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#BuildOutputDir}
OutputBaseFilename=DocuRandomStudio
SetupIconFile=..\assets\docurandom.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
Uninstallable=no
CreateUninstallRegKey=no
UsePreviousAppDir=no
AllowCancelDuringInstall=no
VersionInfoVersion={#AppVersion}
VersionInfoProductName=DocuRandom Studio
VersionInfoDescription=DocuRandom Studio - mở và chạy trực tiếp
VersionInfoCompany=DocuRandom Studio

[Messages]
SetupWindowTitle=DocuRandom Studio {#AppVersion}
InstallingLabel=Đang đặt toàn bộ DocuRandom Studio vào thư mục bạn đã chọn…

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}\App"; Flags: ignoreversion recursesubdirs createallsubdirs; Check: ShouldDeployRuntime

[InstallDelete]
Type: filesandordirs; Name: "{app}\Data\updates\{#AppVersion}"; Check: ShouldDeployRuntime

[Dirs]
Name: "{app}\Data"
Name: "{app}\Data\Temp"
Name: "{app}\Workspaces"

[Registry]
Root: HKCU; Subkey: "Software\DocuRandomStudio"; ValueType: string; ValueName: "Home"; ValueData: "{app}"

[Icons]
Name: "{userdesktop}\DocuRandom Studio"; Filename: "{app}\App\DocuRandomStudio.exe"; WorkingDir: "{app}\App"; IconFilename: "{app}\App\DocuRandomStudio.exe"
Name: "{userprograms}\DocuRandom Studio"; Filename: "{app}\App\DocuRandomStudio.exe"; WorkingDir: "{app}\App"; IconFilename: "{app}\App\DocuRandomStudio.exe"

[Run]
Filename: "{app}\App\DocuRandomUpdater.exe"; Parameters: "--cleanup-installer ""{srcexe}"" --install-dir ""{app}\App"""; WorkingDir: "{app}\App"; Flags: runhidden nowait
Filename: "{app}\App\DocuRandomStudio.exe"; WorkingDir: "{app}\App"; Flags: nowait skipifsilent

[Code]
const
  EmbeddedVersion = '{#AppVersion}';
  VersionMarkerName = '.docurandom-version';

function SetTimer(
  WindowHandle: LongWord;
  EventID: LongWord;
  Interval: LongWord;
  TimerFunction: LongWord
): LongWord;
  external 'SetTimer@user32.dll stdcall';

function KillTimer(WindowHandle: LongWord; EventID: LongWord): Bool;
  external 'KillTimer@user32.dll stdcall';

var
  DeployRuntime: Boolean;
  AutoStartRequested: Boolean;
  AutoStartTimerID: LongWord;
  SavedHome: String;

function GetDefaultBaseDir(Param: String): String;
begin
  if SavedHome <> '' then
    Result := SavedHome
  else
    Result := ExpandConstant('{userdocs}\DocuRandom Studio');
end;

function ReadVersionPart(const Value: String; var Position: Integer): Integer;
begin
  Result := 0;
  while (Position <= Length(Value)) and (Value[Position] <> '.') do
  begin
    if (Value[Position] >= '0') and (Value[Position] <= '9') then
      Result := (Result * 10) + (Ord(Value[Position]) - Ord('0'));
    Position := Position + 1;
  end;
  if (Position <= Length(Value)) and (Value[Position] = '.') then
    Position := Position + 1;
end;

function CompareSemanticVersions(const LeftValue, RightValue: String): Integer;
var
  Index: Integer;
  LeftPosition, RightPosition: Integer;
  LeftPart, RightPart: Integer;
begin
  LeftPosition := 1;
  RightPosition := 1;
  for Index := 1 to 3 do
  begin
    LeftPart := ReadVersionPart(LeftValue, LeftPosition);
    RightPart := ReadVersionPart(RightValue, RightPosition);
    if LeftPart < RightPart then
    begin
      Result := -1;
      exit;
    end;
    if LeftPart > RightPart then
    begin
      Result := 1;
      exit;
    end;
  end;
  Result := 0;
end;

function ShouldDeployRuntime(): Boolean;
begin
  Result := DeployRuntime;
end;

function InitializeSetup(): Boolean;
var
  MarkerVersion: AnsiString;
  ExecutableVersion: String;
  ExecutablePath, MarkerPath: String;
  MarkerReady, ExecutableReady: Boolean;
begin
  SavedHome := '';
  RegQueryStringValue(HKCU, 'Software\DocuRandomStudio', 'Home', SavedHome);
  if SavedHome <> '' then
  begin
    ExecutablePath := AddBackslash(SavedHome) + 'App\DocuRandomStudio.exe';
    MarkerPath := AddBackslash(SavedHome) + 'App\' + VersionMarkerName;
  end
  else
  begin
    ExecutablePath := '';
    MarkerPath := '';
  end;
  DeployRuntime := True;
  AutoStartRequested := False;
  if (ExecutablePath <> '') and FileExists(ExecutablePath) then
  begin
    MarkerReady := LoadStringFromFile(MarkerPath, MarkerVersion);
    ExecutableReady := GetVersionNumbersString(ExecutablePath, ExecutableVersion);
    if MarkerReady and ExecutableReady then
    begin
      { Never let an older launcher downgrade a newer managed App.  Otherwise
        trust neither the marker nor the file alone: a failed update can leave
        them disagreeing, and that exact mismatch previously caused a loop. }
      if (CompareSemanticVersions(Trim(String(MarkerVersion)), EmbeddedVersion) > 0) or
         (CompareSemanticVersions(ExecutableVersion, EmbeddedVersion) > 0) then
        DeployRuntime := False
      else
        DeployRuntime :=
          (CompareSemanticVersions(Trim(String(MarkerVersion)), EmbeddedVersion) <> 0) or
          (CompareSemanticVersions(ExecutableVersion, EmbeddedVersion) <> 0);
    end;
  end;
  Result := True;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if (PageID = wpSelectDir) and (SavedHome <> '') and
     FileExists(AddBackslash(SavedHome) + 'App\DocuRandomStudio.exe') then
    Result := True;
end;

function StopProcessImage(const ImageName: String): Boolean;
var
  ResultCode: Integer;
begin
  { Restart Manager normally closes the app, but Electron's hidden Chat Bridge
    can refuse the graceful request and keep the install directory locked.
    These image names are unique to this product; stop their complete process
    trees before Inno starts replacing the application folder.  A missing
    process is fine. }
  Result := Exec(
    ExpandConstant('{sys}\taskkill.exe'),
    '/F /T /IM "' + ImageName + '"',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  );
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  NeedsRestart := False;
  { The installer itself is also named DocuRandomStudio.exe, so never issue a
    global image-name kill for that executable.  Restart Manager handles the
    main app and the updater path-bounds any forced cleanup. }
  StopProcessImage('DocuRandomUpdater.exe');
  StopProcessImage('DocuRandom Chat Bridge.exe');
  Sleep(500);
  Result := '';
end;

procedure AutoStartTimerProc(
  WindowHandle: LongWord;
  MessageID: LongWord;
  EventID: LongWord;
  TickCount: LongWord
);
begin
  if (WizardForm.CurPageID = wpReady) and
     WizardForm.NextButton.Enabled and
     not AutoStartRequested then
  begin
    AutoStartRequested := True;
    KillTimer(0, AutoStartTimerID);
    WizardForm.NextButton.OnClick(WizardForm.NextButton);
  end;
end;

procedure InitializeWizard();
begin
  WizardForm.SelectDirLabel.Caption :=
    'Chọn một thư mục duy nhất để lưu ứng dụng, workspace, VOICEVOX, cache, cập nhật và tệp tạm.';
  { Let the wizard finish creating its controls, then advance the retained
    Ready page from the UI message loop. This avoids a visible install step
    while preserving the useful extraction progress bar on first launch. }
  AutoStartTimerID := SetTimer(0, 0, 75, CreateCallback(@AutoStartTimerProc));
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if DeployRuntime then
    begin
      if not SaveStringToFile(
        ExpandConstant('{app}\App\' + VersionMarkerName),
        EmbeddedVersion,
        False
      ) then
        RaiseException('Không thể lưu phiên bản runtime DocuRandom Studio.');
    end;
    SaveStringToFile(
      ExpandConstant('{app}\.docurandom-home'),
      'DocuRandom Studio managed folder',
      False
    );
    SaveStringToFile(
      ExpandConstant('{app}\.docurandom-installed-version'),
      EmbeddedVersion,
      False
    );
  end;
end;
