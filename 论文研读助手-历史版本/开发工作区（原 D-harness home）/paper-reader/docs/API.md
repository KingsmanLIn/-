# API 文档（论文研读助手-本地版 · 改造后）

服务只监听 `127.0.0.1`，端口从 8765 起自动找空位；实际地址写在 `run/server.json`。
除 `GET /api/health` 外，所有接口都需要令牌。

## 通用约定

| 项目 | 说明 |
|---|---|
| 认证 | 请求头 `X-Reader-Token: <启动令牌>`，或 JSON 体内 `token` 字段；令牌在页面 `<script id="boot">` 中 |
| 拒绝范围 | 非本机 `Host`、跨站 `Origin`、`Sec-Fetch-Site: cross-site`、错误令牌 → **403**（`{"error": "..."}`） |
| 内容类型 | 请求/响应均为 UTF-8 JSON；文件类接口返回二进制并带 `Content-Disposition` |
| 安全头 | `Content-Security-Policy`（`default-src 'self'`、`object-src 'none'`、`frame-ancestors 'none'`、`connect-src 'self'`）、`X-Frame-Options: DENY`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer` |
| 错误码 | 400 参数/状态错误；403 未授权或来源受限；404 资源不存在；409 版本冲突；413 文件过大 |
| 分析状态 | `none / queued / running / ready / failed / stale / waiting_for_provider / waiting_for_user / cancelled` |
| 分析阶段 | `queued / extracting / chunking / requesting / validating / saving / done`（progress 0/15/30/55/80/95/100） |
| 密钥 | 任何接口都不接受、不回显密钥值；只保存环境变量名 `api_key_env` |

---

## 基础

### `GET /api/health`（无需令牌）

```json
{"app": "paper-reader", "version": "1.1.0", "schema_version": 2,
 "root": "<路径哈希前16位>", "data": "<数据目录哈希前16位>"}
```
不返回令牌、路径明文或库内容。

### `GET /api/state`

```json
{"version": 2, "papers": [...], "settings": {...}, "trash": [...]}
```
`papers[]` 含人工 `fields` / `steps` / `annotations` 与 `analysis` 元数据。**不含**任何答案正文。

### `POST /api/save`

```json
{"id": "medical-2026-zang", "revision": 3,
 "patch": {"fields": {...}, "steps": [true,false,false,false], "annotations": [...],
           "title": "...", "type": "empirical", "lastPage": 7}}
```

- `revision` 必须等于当前版本，否则 **409**（`{"error": "这篇论文已在另一个窗口更新..."}`）。
- 只接受 `fields/steps/annotations/标题类/最后阅读页` 等白名单字段；`analysis` 与未知键会被忽略。
- 返回 `{"paper": {...}, "revision": n+1}`。

### `GET /api/pdf/<paper_id>` / `GET /api/export/<paper_id>`

返回原文 PDF 或 Markdown 阅读卡（含人工记录、批注与页码）。**导出不包含任何 AI 参考内容。**

### `GET /api/backup` / `POST /api/restore`

- 备份：ZIP，含 `library.json`、`manifest.json`、`papers/*.pdf`、`analyses/*.json`（已生成的分析），
  以及 `extracted/`（可再生的文本缓存）。**不含密钥**。
- 恢复：请求体为 ZIP 二进制（上限 200 MB），补入当前库缺少的文献，保留已有同编号记录；
  恢复后的 `analysis` 状态与 `source_sha256` 与备份一致。

### `POST /api/settings`

```json
{"vault_path": "D:\\我的笔记库", "sync_enabled": true,
 "provider": "ollama", "base_url": "", "model": "qwen2.5:7b",
 "api_key_env": "OLLAMA_API_KEY", "auto_analyze": false, "max_concurrent_jobs": 1}
```
- 携带 `api_key` / `api_key_value` → **400**（"不要把密钥写进设置"）。
- 未知 provider → **400**。
- 返回 `{"settings": {...}, "ai": {...}, "providers": [...], "resumed": [...], "health": {...}}`。
  响应中的 `ai` 与 `settings.ai` 都是刚保存的值；密钥值永不出现。

### `GET /api/ai-settings`

返回 `{"ai": {...}, "providers": ["none","ollama","lmstudio","openai_compatible"]}`。

### `POST /api/ai-settings`（只改 AI 配置）

```json
{"ai": {"provider": "lmstudio", "model": "local-model", "auto_analyze": true}}
```
返回 `{"ai": {...}, "resumed": [...], "health": {...}}`。设置 provider 后，等待中的论文会自动重新排队。

### `POST /api/sync` / `POST /api/choose-vault` / `POST /api/shutdown`

立即同步全部文献；选择笔记库目录；保存并退出软件。

---

## 导入

### `POST /api/import`

- 请求体：PDF 二进制；文件名放在 `X-File-Name`（非 ASCII 需百分号编码）。
- 上限 200 MB（`413` 超过时返回）。
- **立即返回**，不等模型：

```json
{"paper": {"id": "medical-2026-zang", "title": "...", "language": "zh",
           "analysis": {"status": "waiting_for_user", "source_sha256": "<64位>", "...": "..."}}}
```

| 导入时的设置 | 返回状态 |
|---|---|
| 已配置 Provider 且 `auto_analyze=true` | `queued`（后台开始生成） |
| 已配置 Provider 且 `auto_analyze=false`（默认） | `waiting_for_user`（点"查看 AI 参考"才入队） |
| 未配置 Provider（`provider=none`） | `waiting_for_provider` |

导入耗时与模型耗时无关：模型延迟 2 s 时接口仍在 1.5 s 内返回，且返回时模型调用次数为 0。

---

## 参考分析

### `GET /api/analysis`（列表与健康检查）

```json
{"statuses": {"<paper_id>": {...}}, "queue": {"queued": [...], "running": [...]},
 "health": {"ok": true, "provider": "ollama", "category": "ok", "detail": "...",
            "models": ["qwen2.5:7b"], "latency_ms": 12, "ready": true}}
```

### `GET /api/analysis/<paper_id>/status`

只返回状态信息，**不含答案正文**，可安全轮询：

```json
{"paper_id": "medical-2026-zang", "status": "running", "stage": "requesting",
 "progress": 55, "message": "正在请求本地模型", "requested_at": "...", "started_at": "...",
 "completed_at": "", "updated_at": "...", "duration_ms": 0, "attempt": 1,
 "provider": "ollama", "adapter": "ollama", "model": "qwen2.5:7b",
 "prompt_version": "reading-card-v1", "language": "zh", "error_code": "",
 "error_summary": "", "answer_file": "", "has_answer": false,
 "in_queue": true, "running_now": true, "generated_at": "..."}
```

- `error_summary` 截断到 200 字；未通过校验的模型文本不会出现在这里。
- 未知论文 → **400**；非法编号或路径穿越（含 `%2e%2e`）→ **400**；未知子资源 → **404**。

### `GET /api/analysis/<paper_id>`

只有 `ready` 才返回正文：

```json
{"paper_id": "...", "status": "ready", "answer_available": true, "answer_error": "",
 "reason": "", "analysis": {...},
 "answer": {"schema_version": 1, "paper_id": "...", "prompt_version": "reading-card-v1",
            "provider": "ollama", "model": "qwen2.5:7b", "language": "zh",
            "source_sha256": "...", "created_at": "...", "generated_at": "...",
            "overall": {"title": "...", "language": "zh", "paper_type": "empirical",
                        "main_claim": "...", "conclusion": "...", "confidence": "medium"},
            "answers": {"<13个字段之一>": {"status": "found|partial|not_found",
                                          "answer": "...",
                                          "evidence": [{"page": 1, "quote": "...",
                                                        "verified": true, "similarity": 1.0}],
                                          "confidence": "high|medium|low",
                                          "causal_strength": "causal|correlational|descriptive|unclear",
                                          "verified_quotes": 1}},
            "terms": [...], "reader_questions": [...], "warnings": [...],
            "verification": {"claimed_quotes": 13, "verified_quotes": 13, "corrected_pages": 0,
                             "corrections": [], "page_count": 29, "field_count": 13,
                             "not_found_fields": []}}}
```

- 非 `ready` 时 `answer=null`、`answer_available=false`，`reason` 说明原因（例如"参考分析尚未就绪"）。
- 13 个字段：`question, motivation, claim, data, variables, method, evidence, robustness,
  boundaries, assessment, summary, connections, next`。
- 未找到依据的字段：`status="not_found"`、`answer="原文未说明"`、`evidence=[]`。

### `POST /api/analysis/<paper_id>/generate`

| 请求体 | 行为 |
|---|---|
| `{}` | 普通重试：`failed/stale/cancelled/none/waiting_for_user` 会入队；`ready` 返回 `already_ready`（不重复请求模型） |
| `{"force": true}` | 明确重跑，无论当前状态，消耗模型算力与时间（外部接口可能产生费用） |

返回 `{"run": {"paper_id": "...", "status": "queued|ready|running", "queued": true,
"reason": "retry|force|already_ready|already_pending"}, "status": {...与 status 接口相同...}}`。

### `POST /api/analysis/<paper_id>/cancel`

取消排队或运行中的任务，返回 `{"cancelled": true|false, "status": {...}}`。

### 兼容路由（阶段 3 接线，保留）

- `POST /api/analysis-run`：等价于 `generate`，请求体 `{"paper_id": "...", "force": true}`。
- `POST /api/analysis-cancel`：等价于 `<id>/cancel`。

---

## 失败与重试语义

| 情况 | 结果 |
|---|---|
| 模型连接失败 / 超时 | `failed`，`error_code` 为 `unreachable` / `timeout`，可重试 |
| 模型拒绝回答 | `failed`，`error_code=refused` |
| 输出不是 JSON | `failed`，`error_code=validation_failed`（`bad_json`/`not_an_object`） |
| 缺字段 / 假引文 / 越界页码 / 因果过度 | 首次校验失败 → **一次**修复请求 → 仍失败则 `failed` |
| 未配置 Provider | 入队被拒绝，状态停在 `waiting_for_provider`（配置后自动恢复） |
| 原文或提示词/模型/语言变化 | 已生成的参考变为 `stale`，需重新生成 |

失败时**不会**产生 `data/analyses/<id>.json`；模型原始输出（脱敏、截断）写入
`data/analyses/<id>.failed.json` 供排查，界面上永远不展示未通过校验的文本。
