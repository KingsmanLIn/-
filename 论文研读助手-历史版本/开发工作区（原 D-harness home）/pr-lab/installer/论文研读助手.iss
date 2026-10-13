; 论文研读助手 —— Windows 安装程序（Inno Setup 6）
;
; 由 tools/build_installer.py 调用，例如：
;   ISCC.exe "/DStageDir=D:\...\论文研读助手-本地版-1.3.8-完整便携版" /DAppVersion=1.3.8 ...
;
; 设计要点
;   · 按当前用户安装（PrivilegesRequired=lowest），因此 {autopf} 解析为 %LOCALAPPDATA%\Programs，
;     不需要管理员权限，而且安装目录**可写**——软件把自己的文献库放在 {app}\data，无需迁移。
;   · 目录选择页保留（DisableDirPage=no），可以装到任意目录（例如 D:\论文研读助手）。
;   · 升级安全：data\ 被排除在覆盖范围之外，library.json 用 onlyifdoesntexist 写入，
;     因此覆盖安装不会动到读者的文献库。
;   · 卸载默认**保留** data\；只有卸载时明确回答“是”才删除。
;   · {app}\icon.ico 供开始菜单/桌面快捷方式使用。

#ifndef StageDir
  #define StageDir "D:\harness home\dist\论文研读助手-本地版-1.3.8-完整便携版"
#endif
#ifndef AppVersion
  #define AppVersion "1.3.8"
#endif
#ifndef OutputDir
  #define OutputDir "D:\harness home\dist"
#endif
#define AppName "论文研读助手"
#define AppId "{{7C4E1B92-5D3A-4F1E-9E7B-2A6C8D51F0A1}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=论文研读助手（本地版）
DefaultDirName={autopf}\{#AppName}
DisableDirPage=no
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir={#OutputDir}
OutputBaseFilename=论文研读助手-{#AppVersion}-安装程序
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\icon.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
VersionInfoDescription={#AppName} 安装程序

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: checkedonce

[Files]
; 软件本体；data\、run\、缓存一律不覆盖（读者的文献库与运行状态必须留下）
Source: "{#StageDir}\*"; DestDir: "{app}"; \
  Excludes: "data\*,run\*,__pycache__\*,.git\*"; \
  Flags: ignoreversion recursesubdirs createallsubdirs
; 首次安装才写入的空文献库；卸载时也保留（除非卸载时选择删除）
Source: "{#StageDir}\data\library.json"; DestDir: "{app}\data"; \
  Flags: onlyifdoesntexist uninsneveruninstall
Source: "icon.ico"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
Name: "{app}\data"; Flags: uninsneveruninstall
Name: "{app}\data\papers"; Flags: uninsneveruninstall
Name: "{app}\data\analyses"; Flags: uninsneveruninstall
Name: "{app}\data\extracted"; Flags: uninsneveruninstall

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\启动论文研读助手.vbs"; IconFilename: "{app}\icon.ico"; Comment: "本地运行的论文阅读与 AI 参考分析工具"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\启动论文研读助手.vbs"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\启动论文研读助手.vbs"; Description: "立即启动 {#AppName}"; \
  Flags: nowait postinstall skipifsilent shellexec

[UninstallDelete]
Type: filesandordirs; Name: "{app}\run"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\static\__pycache__"

[Messages]
; 向导中文化（Inno 官方不含简体中文语言文件，这里覆盖关键文案）
WelcomeLabel1=欢迎安装 [name]
WelcomeLabel2=即将把 [name] [ver] 安装到你的电脑。%n%n这是一个完全在本机运行的工具：论文、阅读记录与 AI 答案都保存在安装目录的 data 文件夹里，只有你点击生成后才会把论文发送给你自己配置的模型服务。%n%n如果软件正在运行，请先退出（在软件里点“保存并退出软件”）。
SelectDirDesc=要把 [name] 安装到哪里？
SelectDirLabel3=安装程序会把 [name] 安装到下面的文件夹。
SelectDirBrowseLabel=点“浏览”选择其它文件夹；点“下一步”继续。
DiskSpaceGBLabel=至少需要 [gb] GB 可用空间。
SelectTasksDesc=还需要执行哪些附加任务？
SelectTasksLabel2=请选择安装 [name] 时要执行的附加任务，然后点“下一步”。
ReadyLabel1=安装程序已准备好开始安装 [name]。
ReadyLabel2a=点“安装”开始安装；如果想检查或修改设置，点“上一步”。
InstallingLabel=正在安装 [name]，请稍候…
FinishedHeadingLabel=正在完成 [name] 安装向导
FinishedLabel=安装完成。[name] 已装到你的电脑上：开始菜单里可以找到快捷方式，软件的文献库在安装目录的 data 文件夹。
FinishedLabelNoIcons=安装完成。[name] 已装到你的电脑上：软件的文献库在安装目录的 data 文件夹。
ClickNext=点“下一步”继续，或点“取消”退出安装。
ButtonNext=下一步(&N) > 
ButtonBack=< 上一步(&B)
ButtonInstall=安装(&I)
ButtonFinish=完成(&F)
ButtonCancel=取消
ButtonBrowse=浏览(&R)…
ButtonYes=是(&Y)
ButtonNo=否(&N)
ConfirmUninstall=确定要卸载 %1 吗？%n%n你的文献库（data 文件夹）默认会保留。
UninstallStatusLabel=正在从你的电脑上卸载 %1，请稍候…
UninstalledAll=%1 已成功卸载。%n%n文献库（data 文件夹）按你的选择处理：保留则可在下次安装后继续使用。
UninstalledMost=%1 卸载完成。%n%n个别文件无法删除（可能仍被占用），可以手动删除安装目录。
ExitSetupTitle=退出安装
ExitSetupMessage=安装尚未完成。现在退出将不会安装 %1。%n%n你可以稍后重新运行安装程序。
SetupAppRunningError=安装程序检测到 %1 正在运行。%n%n请先在软件里点“保存并退出软件”，然后点“确定”继续，或点“取消”退出。
UninstallAppRunningError=卸载程序检测到 %1 正在运行。%n%n请先退出软件，然后点“确定”继续，或点“取消”退出。

[Code]
{ 卸载的最后一步：询问是否连文献库一起删除。默认“否”，避免误删读者多年的记录。 }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{app}\data');
    if DirExists(DataDir) then
    begin
      if MsgBox('是否同时删除本机的文献库（data 文件夹，含 PDF 原文、阅读记录与 AI 答案）？' + #13#10 + #13#10 +
                '选择“否”会保留它：以后重新安装并指向同一个目录，就能继续使用。',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
