"""Build a private-data-free, portable Windows release from reviewed inputs."""
from pathlib import Path, PurePosixPath
import hashlib
import json
import shutil
import sys
import zipfile

WORK = Path(__file__).resolve().parent
CODE = WORK/'review-1.3.2'
OUT = WORK.parent/'outputs'
NAME = '论文研读助手-本地版-1.3.3-完整便携版'
DEST = OUT/NAME
INPUT = Path(r'D:\harness home\releases\1.3.2\论文研读助手-本地版-1.3.2-全新安装.zip')
PYTHON = Path(sys.executable).parent
assert not DEST.exists(), 'Do not overwrite a previously built release'
DEST.mkdir(parents=True)
with zipfile.ZipFile(INPUT) as archive:
    for member in archive.infolist():
        parts = PurePosixPath(member.filename).parts[1:]
        if not parts or member.is_dir():
            continue
        if '..' in parts or any(':' in p or '\\' in p for p in parts):
            raise ValueError('Unsafe archive path')
        if parts[0] not in ('static', 'vendor'):
            continue
        target = DEST.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(member))
for source in CODE.glob('*.py'):
    shutil.copy2(source, DEST/source.name)
# Reviewed UI assets override their originals.
for folder in ('static',):
    for source in (CODE/folder).rglob('*'):
        if source.is_file() and '__pycache__' not in source.parts:
            shutil.copy2(source, DEST/folder/source.relative_to(CODE/folder))

runtime = DEST/'runtime'
runtime.mkdir()
for source in PYTHON.iterdir():
    if source.is_file() and (source.suffix.lower() in ('.exe', '.dll') or source.name=='LICENSE.txt'):
        shutil.copy2(source, runtime/source.name)
for source in (PYTHON/'DLLs').iterdir():
    if source.is_file() and source.suffix.lower() in ('.pyd', '.dll'):
        shutil.copy2(source, runtime/source.name)
with zipfile.ZipFile(runtime/'python312.zip', 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    excluded = {'site-packages','__pycache__','test','tests','idlelib','tkinter','turtledemo','ensurepip'}
    for source in (PYTHON/'Lib').rglob('*'):
        relative = source.relative_to(PYTHON/'Lib')
        if source.is_file() and not any(p in excluded for p in relative.parts) \
                and source.suffix in ('.py', '.txt', '.json', '.pem'):
            z.write(source, relative.as_posix())
# Isolated search path prevents PYTHONPATH and global site-packages from injecting
# modules into the portable app. Relative '..' is the installation's own source folder.
(runtime/'python312._pth').write_text('python312.zip\n.\n..\n',encoding='ascii')
(DEST/'启动论文研读助手.vbs').write_text('''Option Explicit
Dim shell, fso, root, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = root
command = Chr(34) & root & "\\runtime\\pythonw.exe" & Chr(34) & " -I " & Chr(34) & root & "\\launch.py" & Chr(34)
shell.Run command, 0, False
''',encoding='utf-16')
(DEST/'启动（备用）.bat').write_bytes(('''@echo off
cd /d "%~dp0"
"%~dp0runtime\\python.exe" -I "%~dp0launch.py"
if errorlevel 1 pause
''').replace('\n','\r\n').encode('ascii'))
(DEST/'使用说明.md').write_text('''# 论文研读助手 1.3.3 完整便携版

适用 Windows 10 / 11 64 位。内含 Python 3.12.14 与 PDF.js，无需另装 Python 或执行 pip 安装。

## 启动与导入

1. 把完整文件夹解压到可写入的位置。不要在压缩包内直接启动。
2. 双击“启动论文研读助手.vbs”；若系统禁用脚本，双击“启动（备用）.bat”。软件在默认浏览器打开。
3. 使用“导入 PDF”添加论文。该版本从空论文库开始，未附带任何个人论文、阅读卡、密钥或测试凭据。
4. 原版本先“导出备份”，新版本使用“恢复备份”。恢复会重新验证 PDF 与 AI 答案；恢复数据不会自动授权模型调用。旧版本及其 data 目录请先保留。
5. 阅读时选择整页适配，滚轮逐页切换。AI 参考在点击查看后显示，便于和自己的阅读卡对照。
6. 退出使用页面中的“保存并退出”。

## 连接 AI

1. 本机 Ollama / LM Studio：选择对应 Provider、本机模式、模型与回环地址。
2. 远程 API：选择兼容 Provider、cloud_https 模式，填写供应商最终 HTTPS 地址与模型名。云端私网/元数据地址、HTTP 和自动重定向被拒绝。
3. 在 Windows 的“编辑账户的环境变量”中添加 PAPER_READER_VENDOR_KEY，把真实 Key 作为变量值。重启软件使新进程读到变量。
4. 软件设置只填变量名 PAPER_READER_VENDOR_KEY 或 env:PAPER_READER_VENDOR_KEY；不要填 Key 本身。软件不把 Key 写入论文库、备份或导出文件。
5. 保存配置后，针对所需论文点击生成 / 查看 AI 参考。发送论文文本给选定供应商需要你的明确操作；自动分析需自行开启。可随时暂停所有 AI 请求、取消当前任务。
6. 改地址、模型、凭据引用或输出上限后，旧任务需重新确认；同名环境变量内的 Key 更换也会使旧任务失效。已发送请求的供应商计费不能通过本地取消撤回。
7. 每篇任务、24 小时窗口、输出 Token 和队列都有额度限制。应用额度是请求次数上限，不是供应商账单金额；具体价格以供应商为准。

## 安全与兼容范围

本次验证使用本地 HTTP/TLS 桩、合成论文与虚构密钥，未使用真实 API Key、未调用外部供应商。真实供应商的模型名、参数和账单仍需后续兼容性验证。
凭据在请求期间存在于进程内存；拥有同一用户权限的恶意程序、系统管理员、供应商自身、崩溃转储或系统环境变量配置不在软件输出边界的保障范围内。不要在不可信电脑上使用真实 Key。
PDF 文本解析使用无凭据子进程，设置 120 秒总时限、512 MiB 内存、60 秒 CPU、16 MiB 输出、100 MiB 输入和单个解压流 16 MiB 上限。超限时可继续人工阅读，AI 分析报告失败。子进程资源限制不是抵御所有本机代码执行漏洞的操作系统沙箱。
当前发布为 Windows 版；macOS 移植架构仍属后续工作。

## 许可

内含 CPython；许可证见 runtime/LICENSE.txt。PDF.js 沿用随包版权与许可证声明。
''',encoding='utf-8')
(DEST/'版本说明.md').write_text('''# 1.3.3 修复说明

修复 1.3.2 审查发现的配置/任务目标混用、健康检查模型列表回显 Key、GUI 设置丢失模式、旧任务失败覆盖新结果、超过 1000 次的额度截断、取消后的可选参数重发、重复 JSON 键/非有限数字接受、慢速网络无限延长请求、短 Key 与多次轮换漏检、普通 PDF 解析未隔离及临时文件清理遗漏。

增加同名环境变量凭据轮换检查、共享任务协调锁、发送前权限复核、DNS/网络总时限、PDF 资源限制和完整便携运行环境。全新安装仅含空论文库，不复制原安装包里的个人数据。

详细验证结果见随发布交付的测试与修复报告；实际供应商未测试。
''',encoding='utf-8')
# Construct the empty library using the real current Store contract.
sys.path.insert(0,str(CODE))
import server
server.Store(DEST/'data')
for p in (DEST/'data').iterdir():
    if p.is_file() and p.name != 'library.json':
        p.unlink()
metadata = {'version':'1.3.3', 'platform':'Windows x64', 'python':sys.version.split()[0],
    'original_zip_sha256':hashlib.sha256(INPUT.read_bytes()).hexdigest(),
    'private_data_included':False, 'real_api_tested':False}
(DEST/'release.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'stage':str(DEST),'python':str(runtime/'python.exe'),
                 'files':len(list(DEST.rglob('*')))},ensure_ascii=False))
