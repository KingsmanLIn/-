# 改造记录（论文研读助手-本地版）

每次批次结束更新。原始发布包从未被修改；备份位置见 `D:\harness home\backups\`。

---

## 阶段 10 — 原文区改为纵向主区域（发布 1.3.0）

用户反馈：论文原文在软件里占比太小、不好读。改动分两层，都只动布局，不动数据与交互语义。

### 1. 压缩上方条目，把高度让给原文（桌面 ≥931px）

| 元素 | 之前 | 之后 |
|---|---|---|
| 顶栏 | 65px | 48px（隐藏英文小标题行） |
| 标题条 | 80–87px（三行） | 26px（单行：标题 + 标签 + 作者 + 按钮） |
| 四步研读条 | 39px | 29px |
| 工具条 / 副栏 / 页脚 | 51 / 29 / 38px | 43 / 23 / 32px |
| **上方占用合计** | **346px** | **245px** |
| **原文可视高度（1366×768）** | **396px（51.6% 视口）** | **525px（68.4% 视口）** |

### 2. 收起记录栏：原文区占满宽度

- 工具条右上角新增 **收起记录栏 / 展开记录栏** 按钮（`#toggle-notes`，`aria-pressed`）。
- 收起时 `.workspace` 变单列、`.notes-panel` 隐藏；原文区宽度 734→1096px，
  正文由约 11px 增大到约 16.8px；选择被写入 `localStorage`，刷新后保持。
- 收起期间记录数据照常保存（只是不显示）；窄屏下按钮不参与布局，仍是上下结构。

### 3. 连带调整

- 可读性阈值从 `MIN_READABLE_SCALE=0.62` 提到 **0.78**：压缩上方条目后 1280×800 会变成"整页
  可见但只有 6.1px"，阈值提高后该尺寸改为"适合宽度"（10.1px）。
- 标题条结构改为 `.paper-strip`，`styles.css` 的窄屏选择器同步扩展（`.paper-header` 保留兼容）。

### 实测（1366×768，整页宽 612pt）

| 状态 | 原文区 | canvas | 正文 |
|---|---|---|---|
| 默认（记录栏展开，适合宽度） | 734×525 | 688×934 | 10.99px |
| 收起记录栏 | 1096×525 | 1050×1425 | 16.77px |

其他尺寸：1920×1080 整页可见（9.43px，原文区 77.5% 视口）；1280×800 适合宽度 10.08px；
1600×900 适合宽度 13.26px。

### 测试

```
python -m unittest discover -s tests -p "test_reader_focus.py"   # 新增 1 项，含收起/展开与保存校验
python -m unittest discover -s tests                            # 299 项 OK
python tests/measure_vertical_budget.py --label after            # 上方占用与原文高度
```

---

## 阶段 9 — 独立审查问题的修复

独立审查（见审查报告）提出 1 项阻塞发布、5 项必须修复、若干优化项，本轮全部处理并补了
21 条回归测试（`tests/test_review_fixes.py`）。

| 编号 | 问题 | 修复 |
|---|---|---|
| B1 | 默认整页缩放把正文缩到 4–5 px（1366×768 实测不可读） | `static/app.js`：新增 `MIN_READABLE_SCALE=0.62` 与一次性自动判定。首次渲染按适合页面计算，低于阈值时自动切到"适合宽度"、同步下拉框、在页码下方提示一次；此后使用者的选择优先。实测 1366×768 正文由 4.24 px 提升到 11.02 px |
| M1 | 上一次渲染未取消完成就复用同一张 canvas | `renderPage()` 在创建新渲染前 `await pdfRender.promise`，并用 `renderVersion` 二次校验；快速翻页不再有 canvas 争用 |
| M2 | 防抖锁内与非边界处的滚轮被静默吞掉，可能滚动外层容器 | `onPdfWheel()` 在"不翻页且容器无可滚动内容 / 已到首末页"时显式 `preventDefault()`；页面外壳与笔记栏不再被动滚动 |
| M3 | 答案文件丢失而状态仍为 `ready`，界面无重试入口 | `analysis.py::recover()` 检测 `ready` 但答案文件缺失（含文件名被篡改）→ 转为 `stale`（`error_code=answer_missing`），界面出现"生成 / 重试" |
| M4 | 长论文被静默截断，用户不知模型只看到前一部分 | 新增 `reading_card.build_prompt_body()` 与 `input` 区块：记录实际送入的页码、是否截断、预算与字数；`ready` 消息与 AI 面板顶部都会写明"本次只送入第 X–Y 页"。中文预算 28 000 字、英文 36 000 字 |
| M5 | 上游错误响应体（最多 200 字）被写进文献库并显示 | `providers._first_line()`：只保留首行、最多 120 字，先 `redact()` 再截断；日志仍保留细节 |
| O6 | `run/startup.log` 无上限 | `launch.py` 超过 2 MB 时轮转为 `startup.old.log` |
| 附加 | 证据页码被修正后无审计痕迹 | 证据条目新增 `claimed_page`（被修正时才出现），AI 面板显示"模型写了第 N 页，已按原文修正"；`verification` 新增 `evidence_pages` |

### 受影响的行为变化（测试已同步）

- 1366×768 等小屏默认进入"适合宽度"，因此滚轮"先滚动、到边界再翻页"；
  `test_reader_paging.py` 的滚轮步骤改为在页面边缘施测，并保留大视口下的整页断言。
- 答案 JSON 新增 `input` 区块；`verification` 新增 `evidence_pages`。

### 回归

```
python -m unittest discover -s tests            # 297 项，OK
python tests/audit_legibility.py                # 1366×768 正文 11.02px（原 4.24px）
```

---

## 阶段 8 — 一体化验收与交付文档

**范围**：不新增产品功能，只做端到端验收、样例、截图与文档。

### 新增

| 文件 | 内容 |
|---|---|
| `tests/test_acceptance.py` | 21 例服务端验收：迁移保真、导入分流、按需展示、失败可用、备份还原、Obsidian 与导出、并发冲突、重启恢复、耗时与答案完整性 |
| `tests/test_acceptance_browser.py` | 真实 Chrome 的一体化验收：fit-page + 滚轮 + 键盘 + `lastPage` 恢复 + 拖选批注高亮 + 阅读卡不受影响 + 人工记录刷新后仍在 |
| `tests/capture_phase8_samples.py` | 生成中英文样例分析文件与字段级页码索引 |
| `tests/capture_acceptance_shots.py` | 三窗口截图与布局数字 |
| `tests/build_delivery_docs.py` | 从代码与测试结果生成 `docs/文件清单.md`、`docs/测试记录.md`、`docs/test-run-verbose.txt` |
| `docs/API.md` | 全部接口、参数、状态码与分析状态机 |
| `docs/Provider配置.md` | Ollama / LM Studio / OpenAI 兼容接口、密钥、并发与耗时、常见问题 |
| `docs/迁移与回滚.md` | v1→v2 迁移细节、按阶段回滚表、整体回滚与清理 |
| `docs/已知限制.md` | 扫描件 OCR、伪文本层、模型要求、长论文耗时、校验边界等 |
| `docs/视觉检查.md`、`docs/交付说明.md` | 三窗口截图记录与验收索引 |
| `tests/samples/*.json` | 中文（29 页）与英文（57 页）真实 PDF 的分析文件，13 字段全部可回到物理页码 |

### 验收结果

```
python -m unittest discover -s tests -v      # 276 项，OK（约 522 秒）
```

九项检查全部通过，逐条对应见 `docs/交付说明.md`；完整输出见 `docs/test-run-verbose.txt`。

### 回滚

本阶段只新增测试与文档，不改动应用代码；回滚即删除 `tests/test_acceptance*.py`、
`tests/capture_phase8_samples.py`、`tests/capture_acceptance_shots.py`、
`tests/build_delivery_docs.py` 与 `docs/`、`tests/samples/`。

---

## 阶段 7 — PDF 布局与分页交互（fit-page 默认 + 滚轮翻页）

**范围**：`static/app.js`（缩放与滚轮）、`static/index.html`（缩放选项与页码播报）、新增
`static/reader-layout.css`（PDF 为主体区域的栅格布局）。文本选择、高亮层、批注与 lastPage
保存逻辑全部沿用原实现。

### 缩放

- 默认模式 `page`（下拉首项“适合页面”）：`scale = min(可用宽度/页宽, 可用高度/页高) * userZoom`。
- `width` 保留原“适合宽度”；`125% / 150%` 在**适合页面**的基础上放大，超出部分改为可滚动。
- 整页可见时给 `#pdf-scroll` 加 `page-fits`（`overflow-y:hidden`），消除同页内的无意义滚动；
  放大后自动移除，恢复滚动。
- `fitScale()` 是唯一的缩放计算入口，`renderPage()` 通过它取 scale，`ResizeObserver` 与窗口
  resize 都会重新计算。

### 滚轮翻页

| 规则 | 实现 |
|---|---|
| deltaMode | `deltaMode===1`（行）按 16px、`===2`（页）按容器高度换算 |
| 触控板小位移 | `abs(deltaY) < 14px` 不翻页；横向位移大于纵向时忽略 |
| 防抖锁 | 成功翻页后 600ms 内不再翻页；手势在 260ms 无事件后重置 |
| 一次手势一页 | 一次连续滚轮序列最多前进/后退一页（实测 5 连击只翻 1 页） |
| 边界 | 第 1 页向上、末页向下都不越界 |
| 不误拦 | ctrl/meta/alt/shift、input/textarea/select/contenteditable/dialog 内、正在拖选文本、容器外事件都直接放行 |
| 阻止页面滚动 | 只有确认翻页才 `preventDefault()`（监听器 `{passive:false}`） |
| 反馈 | `#page-announcer`（`aria-live="polite"`）播报“第 N 页，共 M 页”；`goPage()` 继续写 `lastPage` 并 `markDirty()` |

放大后（125%/150%）滚轮先滚动到边界，再由下一次手势翻页。

### 布局

- ≥931px：`.workspace` 改为栅格 `minmax(67%,1fr) + minmax(300px, min(33%,420px))`，
  `grid-template-rows:minmax(0,1fr)` 让两栏都在视口内收缩、各自内部滚动。
  实测 PDF 面板占工作区：1366×768 **67%**、1280×800 **67%**、1920×1080 **72.8%**、1024×768 67%。
- ≤930px：沿用原纵向堆叠（PDF 在上、笔记在下），窄屏仍可读、可滚动。

### 测试（真实 Chrome + CDP）

```
python -m unittest discover -s tests -p "test_reader_paging.py"   # 15 组断言
python tests/measure_layout.py --label after                       # 六种视口的几何与截图
```

证据见 `tests/phase7-paging-report.json`、`tests/phase7-layout.json` 与
`tests/phase7-layout-*.png`：

- 默认 fit-page：canvas 265×360 落在 396px 高的可视区内，`page-fits` 生效，overflow=0。
- 滚轮：连续三次手势依次到第 2/3/4 页；4px 小位移不动；5 连击只翻 1 页；反向一次回到上一页；
  ctrl+滚轮不动。
- 键盘：ArrowRight/ArrowLeft 正常；页码输入 3 生效，输入 9999 夹到末页 29；第 1 页向上不越界、
  `scrollTop` 为 0、上一页按钮禁用。
- 快速翻页：6 次手势到第 7 页，再连按 4 次右箭头到第 11 页，最终 canvas 与 text layer 尺寸一致，
  `page-announcer` 同步。
- 手动 150%：overflow=1008px，滚轮滚动 120→240px 且页码不变；切回“适合页面”overflow 归 0。
- 选择与高亮：在第 2 页拖选文字 → 生成批注 → `#highlight-layer` 出现 1 个高亮块且带百分比矩形。
- resize：1920×1080 / 1280×800 / 1366×768 三种尺寸下 canvas 与 text layer 尺寸始终一致、overflow=0。

### 顺带修掉的问题

1. `.workspace` 作为 flex 子项被内容撑高（body scrollHeight 1218 > 768），PDF 区域实际有 861px
   高，fit-page 因此算错。改用栅格 + `minmax(0,1fr)` 后严格贴合视口。
2. `page-fits` 原先依据渲染后的 `scrollHeight` 判断，时序不稳导致 150% 时仍被 `overflow:hidden`
   锁住。改为用刚渲染出的 viewport 高度判断。
3. `test_analysis_api.wait_status()` 可能在 stage 仍为 `saving` 时提前返回（ready 与 done 之间），
   偶发失败；改为等到终态且 stage 为 `done`。

回滚：`static/app.js`、`static/index.html` 换回阶段 6 快照并删除 `static/reader-layout.css` 即可，
数据层与接口不受影响。

---

## 阶段 6 — 阅读卡的 AI 参考 UI（不改变人工阅读流）

**范围**：`static/app.js`、`static/index.html`，新增 `static/ai-reference.css`。人工阅读卡的
字段、步骤、批注、保存与导出逻辑逐字节保持原样。

### 交互设计

| 要求 | 实现 |
|---|---|
| 导入后只显示状态 | 阅读卡顶部状态条按状态显示"AI 分析已排队 / 等待配置本地模型 / AI 正在生成参考 / AI 参考已就绪 / AI 生成失败 / 需要重新生成"，**不自动取答案** |
| 人工优先 | 每个栏目先渲染人工 `p.fields[k]`，右侧才是"查看 AI 参考"按钮 |
| 点击才请求 | 只有点击按钮（或状态条上的生成/重试/刷新）才调用 `GET /api/analysis/<id>`；打开阅读卡、切换标签、切换论文、刷新页面都不请求 |
| 单栏展开 | `aiState.expanded` 只保存一个字段键；点击即切换，展开时显示答案、依据状态、置信度、证据页码与短引文 |
| 页码跳转 | 证据按钮带 `data-jump`，走现有 `goPage()`，显示"PDF 第 N 页 · 文内 X"，明确区分物理页与文内页 |
| 状态与重试 | 状态条含 chip + 说明 + "生成 / 重试"（failed/stale/cancelled/waiting_for_user）、"重新生成"（ready）与"刷新状态" |
| 独立视觉层级 | 浅绿面板、左侧色条、"AI 参考答案，请回到原文核对。"与顶部免责说明 |
| 复制是显式操作 | "复制到该栏目（追加，不覆盖）"只追加文本，绝不覆盖已有内容，也不改 steps |
| 不污染人工数据 | `aiState` 仅存内存；展开状态不写 `paper.fields` / `steps` / `annotations`，与进度计算无关；切换论文即清空 |
| 降级可用 | 无分析、答案损坏或接口失败时，面板显示原因，阅读卡与批注照常编辑保存 |

### 测试（真实 Chrome + CDP）

```
python -m unittest discover -s tests -p "test_card_browser.py"
```

证据见 `tests/phase6-card-browser.json` 与 `tests/phase6-card-ai-panel.png`：

- 打开页面与打开阅读卡时 `/api/analysis` 请求数 = **0**；点击一个字段后 = **1**（`delta=1`）。
- 13 个栏目 = 13 个人工段落 + 13 个 AI 按钮；点击后 `.ai-panel` 数量 = 1，且只有该字段 `aria-expanded="true"`。
- 切换到另一字段后仍只有 1 个面板，请求数不变。
- 证据页按钮目标 PDF 第 3 页 → `#page-input` 由 1 变 3，页签显示"PDF 第 3 页 · 文内 130"。
- 复制后文本框为"人工复述… ＋ 【AI 参考】…"，steps 仍为 `[false,false,false,false]`，批注数 0。
- 切换论文：面板归零、状态条显示新论文自己的状态，旧答案不出现在 DOM。
- 刷新：人工记录仍在，AI 展开按设计清空；控制台无错误。

### 顺带修掉的测试脆弱点

`test_baseline_docs` 中两条按**行号**定位的断言（app.js:145 / app.js:123）在阶段 4–6 编辑后失效，
已改为按代码锚点断言，并保留"人工字段仍是卡片主体"的检查。

回滚：只涉及三个前端文件；删除 `static/ai-reference.css` 并还原 `app.js`、`index.html` 即可，
后端与数据不受影响。

---

## 阶段 5 — 接入现有本地服务（接口、设置页与安全边界）

**范围**：把阶段 1–4 接到正式 HTTP 接口与设置页；保持 token、Host、Origin、CSP 与本机
访问限制不变。

### 接口

| 接口 | 行为 |
|---|---|
| `POST /api/import` | 保存 PDF 与 paper 后**立即**返回 `paper.analysis.status`（`waiting_for_user` / `queued` / `waiting_for_provider`）；入队由 manager 完成，请求线程不等模型 |
| `GET /api/analysis/<id>/status` | 只返回状态、stage、progress、四个时间戳、attempt、duration、provider/model/prompt_version、`error_code` 与 ≤200 字的 `error_summary`、`has_answer`、`in_queue`、`running_now`。**不含答案正文**，可安全轮询 |
| `GET /api/analysis/<id>` | 只有 `ready` 才返回完整分析；其余状态 `answer=null` 并给出 `reason`。UI 仅在用户点击“查看 AI 参考”时调用 |
| `POST /api/analysis/<id>/generate` | 不带 `force` 为普通重试（`ready` 时返回 `already_ready`，不重复请求模型）；`force=true` 为明确重跑 |
| `POST /api/analysis/<id>/cancel` | 取消排队或运行中的任务 |
| `POST /api/settings` | 一次保存 Obsidian 配置与 `provider / base_url / model / api_key_env / auto_analyze / max_concurrent_jobs`；**拒绝** `api_key` 字段，`api_key_env` 只接受环境变量名，响应与库中都只有变量名 |
| `GET /api/ai-settings` | 只读返回 AI 设置、Provider 名单与健康检查 |

路由安全：`/api/analysis/...` 先对**原始路径**做穿越检测（`..`、`\`，含 `%2e` 形式）再解析，
`../library`、`%2e%2e%2flibrary` 一律 400，未知子资源 404。

### 设置页（`static/index.html` + `static/app.js`）

新增“AI 参考分析（可选）”区块：Provider/模型/地址/密钥变量名/自动排队开关、保存与重新检查
按钮、状态行与最近一次任务详情。页面明确说明四件事：当前 Provider 是否可用；未配置时导入仍
可阅读但 AI 状态为“等待配置”；本机模型与外部兼容接口的数据边界；强制重跑的时间与费用影响。
`使用说明.md` 同步新增“AI 参考分析（可选，默认关闭）”一章。

### 测试结果

```
python -m unittest discover -s tests -v          # 252 项，约 140 s，OK
tests\api_smoke.cmd <base> <token> <paper>       # curl 真实 HTTP 检查
python tests\fail_path_smoke.py --base <url> --paper <id>
```

`tests/phase5-curl-report.txt`（成功路径）：错误 token → 403；未知论文 → 400；编码穿越 → 400；
未就绪取正文 → `answer=null`；generate → queued → running → ready；不带 force 不再调用模型；
带 force 重新排队；设置保存成功且回显为**新值**；夹带 `api_key` → 400。
`tests/phase5-curl-failure-report.txt`（失败路径）：校验失败 → `failed`，状态接口只给摘要，
正文接口不给答案，修复后可再次重跑。

导入耗时与模型耗时解耦：模型故意延迟 3 s 时，`/api/import` 仍在 1.5 s 内返回且 Provider
调用次数为 0（`test_import_time_does_not_follow_model_time`）。

密钥隔离：设置 `PAPER_READER_PHASE5_KEY` 后跑完整流程，`data/` 下所有文件、备份 ZIP 的每个
成员、事件日志与首页 HTML 都不含密钥原文（`SecretIsolation`）。

### 本阶段修掉的两个真实缺陷

1. `POST /api/settings` 原先在写入 AI 字段**之前**就取了 `settings` 快照，响应里的
   `settings.ai` 是旧值（curl 检查发现）。
2. `force` 重跑时 `completed_at` 未清空，会显示上一次的完成时间（状态接口发现）。

回滚：只改了 `server.py`、`static/index.html`、`static/app.js`、`使用说明.md`；把四个文件换回
阶段 4 快照即可，数据与 `analyses/` 不受影响。

---

## 阶段 4 — 可核对的阅读卡（prompt_version = reading-card-v1）

**范围**：定义完整 JSON Schema、系统提示词、严格校验与一次修复；只有通过校验的
内容才会写成 `data/analyses/<paper_id>.json` 并进入 `ready`。未接真实模型，未改动
`static/*`（阅读卡 UI 与线上逐字节一致）。

### 修改的文件

| 文件 | 变更 |
|---|---|
| `reading_card.py`（新） | `PROMPT_VERSION="reading-card-v1"`；`SYSTEM_PROMPT`（7 条硬规则）、用户提示词、修复提示词；`validate_reading_card()` 全量校验与归一化；错误码表 |
| `quotes.py`（新） | 引文核对：`PageTextIndex.find/find_anywhere`（容忍空白、全半角、连字与 CID 抽字插空）、`normalise()` |
| `analysis.py` | 改为调用 `reading_card` 的提示词与校验；新增 `_accept_or_repair()`（**恰好一次**修复请求）与 `_record_failure()`（写失败日志） |
| `providers.py` | `AdapterResult` 增加 `raw_text` / `repair_text`，失败日志可记录模型原始输出 |
| `storage.py`、`server.py` | `analyses/<id>.failed.json` 的原子写入与 `Store.write_failed_answer()` |
| `tests/card_fixtures.py`（新） | 从真实页文本构建合法阅读卡的共享夹具 |
| `tests/test_reading_card.py`、`tests/test_reading_card_jobs.py`（新） | 校验规则与端到端门禁测试 |
| `tests/capture_reading_card.py`（新） | 阶段 4 样例（合法卡、失败日志、一次修复） |

### Schema（写入 `data/analyses/<paper_id>.json`）

```json
{
  "schema": 1, "schema_version": 1,
  "paper_id": "medical-2026-zang",
  "prompt_version": "reading-card-v1",
  "provider": "ollama", "model": "qwen2.5:7b", "language": "zh",
  "source_sha256": "cc262f…", "generated_at": "2026-10-10T08:45:39Z",
  "created_at": "2026-10-10T08:45:39Z",
  "overall": {"title": "…", "language": "zh", "paper_type": "empirical",
              "main_claim": "…", "conclusion": "…", "confidence": "high|medium|low"},
  "answers": {
    "summary": {"status": "found|partial|not_found", "answer": "…",
                "evidence": [{"page": 1, "quote": "…", "verified": true, "similarity": 1.0}],
                "confidence": "high|medium|low", "causal_strength": "causal|correlational|descriptive|unclear",
                "verified_quotes": 1},
    "…13 个字段：question motivation claim data variables method evidence robustness
     boundaries assessment summary connections next…"
  },
  "terms": [{"term": "…", "explanation": "…", "page": 1}],
  "reader_questions": ["…"], "warnings": ["…"],
  "verification": {"claimed_quotes": 13, "verified_quotes": 13, "corrected_pages": 5,
                   "corrections": ["variables:3->1"], "page_count": 29,
                   "field_count": 13, "not_found_fields": []}
}
```

### 校验规则与错误码

| 错误码 | 规则 | 是否阻止 ready |
|---|---|---|
| `bad_json` | 模型输出不是合法 JSON（含代码围栏容忍） | 是 |
| `not_an_object` | 顶层不是 JSON 对象 | 是 |
| `field_completeness` | 13 个字段必须齐全 | 是 |
| `unknown_field` | 出现 13 个字段以外的键 | 是 |
| `answer_shape` | 枚举值（status/confidence/causal_strength/language）或类型错误 | 是 |
| `missing_answer_text` | found/partial 的 answer 为空；not_found 未写"原文未说明" | 是 |
| `length_limit` | answer≤4000、quote≤200、overall≤2000、terms≤40、questions≤20、warnings≤20 | 是 |
| `page_range` | 页码非 1..page_count 的整数 | 是 |
| `quote_missing` | 引文在原文中找不到（含引文过短、found/partial 无引文） | 是 |
| `evidence_unverified` | 可核对引文 <4 条，或核对率 <60% | 是 |
| `causal_overreach` | 用了因果动词或 `causal_strength=causal`，但引文没有因果识别依据 | 是 |
| `not_found_overreach` | 把"未发现证据"写成"证明不存在" | 是 |
| `empty_card` | 13 个字段全无依据 | 是 |
| `page_corrected` | 引文真实但页码写错，已按实际页码修正并记入 `verification.corrections` | **否**（已修正） |

修复流程：首次校验失败 → 携带错误码与错误详情向模型发**一次**修复请求 → 再校验；
仍失败即 `failed`，并把原始输出写入 `data/analyses/<paper_id>.failed.json`，
**不产生**任何 answer 文件、不展示未校验文本。失败样例（缺字段、坏 JSON、越界页码、
伪造引文、因果过度陈述）均有测试证明无法进入 `ready`。

### 测试结果

```
python -m unittest discover -s tests -v     # 210 项，约 57 s，OK
python tests/capture_reading_card.py        # 见 tests/phase4-reading-card-sample.json
```

实测（FakeProvider，引文取自真实论文文本层）：合法卡 `ready`（13 字段、13 条引文全部核对、
5 处页码自动修正）；伪造引文 + 因果过度陈述 → `failed`，answer 文件不存在，失败日志含
`causal_overreach / quote_missing` 与模型原始输出；缺字段 → 修复一次 → `ready`；
修复也失败 → `failed`。

### 回滚方法

阶段 4 只新增 `reading_card.py`、`quotes.py` 与测试：删除这两个模块、把 `analysis.py`
换回阶段 3 快照即可。`analyses/` 下的答案与失败日志可整目录删除，主库 `library.json`
中的 `analysis` 元数据保持兼容。

---

## 阶段 3 — Provider 适配器与后台分析任务（FakeProvider 驱动，不调用真实模型）

**范围**：Provider 适配器接口与三种实现、`AnalysisManager` 后台队列、状态机、重启恢复、
幂等检查、状态接口。**没有**接真实模型调用（默认 `provider=none`），**没有**改动
`static/*`（阅读卡 UI 与前端逐字节未变）。

### 修改的文件

| 文件 | 变更 |
|---|---|
| `providers.py`（新） | `ProviderAdapter` 接口（`health_check` / `complete_json` / `cancel` / `list_models` / `describe`）、`DisabledAdapter`、`OllamaAdapter`（`/api/chat` + `format=json`）、`OpenAICompatibleAdapter`（`/v1/chat/completions`）、`FakeProvider`（success / timeout / invalid_json / refused / error）、`CompletionRequest`、错误分类、`redact()`、`resolve_api_key()`、`build_adapter()` |
| `analysis.py`（新） | `AnalysisManager`：单 worker + 受控并发（信号量按 `max_concurrent_jobs`）、状态机、重启恢复、幂等检查、stale 判定、取消、提示词与结构校验、每次状态变化原子持久化 |
| `schema.py` | `analysis` 新增 `stage/progress/message/updated_at/adapter/duration_ms/attempt/language`；状态新增 `waiting_for_user/waiting_for_provider/stale/cancelled`；**迁移补齐旧记录缺失的新字段**（幂等） |
| `storage.py` | `credentials.json` 读取（可选凭据存储，键不在库/备份/日志中） |
| `server.py` | `AnalysisManager` 接线与 `--no-worker`；`/api/import` 记录任务并入队（不等待模型）；`GET /api/analysis`、`POST /api/analysis-run`、`POST /api/analysis-cancel`；`/api/ai-settings` 返回健康检查并唤醒等待中的论文；日志只写 provider/model/耗时/错误类别 |
| `tests/test_analysis_jobs.py`、`tests/test_analysis_api.py`（新） | 状态机、恢复、幂等、并发隔离、适配器载荷形状、HTTP 端到端 |
| `tests/capture_jobs_baseline.py`（新） | 阶段 3 基线（FakeProvider） |

### 状态机

```
导入 PDF
   │
   ├─ 未配置 Provider ──────────► waiting_for_provider ──(配置 Provider)──► queued
   ├─ auto_analyze = false ─────► waiting_for_user ──(点击“查看 AI 参考”)──► queued
   └─ auto_analyze = true ───────────────────────────────────────────────► queued
                                                                             │ worker
                                                                             ▼
                                                                         running
                                                                             │
       ┌─────────────────────────────────────────────────────────────────────┤
       ▼                     ▼                    ▼                ▼         ▼
    ready                 failed             cancelled      waiting_for_provider  queued(重试)
   (答案已存)        (含错误类别与建议)      (用户取消)        (运行中 Provider 消失)  (瞬时错误重试1次)

ready ──(source_sha256 / prompt_version / model / language 变化)──► stale
running ──(服务重启或超过 900s)──► queued（可安全重试）
```

`running` 内部阶段：`extracting → chunking → requesting → validating → saving → done`，
progress 15/30/55/80/95/100，每次变化都原子落盘（`library.json` + `library.previous.json`）。

### 测试结果

```
python -m unittest discover -s tests -v      # 165 项，约 31 s，OK
python tests/capture_jobs_baseline.py        # 见 tests/phase3-jobs-baseline.json
```

端到端证据（FakeProvider，模型故意 delay 1.2 s）：

| 指标 | 值 |
|---|---|
| `/api/import` 返回耗时 | **0.111 s** |
| 导入返回时的状态 / Provider 调用次数 | `queued` / **0** |
| 后台完成状态 / 阶段 / 进度 | `ready` / `done` / 100 |
| 分析总耗时 | 1404 ms（含模型延迟） |
| 答案文件字段数 | 13 |
| 落盘状态 | `ready` |

### Provider 配置说明

```json
"ai": {
  "provider": "ollama",                  // none | ollama | lmstudio | openai_compatible
  "base_url": "http://127.0.0.1:11434",  // 留空用默认端口
  "model": "qwen2.5:7b",
  "api_key_env": "OLLAMA_API_KEY",       // 只允许环境变量名，不允许密钥本身
  "auto_analyze": false,                 // true = 导入后自动排队；默认等用户点击
  "max_concurrent_jobs": 1               // 1–4
}
```

* Ollama：`GET /api/tags` 健康检查，`POST /api/chat` + `format=json`。
* LM Studio / vLLM / llama.cpp / LocalAI / Jan：`GET /v1/models` + `POST /v1/chat/completions`
  + `response_format=json_object`。
* 密钥来源只有两处：`api_key_env` 指定的环境变量，或 `data/credentials.json`
  （`{ENV_NAME: key}`，不进备份、不进库、不进日志）。
* 日志每行一个 JSON：`provider`、`model`、`duration_ms`、`category`，经 `redact()` 处理。

### 回滚方法

阶段 3 只新增文件并扩展 `schema.py`/`server.py`：删除 `providers.py`、`analysis.py`、
`tests/test_analysis_jobs.py`、`tests/test_analysis_api.py`，把 `server.py` 换回阶段 2 快照，
并把 `library.json` 中每篇的 `analysis` 恢复为阶段 2 的字段集合即可（或直接用
`backups\phase0-*\` 与出厂库覆盖）。`analyses/` 里的答案文件可整目录删除。

---

## 阶段 2 — 可验证的论文文本输入（不调用模型）

**范围**：建立 PDF 文本抽取、保守清洗、章节切块与抽取缓存。未实现任何 Provider，未发起模型
调用，未改动 `static/*`（阅读卡 UI 与前端逐字节未变）。

### 修改的文件

| 文件 | 变更 |
|---|---|
| `pdftext.py`（新） | `PdfTextExtractor.extract(path) -> pages` 的数据层：三个后端（`pdftotext` → `builtin` → `pypdf`）、`Page`/`ExtractionResult`、物理页码、`text_quality`/`meaningful_chars`/`is_junk_text` 判定、`ExtractionError(code, message, action)` |
| `textclean.py`（新） | 保守清洗：连字/不可见字符/空白/破折号/引号归一化（NFC，保留上下标）、可选连字符合并、重复页眉页脚与页码识别，全部改动计数上报 |
| `chunker.py`（新） | `Chunker(max_chars, overlap_ratio)`、章节识别、`Chunk(start_page, end_page, chunk_id, text)`、`page_of_offset()` 页码回映 |
| `extraction.py`（新） | 门面：后端选择、`data/extracted/<paper_id>.json` 缓存读写与校验（source_sha256 + 抽取器版本 + 页数 + 连续物理页码）、`clean()`、`chunks()`、失败时返回可执行建议 |
| `tests/pdf_fixtures.py`（新） | 标准库生成 5 份合成 PDF（含真实 Identity-H 字体 + ToUnicode CMap），覆盖中/英/双栏/空白/扫描件 |
| `tests/test_pdftext.py`、`tests/test_chunk_cache.py`（新） | 阶段 2 测试 |
| `tests/capture_extraction_baseline.py`（新） | 抽取基线采集 |
| `tests/README.md` | 更新阶段 2 测试计划 |

### 抽取数据结构

```json
{
  "pdf_page": 1,            // 物理 PDF 页码，从 1 开始，绝不用文内页码
  "text": "……",
  "char_count": 1531,
  "width": 595.28, "height": 841.89,
  "has_text": true, "has_images": false,
  "warnings": []
}
```

`ExtractionResult`：`path / source_sha256 / backend / extractor_version / page_count / pages /
code / message / warnings / cached`；`code ∈ {ok, no_text_layer, encrypted_pdf, broken_pdf, error,
no_backend, missing_file, unreadable, timeout}`。

### 缓存格式（`data/extracted/<paper_id>.json`）

```json
{"schema": 1, "source_sha256": "…", "extractor": {"name": "pdftotext", "version": "pdftotext version 25.06.0"},
 "page_count": 29, "created_at": "…Z", "code": "ok", "message": "…", "pages": [ … ]}
```

命中条件（全部满足）：schema 相符、`source_sha256` 相符、`extractor.version` 相符、
`pdfinfo` 的页数相符、自述页数与实际页数相符、页码为 1..N 连续。任一不符即重新抽取并覆盖。

### 测试结果

```
python -m unittest discover -s tests -v     # 111 项，约 17 s，OK
python tests/capture_extraction_baseline.py # 真实三篇：见 tests/phase2-extraction-baseline.json
```

| 论文 | backend | 页数 | 有文本页 | 二次抽取 | 切块 | 页码回映失败 |
|---|---|---|---|---|---|---|
| fertility-2026-coffey | pdftotext | 57 | 57 | 缓存命中 0.02 s | 215 | 0 |
| medical-2026-zang | pdftotext | 29 | 29 | 缓存命中 0.02 s | 43 | 0 |
| finance-2021-zhang | pdftotext | 15 | 15（1 页文字过少） | 缓存命中 0.02 s | 42 | 0 |

内置后端（无外部命令时的兜底）在同一批文件上的表现：medical 29/29 页、65830 字符、
质量 0.917；fertility 57/57 页、94028 字符、质量 0.972；finance 0 页文本（扫描件）。

### 回滚方法

阶段 2 只新增文件，不迁移数据：删除 `pdftext.py`、`textclean.py`、`chunker.py`、`extraction.py`
与 `data/extracted/` 即可回到阶段 1 状态；主库 `library.json` 与 PDF 未受影响。

---

## 阶段 1 — 数据层与文件存储（schema v1 → v2）

**范围**：只做数据层与文件存储。未实现任何 Provider，未发起任何模型调用，未改动 UI
（`static/*` 与 `launch.py` 逐字节未变）。

### 修改的文件

| 文件 | 变更 |
|---|---|
| `schema.py`（新） | schema 版本常量、`settings.ai` 与 `paper.analysis` 契约与校验、迁移规则表 `MIGRATIONS`、`migrate_state()` |
| `storage.py`（新） | `DataStore`（papers/analyses/extracted 的路径与读写）、原子写入、JSON 读取与大小上限、文件名/编号/路径穿越校验 |
| `server.py` | 启动时迁移并保留迁移前副本；`analysis` 校验接入 `validate_paper`；`EDITABLE_KEYS` 白名单固化（`analysis` 不在其中）；分析读写与 AI 设置方法；`GET/POST /api/ai-settings`、`GET /api/analysis/<id>`；备份纳入 `analyses/`、`extracted/` 与 `manifest.json`；恢复兼容 v1 备份；健康检查上报 `version` 与 `schema_version` |
| `tests/test_migration.py`（新） | 迁移幂等、阅读数据保全、默认值、边界与拒绝规则 |
| `tests/test_storage.py`（新） | 路径安全、原子写入中断、并发写、大小上限、缓存往返 |
| `tests/test_backup_restore.py`（新） | 备份内容与清单、跨版本恢复、旧备份、恶意归档、恢复后阅读卡可用 |
| `tests/test_api_v2.py`（新） | v2 状态、AI 设置读写与拒绝、分析只读元数据、`/api/save` 不能写 AI 元数据、备份/恢复接口、健康版本 |
| `tests/test_baseline_docs.py`、`tests/test_phase0_readonly.py` | 基线锚点更新为 v2（fixture 仍为 v1，用于验证迁移） |
| `data/library.json`（工作副本） | 已实际迁移到 v2；`data/library.previous.json` 保留出厂 v1 字节 |

### 迁移规则 v1 → v2

1. `settings.ai` 缺失时补齐默认值：`provider=none`、`base_url=""`、`model=""`、`api_key_env=""`、
   `auto_analyze=false`、`max_concurrent_jobs=1`；已存在的键保留，缺失的键补齐，未知键报错。
2. 每篇 `paper.analysis` 缺失时补齐：`status=none`、`source_sha256=""`、`prompt_version=""`、
   `provider=""`、`model=""`、`requested_at=""`、`started_at=""`、`completed_at=""`、
   `error_code=""`、`error_message=""`、`answer_file=""`。
3. 启动时若论文有 PDF 且 `source_sha256` 为空，写入该 PDF 的 SHA-256；已有值不覆盖。
4. 幂等：第二次加载 `migrated=false` 且 `library.json` 字节不变。
5. 迁移绝不开启 AI：`provider` 保持 `none`、`auto_analyze` 保持 `false`、不生成 `pending` 任务、
   不写入任何 `data/analyses/` 内容。
6. `settings` 的 Obsidian 配置、每篇论文的 `fields/steps/annotations/lastPage/pageOffset/
   pageLabels/revision/created/updated/note_name` 原样保留。

### 测试结果

```
python -m unittest discover -s tests -v      # 64 项，约 13 s，OK
```

覆盖的四项要求：迁移两次不改数据（`test_migration_is_idempotent`）、迁移前后
`fields/steps/annotations` 逐字节一致（`test_reader_data_is_preserved_exactly`）、
写入中断不破坏原文件（`test_interrupted_write_keeps_the_previous_file`）、
旧备份恢复后阅读卡仍可用（`ReadingCardStillOpens`）。

两条额外的安全性结论（已实测）：

- 连续跑两轮完整测试后，工作副本的 `data/library.json` 与三份 PDF 哈希不变；
  `data/analyses/`、`data/extracted/` 仍为空——测试只读真实数据。
- v1 夹具已固化为 `tests/fixtures/library-v1.json`（SHA256 前缀 `3436614B83FF12E1`，
  与出厂库逐字节一致）。**不要**再用活的 `data/library.json` 当迁移夹具：它已被迁移为 v2。

### 回滚方法

```powershell
# 1) 停服务（设置 → 保存并退出软件，或结束运行中的 server.py 进程）
# 2) 用出厂 v1 库覆盖（两份都可）：
Copy-Item 'D:\work\常用软件\论文研读助手-本地版\论文研读助手\data\library.json' `
          'D:\harness home\paper-reader\data\library.json' -Force
# 3) 把代码回退到 v1（备份里只有 data/ 与源码快照，见 backups\phase0-*\src）
```

v2 代码可以读取 v1 库（会自动迁移），但 **v1 代码读不了 v2 库**，所以回滚时必须同时回退
`library.json` 与三个新模块（`schema.py`、`storage.py` 及 `server.py`）。

---

## 阶段 0 — 勘察与基线

- 建立 `tests/` 只读测试框架与 `tests/baseline-ui.json`（PDF 面板几何、默认缩放、滚轮行为、阅读卡字段来源）。
- 备份：`backups\baseline-source-*.zip`（原始发布包副本）、`backups\baseline-data-*\`（库+PDF）、
  `backups\phase0-*\`（库+PDF+源码快照）。
- 记录 `/api/save` 载荷契约：未知顶层键被静默忽略且修订号仍递增（假成功），人工字段必须写在
  `patch.fields` 下。
