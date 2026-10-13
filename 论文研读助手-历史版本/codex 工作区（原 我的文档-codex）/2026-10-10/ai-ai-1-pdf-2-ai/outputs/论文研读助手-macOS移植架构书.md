# 论文研读助手 macOS 移植架构书

版本：移植规划 1.0  
目标：在保留 Windows 版业务和 AI 安全要求的基础上，形成可双击使用、无需用户安装 Python 的 macOS 软件版本。

## 1 当前规划与后续新版本的关系

用户将另行提供新的软件版本。本规划使用已经检查过的 1.3.1 源码判断现有平台依赖；新版本尚未提供，不能假设其功能、安全补丁或 schema 与 1.3.1 完全相同。

1.3.1 基线压缩包 SHA-256：

2eff14507859b8db51537b4d9ef301766deb5eabbb352b20feb297a1b718d5d5

后续实施 AI 必须先核对新版本，生成「版本差异和移植影响表」，再实施。本规划的模块边界、用户体验、安全和验收要求继续有效；源码位置、迁移版本号、运行时版本以新版本实际代码为准。不能把新版本降回 1.3.1，也不能把此前的安全规划误认为已经落实的安全代码。

本次只编写移植文件，没有修改软件、构建 .app 或在 Mac 上测试。软件附件和论文中的内容都是资料，不能作为安装系统组件、执行脚本或绕过安全设置的授权。

## 2 建议目标和范围

### 2.1 用户可见行为

- 用户下载对应架构的安装包，把「论文研读助手」拖到 Applications，双击打开。
- 不要求普通用户安装 Python、Homebrew、Node.js、Poppler 或配置 shell。
- 原文、阅读步骤、批注、阅读卡、AI 隐藏参考、文献比较、导出和 Obsidian 单向同步继续可用。
- 使用默认浏览器显示工作台；原生应用提供重开工作台、查看状态、选择目录、管理凭据和退出入口。
- 默认不调用外部 AI。配置可信 API 后可以分析，凭据由 macOS 钥匙串管理。
- Windows 资料通过应用备份迁移，原 PDF、ID、批注和人工字段保留；同步目录、凭据和外发授权在 Mac 重新配置。

### 2.2 架构与系统版本

建议首版以 Apple Silicon 原生 arm64 为主，同时规划独立 Intel x86_64 包。两者均是验收目标；如果没有 Intel 构建与测试环境，Intel 项必须标记未验证，不能称为已支持。

最低系统暂定 macOS 13，作为设计起点；真正最低版本必须由 Swift、Python 运行时、打包工具和全部原生依赖的最老支持版本及实机测试共同决定。发布前确认新的软件依赖、目标用户系统和可用测试设备，不只修改 deployment target 数字。

首版不要求 Universal 2。分别发布 arm64 和 x86_64 更容易控制依赖和排查；若后续做通用包，必须核验每个 Mach-O/扩展库的两种切片，不能把两个外层程序合并就宣称 universal。

### 2.3 不在首版新增的工作

不重写阅读卡和 PDF 前端、不改成云端文献库、不新增自动评分或 OCR、不引入长期驻留系统服务、不要求管理员权限。Mac App Store 沙盒版本和内嵌 WebView 可作为后续独立决策，不能顺手改变首版文件访问和会话架构。

## 3 为什么选择原生启动器加现有后端

| 方案 | 优点 | 代价 | 决策 |
| --- | --- | --- | --- |
| .command + 系统 Python | 适合开发调试 | 用户需装运行时，Finder 环境不稳定，错误进入终端 | 仅开发备用，不作为正式产品 |
| AppKit 启动器 + 内置 Python + 浏览器 | 复用已有代码，原生目录/钥匙串/生命周期，依赖较少 | 需实现小型原生桥接和签名 | 首版推荐 |
| Tauri/WKWebView + Python | 独立窗口和桌面交互 | 新增 WebView/IPC/打包及 PDF 回归范围 | 有独立窗口要求时重新评估 |
| Electron + Python | 桌面窗口及浏览器版本固定 | 安装包、更新、安全和进程复杂度增大 | 首版不采用 |
| 全部重写 Swift | 深度原生 | 业务、PDF 和 AI 链路重写成本高 | 当前需求无需重写 |

「macOS 能使用」不等于只把启动脚本换成 Shell。主要移植工作是平台服务、路径、生命周期、签名和数据迁移。

## 4 现有代码中的平台依赖

以下起始行号对应 1.3.1，仅作勘察入口，实施前应在新版本重新定位。

| 功能 | 现状位置 | macOS 处理 |
| --- | --- | --- |
| Windows 启动文件 | 启动论文研读助手.vbs、备用启动.cmd | 使用签名 AppKit .app；脚本只用于开发 |
| 弹窗 | launch.py:28、111；server.py:938 | AppKit 主线程 NSAlert，不在后台线程调用 Windows API |
| 目录选择 | server.py:558 | NSOpenPanel，经受控 IPC 返回已选择路径 |
| 进程和端口探测 | launch.py:73 | 不解析 Windows netstat/kernel32；改为子进程句柄和私有握手 |
| 程序目录写数据 | launch.py:24、25、153；server.py:1003 | Bundle 只读，用户数据/日志/run 全部移出 .app |
| 单实例写锁 | server.py:870 | 保留非 Windows 的 fcntl.flock，锁对象是稳定 library_id/数据目录 |
| 凭据来源 | providers.py:556；storage.py:268 | Keychain 平台实现，取消明文 credentials.json 回退 |
| PDF 命令发现 | pdftext.py:144、167 | 打包依赖显式资源路径；不依赖 Finder 启动后的 PATH |
| 前端快捷键 | static/app.js 的 keydown | 保留 metaKey，实测 Cmd+S/Cmd+Enter 和浏览器快捷键冲突 |
| Windows 同步路径 | library.settings.vault_path | 导入时清空并要求重新选择，不能把 D盘路径当 POSIX 目录 |

核心 Python 业务已有跨平台基础，但平台依赖必须隔离，不在业务函数中继续散落 os.name 条件。

## 5 目标组件和进程结构

~~~text
macOS AppKit 应用 PaperReader
  ├─ 应用菜单/状态入口、打开/退出
  ├─ NSOpenPanel、NSAlert
  ├─ Keychain 服务与安全凭据输入
  ├─ 子进程 Supervisor
  └─ NativeBridge（有限类型消息）
            │ 私有继承 socketpair / 有限 IPC
            ↓
内置 Python Core
  ├─ 原有 Store / schema / reading_card / analysis
  ├─ ResourceLocator / AppPaths / LibraryLock
  ├─ CredentialStore macOS 适配器
  ├─ EndpointPolicy / SecureTransport / SecretGuard
  ├─ JobPolicy / 外发授权 / 预算
  └─ 127.0.0.1 HTTP 服务
            │ 本机会话和 CSRF
            ↓
默认浏览器
  └─ 原有 PDF.js / app.js / 阅读卡和 AI 隐藏参考

本机 Ollama/LM Studio 或批准的外部 HTTPS API
  ↑ 仅由 Python 出站连接器访问
~~~

推荐 AppKit 启动器由 Swift 编写；后端采用在 Mac 构建的 PyInstaller onedir 或等价可审计方案。不是 Windows 上打出 Mac 二进制，也不要求在每台用户 Mac 上 pip install。

Swift 本身不重写论文分析逻辑。它只承担平台能力和后端生命周期。外部浏览器不直接持有或发送服务商 Key。

## 6 应用包和资源定位

建议结构：

~~~text
PaperReader.app/
  Contents/
    Info.plist
    MacOS/
      PaperReader                  原生 AppKit 可执行文件
    Helpers/
      PaperReaderCore/
        PaperReaderCore            冻结 Python 服务入口
        _internal/                 Python 和所需动态库
    Resources/
      static/
      vendor/pdfjs/
      seed/                        可选初始资料与清单
      licenses/
      app-version.json
~~~

中文显示名为「论文研读助手」，技术文件名可为 PaperReader。最终 Bundle ID 由发布者选定且保持稳定，不能擅自使用他人域名或在版本间随意改变；签名身份会影响钥匙串升级访问。

ResourceLocator 在 frozen/开发模式分别定位资源；不得依赖 cwd，也不能默认资源总在 __file__ 的同一目录。原生启动器显式给后端传递可信资源目录和数据目录，参数中没有秘密。HTTP 仅开放 static/vendor 指定资源，不开放整个 Contents 或用户数据目录。

首次初始化从 seed 复制到用户库，写入初始化标记；更新不能再次复制种子覆盖用户资料。若新版本没有可分发示例论文，不添加来源不明或不具备分发权限的 PDF。

## 7 macOS 数据目录和跨系统兼容

建议路径：

| 类型 | 建议位置 | 规则 |
| --- | --- | --- |
| 主库/PDF/正式分析 | ~/Library/Application Support/PaperReader/libraries/<library_id>/data | 可备份，保持稳定 library_id |
| 私有策略/job/预算 | ~/Library/Application Support/PaperReader/private | 当前用户权限，不进入资料备份 |
| 重建缓存 | ~/Library/Caches/PaperReader/<library_id> | 可被系统清理，缺失能重建；视为敏感派生文本 |
| 运行信息 | 用户 Application Support 的 private/run | 无 Key/会话秘密，过期验证后清理 |
| 元数据日志 | ~/Library/Logs/PaperReader | 脱敏、轮转、固定事件码 |
| API Key | macOS Keychain | 不在 Bundle、JSON、缓存、日志和备份 |

创建私有目录建议 0700，敏感普通文件 0600；不能以 chmod 防住同一账户已被控制的程序。核心主库保持新版本真实 schema，新增 platform/path 配置按迁移链处理，不在本规划硬编码下一版本号。

### 7.1 Windows 到 Mac 导入

1. 使用「导出完整备份」和 Mac 的「导入备份」，不要要求复制整个 Windows 软件目录。
2. 在临时目录验证 PDF/记录/分析，保留 paper_id、library 元数据、fields、steps、annotations、lastPage、pageLabels、note_name。
3. 清除旧 vault_path、自动同步开关、Windows API Key 引用、凭据、外发批准和可执行 queued/running 状态。
4. 恢复答案与缓存按安全规范重新校验；缓存可丢弃重建，答案标记导入来源，不信自报 verified。
5. Mac 上重新选择 Obsidian vault、配置 Keychain 和批准 API。
6. 非法 ZIP/路径/同编号冲突应明确提示并保留当前库，失败不半提交。

Windows 的 Credential Manager、DPAPI 和 Keychain 不做可移植秘密导出。跨系统迁移的是论文资料，不是账号秘密。

### 7.2 路径和文件系统

所有路径使用 pathlib/系统 API，不拼接 shell 命令。测试中文、空格、引号、emoji、长文件名、Unicode NFC/NFD、大小写敏感 APFS、同名标题和路径重解析/符号链接。以稳定 ID 识别论文，不能以显示标题的字符串大小写判断身份。

数据库放本机磁盘默认目录，不把活跃 library.json 放 iCloud/网络盘作为默认同步方案。外部盘或只读目录访问失败应可行动；不自动 chmod 用户目录或覆盖别的文件。

## 8 平台适配接口

建议保持业务核心平台无关，集中实现以下接口：

~~~text
AppPaths:
  resource_root / data_root / cache_root / log_root / runtime_root

NativeBridge:
  choose_directory(request_id) -> selected_path | cancelled
  show_error(code, safe_message)
  open_workbench(approved_local_url)
  request_credential_setup(profile_id)

CredentialStore:
  configured(profile_id) -> bool
  get_for_request(profile_id, origin, revision) -> in-memory secret
  update/delete(owned_ref)

ProcessSupervisor:
  start / wait_ready / reopen / graceful_stop / observe_exit

LibraryLock:
  acquire(library_id, data_root) / release
~~~

Windows 原有 adapter 留在 Windows 模块；macOS import 时不能执行 windll、msvcrt、PowerShell 或 CREATE_NO_WINDOW 分支。业务接口不要返回具体操作系统凭据值给 UI。

IPC 使用限长、类型白名单消息，不用 pickle、不执行任意命令、不给任意文件读写/Keychain 搜索能力。NSOpenPanel 和 Keychain 输入 UI在主线程，后端请求线程等待受控结果，取消/超时可返回；不能阻塞所有 HTTP线程。

socketpair 必须只在原生启动器和指定 core 间继承；关闭无关端点，启动 PDF 抽取/其他子进程前关闭桥接描述符。协议固定版本、带 request_id 和长度前缀，设置消息字节上限、未完成请求上限与超时；读取由单一分发器处理，避免多个线程抢读造成凭据串包。只有登记的 profile/ref/origin/revision 可以请求凭据；目录选择结果不能成为任意执行文件或打开网址能力。

### 8.1 目录选择和 TCC

首版是 Developer ID 直接分发、非 App Store 沙盒路线。NSOpenPanel 由用户选择 vault；这不等于取得任意目录永久权限，也不绕过 macOS TCC。

按实际访问只处理必要的 Documents/外部卷等权限提示，拒绝后人工阅读继续工作，显示「重新选择目录」。默认不要求 Full Disk Access、Accessibility 或 Automation。普通目录选择不需要 Finder AppleScript 控制。

若未来启用 App Sandbox，需单独设计 user-selected read/write entitlements、security-scoped bookmarks、后台 helper 权限传递和启动时 startAccessing/stopAccessing，不能把无沙盒路径方案原封不动当成已沙盒。

## 9 启动、重开和退出生命周期

### 9.1 正常启动

1. AppKit 启动器解析应用资源和用户目录，不假设终端 PATH或.zshrc已加载。
2. 启动固定签名的 bundled core，受控子进程参数、工作目录和精简环境；通过继承 socketpair建立 NativeBridge。
3. core 持有该 library 的 flock，初始化/迁移数据并启动回环 HTTP 服务。
4. 首选绑定 127.0.0.1 的系统分配空闲端口，实际端口用于 Host/Origin/CSP/会话校验，不放宽监听到局域网。
5. core 通过私有 IPC 返回 ready、library_id、instance_id、端口和一次性 bootstrap；原生启动器才打开默认浏览器。
6. 页面通过受控 bootstrap建立会话，清除fragment；不能在公开首页裸发管理 token。

bootstrap 必须短时有效、单次消费，在页面执行其他逻辑前清除地址栏片段；不进入浏览器持久存储、HTTP日志、系统诊断或导出。兑换端点设置请求限额，建立 HttpOnly、SameSite 严格的本机会话；当前 HTTP 回环方案不能假定 Secure cookie 一定可用，应在 Safari/Chrome 验证实际行为。公开健康端点不返回论文、路径、profile 或授权信息。认证、精确 Host/Origin 检查、CSRF、禁止通配 CORS 共同保护所有管理路由；启动 HTML 和 API 一律 no-store。CSP 禁止浏览器直连服务商和加载模型输出提供的外部资源。该方案防御非授权网页/进程接入，不声称抵御已控制用户账户或浏览器的攻击者。

启动器用结构化 ready握手和子进程状态，不扫描一组端口并把 Key 发给疑似模型/服务。仅可发布无秘密健康信息；root路径哈希不是服务器身份证明。

### 9.2 双击第二次和多个应用副本

同一已运行 App 的 LaunchServices reopen 事件由原生应用处理，打开现有工作台或签发新的单次 bootstrap，不创建第二个 writer。浏览器刷新/第二窗口按有效会话恢复。

两个不同 .app副本尝试打开同一库时，库级 flock仍是最终保护。首版可安全提示「该资料库正被使用」，而不是自动转到未知本机服务器或杀进程。若后续实现跨副本复用，需专用当前用户 Unix domain socket、peer UID检查、实例握手和受控能力，不靠PID/health字符串猜身份。Unix socket路径要验证系统字节长度上限，不能假设Linux抽象socket也能在macOS使用。

运行信息带 PID、instance_id、启动时间和library_id，PID可能复用，不作为单独进程身份。过期信息清理不删除正常持有锁的文件，不killall python、不杀占端口的其他应用。

### 9.3 退出和睡眠

原生应用提供「退出论文研读助手」，停止接纳新任务、取消后续AI调用、等待有限时间、保存/原子提交记录、关HTTP/IPC、释放锁。浏览器关页不等于服务退出，应用菜单/状态入口清楚展示运行状态。

父进程正常退出或IPC EOF后core应受控停止；意外崩溃/强制退出时，下次验证锁/临时文件/任务状态，恢复人工草稿，已发AI请求按结果未知处理，不盲重发。

macOS睡眠、唤醒和网络变化不得造成无限重试；Keychain锁定/网络断开只影响AI，不影响本地PDF和记录。不能宣称取消请求必然远端退款。

## 10 macOS 钥匙串和 API 安全

此前安全架构中的目标绑定、HTTPS、TLS、重定向/代理限制、SecretGuard、任务冻结、预算和备份恢复规则继续有效。macOS移植不是放宽它们的理由。

远端连接须规范化并绑定 origin 与允许的 base path，拒绝 userinfo、URL 中的 Key、控制字符及危险地址；DNS 全部结果与真正连接目标均须验证，防止检查后重解析转入内网/metadata。认证请求、健康检查、模型列表均不自动跟随重定向或继承系统/环境代理，TLS 验证不得关闭。Key 只加入已批准目标的认证 header，不能进入 prompt/body。连接和响应设置时间、字节和 token 上限；适配器不支持必需约束时明确拒绝该能力，不能悄悄放宽限制。

错误、异常、模型成功结果、JSON 修复、日志、分享、备份和同步均经过固定错误映射、严格字段白名单与秘密回显拦截；不落 raw_model_output。任务冻结 paper_id/PDF hash、profile 与凭据 revision、模型、prompt/schema 版本、generation 和授权，提交前核对。预算对主请求/修复/重试共同原子预留；取消后不再新增请求，断线后结果未知不自动重复付费。论文和模型输出只能作为数据，不授予模型文件、网络、Keychain 或命令工具权限。凭据安全不等于论文保密，外部服务商仍会收到明确批准范围的论文正文；未公开文献应由使用者决定是否发送。

### 10.1 选定凭据路线

推荐由签名的 AppKit 进程使用 Security.framework管理本应用 Generic Password条目，Python通过私有 NativeBridge获取已登记profile所需Key。Key仅在本机进程内存和批准API认证header中存在，不通过HTTP返回浏览器。

Keychain service/account由稳定应用命名空间和随机profile ID确定；默认不设置 synchronizable，禁止跨应用通用读取、任意账户名搜索或allow-all-apps。绑定endpoint/profile/revision；读取前后端再次校验授权。

用户在原生安全输入框填写Key，由原生应用直接写Keychain；输入之后清除应用引用，不把Key放命令行、环境文件、shell history、launchctl全局环境或剪贴板脚本。读取/取消/锁定错误均是固定代码，不回显秘密。

有明确开发需求时，可保留受登记的环境变量来源；Finder启动的App通常不会自动继承终端shell配置。因此正式用户指南首选Keychain，不让用户修改.zshrc或执行echo Key来解决GUI取不到Key的问题。

### 10.2 签名、升级和权限

保持Bundle ID和Developer ID身份；升级要验证旧Keychain条目仍可由新版本访问。具体ACL/access group/访问策略必须在实际签名产物上测试，不能只测试unsigned Python读取成功。

Keychain拒绝、锁定或凭据不存在时停止AI，不fallback到credentials.json。换Mac、换系统用户或Windows迁移后重新录入，不打包导出钥匙串条目。

Keychain保护磁盘凭据，不抵御管理员、同账户恶意进程、进程内存被控制或不可信服务商。原生/后端IPC也不能因此打印秘密或授予模型工具。

## 11 PDF、浏览器和输入体验

PDF.js静态资源本地打包，包括worker、cmaps、standard_fonts/wasm等，以新版本真实目录为准。不得在Mac版转用CDN，导致阅读需要网络或泄露文献。

保留原有页码、选择文字、独立高亮和批注归一化坐标。Retina按设备像素比渲染，限制canvas内存；ResizeObserver和异步取消防止旧页覆盖。Mac触控板deltaMode、惯性、放大后滚动边界需实测，不靠Windows鼠标测试宣称通过。

首版验收Safari稳定版和Chrome稳定版；Edge/Firefox可扩展但未测不宣称。检查PDF.js/TextLayer/wasm、下载、dialog、file input、CSP和本机cookie行为。

Cmd+S保存、Cmd+Enter保存批注要在页面焦点场景正常；Cmd+Q退出原生App与关闭浏览器区分。不要抢占全局系统快捷键，也不需要Accessibility权限。

中文输入法组合事件不能触发错误保存/翻页；trackpad pinch和Cmd滚动不能被误判翻页。浏览器缓存/资料库草稿 namespace隔离继续保留。

### 11.1 继承既有阅读体验需求

Mac 版必须继续满足用户之前提出的整页阅读和隐藏 AI 对照需求。默认 fit-page 按当前阅读容器的宽高较小缩放比计算，让完整页边界同时可见，PDF 是阅读模式的主要区域；阅读卡采用可收起侧栏，不能因移植重新压缩原文。初步验收在 1280×800 和 1440×900 CSS 视口、侧栏收起状态下，PDF 区域占内容区宽度至少 70%，适页状态没有页面内容裁切；浏览器工具栏不计入应用视口。若新版本已有更明确的尺寸标准，以用户已接受的新版要求为准。

默认滚轮/触控板以页为单位前后翻页，一个惯性手势最多触发一次；边界页保持稳定。放大到无法整页显示时，页内滚动优先，达到边界并经新的意图明确手势才翻页。滚动阅读卡、输入框或系统缩放手势不翻 PDF；不能通过简单固定毫秒节流就宣称触控板体验达标。

每篇论文按实际阅读卡 schema 生成独立分析，默认折叠，点击才展示；查看已有答案不发 API 请求，不写人工字段或进度。初次生成按钮必须可用；用户明确启用并批准指定 API/发送范围后，新论文可以自动入队，不重复索要同一有效授权。换目标、恢复备份或越出授权范围需重新确认；失败保留原 PDF 和人工记录，答案作为对照而非替代。

## 12 PDF 抽取和运行时依赖

在 Mac 上构建并固定Python运行时、PyInstaller、Swift target和解析器版本。开发工具可以在构建机器使用，普通用户不应被要求安装。

建议初版以内置受限抽取器加打包的受维护纯Python PDF库作为可部署基线，选择准确性与资源测试通过的后端。现有pdftotext选项保留为显式可选：

- 若提供bundled Poppler，需同时打包对应架构dylib、许可、签名和最老系统兼容证据。
- 若不打包，不依赖用户PATH中同名命令；找不到时明确降级到经过验证的后端。
- 不能把Windows .exe或/opt/homebrew路径写死到正式包；Intel Homebrew位置不同，Finder环境也不同。

抽取进程无Key环境，受限CPU时间、内存、解压、页数和输出。子进程shell=False且参数数组，避免文件名注入。不执行PDF脚本/附件或外链。无文本、加密、损坏时人工阅读保留，不伪造AI分析。

## 13 构建、签名和发布

### 13.1 构建环境

真正macOS构建在Mac或可信Mac CI上进行。不能在当前Windows环境宣称完成Mac .app、codesign或实机验证。

分别锁定arm64/x86_64依赖与构建环境；所有原生二进制架构一致，检测绝对构建路径、缺失dylib、最低系统要求和许可证。PyInstaller采用onedir以避免启动时大量解包，并在Finder环境验证资源定位。

### 13.2 发布顺序

1. 构建Swift launcher和Python core。
2. 收集资源/依赖并扫描无Key、凭据、runtime、logs和用户资料。
3. 内部原生库/扩展/辅助程序按依赖关系逐个签名，再签主.app。
4. 开启Hardened Runtime，按真实需要评估最小entitlements；不默认关闭library validation或添加任意JIT/unsigned memory权限。
5. 使用发布者自己的Developer ID Application签名。私钥、证书口令、notary凭据留在Keychain/CI秘密存储，不进入源码和支持包。
6. 生成DMG；提交Apple notarization，检查结果，staple适用的.app/分发产物，按实际流程验证。
7. 在从下载入口重新下载的、保留quarantine的干净Mac上验证Gatekeeper、首次启动、离线启动和功能。
8. 发布arm64与x86_64包、版本/架构/最低系统说明、哈希、许可、迁移说明。

不要把codesign --deep作为一条签名魔法命令，不递归随意重签用户文件。不指导用户关闭Gatekeeper、移除整目录quarantine、用sudo修权限或安装脚本绕过系统保护。

没有开发者证书/公证条件时，可产出标为未签名的内部测试构建和未完成清单，但不能冒充可正常分发的正式版本。发布授权和证书配置在具体产物可审查后处理。

### 13.3 更新和卸载

首版手动更新优先：退出旧服务，更换.app，用户Application Support数据和Keychain保留。迁移前安全备份，不用覆盖Bundle内data实现升级。

卸载.app默认保留用户资料；提供独立「删除本应用资料和凭据」动作，先展示具体范围并要求明确选择。不得删除用户Obsidian vault、原始外部PDF、其他应用钥匙串或已分享文件。

将来自动更新需要独立签名/版本降级/供应链设计，不从模型响应或任意HTTP链接下载执行。

## 14 新版本到来后必须核对的清单

- 新版源码、app版本、schema版本、资源目录与打包依赖。
- PDF前端是否更换，是否增加桌面框架、OCR或原生二进制。
- AI安全措施究竟哪些已实施；F01–F15在新版的对应状态。
- credential/store/profile/job/授权/预算/backup接口是否变化。
- Windows限定模块是否新增，不只查旧launch.py。
- 最新备份格式和跨平台迁移约束。
- 目标Mac CPU/最低系统、签名发布者身份、Mac构建/测试环境。

这些核对只改变必要实施细节，不取消外部API安全要求或人工数据兼容性。

## 15 完成与交付门槛

按「macOS分阶段执行提示词」进行实施，按「macOS验收与新版本对接清单」记录结果。完成必须满足：

1. 干净Mac无需Python/Homebrew可从Finder双击启动，Bundle可只读。
2. 标注支持的CPU/系统均有实际产物和测试；未测架构不冒称支持。
3. 平台依赖有统一adapter，Windows原版核心回归通过。
4. Keychain真实签名产物的新增/读取/锁定/删除/升级流程通过，Key未进页面/日志/备份。
5. 外部HTTPS API、本机API、AI默认关闭及隐藏参考行为通过；恢复不自动上传。
6. Windows备份可迁入Mac且人工记录不丢；目录权限/取消/睡眠/多实例/退出可预期。
7. Safari/Chrome、触控板/Retina/中文输入和PDF证据跳页实测通过。
8. 正式发布签名、公证、Gatekeeper与下载后quarantine测试通过。

后续AI交付：源码补丁、平台接口说明、构建锁文件与脚本、两架构产物或明确未验证项、签名/公证证据、迁移/回滚/用户指南、Mac实机报告及安全测试。仅有一个.command或在Windows通过Python单元测试不等于macOS移植完成。
