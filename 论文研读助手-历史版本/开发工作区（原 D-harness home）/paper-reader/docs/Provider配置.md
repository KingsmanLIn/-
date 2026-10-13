# Provider 配置文档

参考分析由**本机**语言模型生成。软件不内置模型、不联网下载、不上传论文；需要你自己
在本机跑一个推理服务，然后在设置页里填入它的地址与模型名。

---

## 一、支持的三类 Provider

| Provider | 适用 | 默认地址 | 协议 |
|---|---|---|---|
| `ollama` | Ollama 本机服务 | `http://127.0.0.1:11434` | `/api/chat`（`format: json`） |
| `lmstudio` | LM Studio 本机服务 | `http://127.0.0.1:1234/v1` | OpenAI 兼容 `/chat/completions` |
| `openai_compatible` | 任何 OpenAI 兼容接口（含局域网机器、第三方服务） | 需自行填写 `base_url` | OpenAI 兼容 `/chat/completions` |
| `none` | 不使用 AI（默认） | — | 不发送任何请求 |

`base_url` 留空即使用上表默认值。

---

## 二、Ollama

1. 安装 Ollama（Windows 安装包），确认服务在运行：`ollama list` 能列出模型。
2. 拉一个适合中文与长文的中等模型，例如：
   ```
   ollama pull qwen2.5:7b
   ```
3. 在软件"设置与 Obsidian 同步 → AI 参考分析"里填：
   - Provider：`Ollama（本机）`
   - 模型名称：`qwen2.5:7b`
   - 接口地址：留空（默认 `127.0.0.1:11434`）
   - 密钥环境变量名：留空（本机 Ollama 不需要密钥）
4. 点"保存 AI 设置"，状态行应显示 `Provider 可用：ollama · qwen2.5:7b`。

## 三、LM Studio

1. 打开 LM Studio，在 "Developer" / "Local Server" 里 **Start Server**（默认端口 1234）。
2. 加载一个模型（建议 7B–14B 量化版，显存不足时用 Q4 量化）。
3. 设置页填：
   - Provider：`LM Studio（本机 OpenAI 兼容）`
   - 模型名称：LM Studio 里显示的模型标识（如 `qwen2.5-7b-instruct`）
   - 接口地址：留空（默认 `127.0.0.1:1234/v1`），若改过端口则填 `http://127.0.0.1:<端口>/v1`
4. 保存后状态行会列出可用模型。

## 四、需要密钥的接口

1. 在系统里设置环境变量，例如 PowerShell：
   ```powershell
   setx MY_LLM_API_KEY "sk-..."
   ```
   或在软件 `data/credentials.json` 中写入 `{"MY_LLM_API_KEY": "sk-..."}`（该文件不进入备份）。
2. 设置页只填**变量名** `MY_LLM_API_KEY`，不要填密钥本身（填了会被拒绝并提示）。
3. 密钥只从环境变量或 `data/credentials.json` 读取，**不会**写入文献库、备份、日志、页面或导出文件。

**数据边界**：`ollama` / `lmstudio` 默认只连本机回环地址，论文原文不出本机。一旦你把
`base_url` 改成局域网或公网地址（含 `openai_compatible` 指向第三方），论文原文就会发送到
那个地址——请自行确认对方的隐私与留存政策。软件不会替你判断这一点。

---

## 五、自动排队开关

| 设置 | 行为 |
|---|---|
| `auto_analyze = false`（默认，推荐） | 导入新论文只登记状态 `waiting_for_user`；你点击某篇的"查看 AI 参考"后才请求模型 |
| `auto_analyze = true` | 导入后立即 `queued`，后台生成；未配置 Provider 时保持 `waiting_for_provider`，配置后自动补齐 |

## 六、并发与耗时

- `max_concurrent_jobs` 当前固定为 1（单篇顺序处理），修改后需重启服务生效。
- 单篇耗时与页数、模型大小、量化方式、CPU/GPU 相关，量级参考：
  - 7B Q4（GPU 推理）：20–40 页论文约 30–90 秒；
  - 7B Q4（纯 CPU）：同样的论文约 3–10 分钟；
  - 14B 及以上：约为上面的 2–3 倍。
- 生成期间可以照常阅读、批注、填写阅读卡、导出；关闭页面不影响后台任务，但关闭软件会中断，
  下次启动会安全地把未完成任务重新排队（不会重复生成已完成的结果）。

## 七、超时与取消

- 单次请求超时 900 秒（`RUNNING_TIMEOUT_SECONDS`），超时记为 `failed`（`error_code=timeout`），可重试。
- 界面上可"取消"排队中或运行中的任务；取消后状态为 `cancelled`，可再次生成。
- 服务重启时：`running` 且已超时或残留的任务会重新排队；`queued` 会重新排队；
  `waiting_for_provider` 在有可用 Provider 时自动排队。恢复过程幂等，不会产生重复的 `ready` 结果。

## 八、常见问题

| 现象 | 处理 |
|---|---|
| 状态显示"Provider 当前不可用：unreachable" | 确认 Ollama/LM Studio 正在运行、端口正确；本机防火墙是否拦截 |
| `model_not_found` | 模型名写错或尚未拉取；先在 Ollama/LM Studio 里确认模型标识 |
| 一直 `waiting_for_provider` | 设置页里 Provider 仍是"不使用"；保存一个可用 Provider 后会自动恢复 |
| 生成结果 `failed / validation_failed` | 模型没按 JSON 输出或引文核对不上；换更大的模型或降低上下文长度后重试 |
| 想换成外部接口 | 填 `openai_compatible` + `base_url` + 密钥变量名；注意上面的数据边界说明 |
