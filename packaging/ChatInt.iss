; Inno Setup 6 Script for ChatInt (User-level installer for WinGet & GitHub Releases)
; Builds: dist\installer\ChatInt-Setup-x64.exe

#define MyAppName "ChatInt"
#define MyAppVersion "0.5.1"
#define MyAppPublisher "Rishav Sinha"
#define MyAppURL "https://github.com/RishavSinha-2/ChatINT"
#define MyAppExeName "ChatInt.exe"

[Setup]
AppId={{8A8707C9-09A1-4D54-8AE4-20C0617CE93F}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={localappdata}\Programs\ChatInt
DisableDirPage=yes
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\installer
OutputBaseFilename=ChatInt-Setup-x64
SetupIconFile=..\src\chatint\ui\assets\logo.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\v{#MyAppVersion}\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
  PromptMsg: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // During silent uninstalls (e.g. WinGet upgrade/reinstall), NEVER touch user data in %APPDATA%\ChatInt
    if not WizardSilent() then
    begin
      DataDir := ExpandConstant('{userappdata}\ChatInt');
      if DirExists(DataDir) then
      begin
        PromptMsg := 'Do you want to delete your ChatInt chat history, configuration, and databases?' + #13#10 + #13#10 +
                     'Location: ' + DataDir + #13#10 + #13#10 +
                     'Click Yes to delete all user data, or No to keep it for future reinstalls.';
        if MsgBox(PromptMsg, mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        begin
          DelTree(DataDir, True, True, True);
        end;
      end;
    end;
  end;
end;
