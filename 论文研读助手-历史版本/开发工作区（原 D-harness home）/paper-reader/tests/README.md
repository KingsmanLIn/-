# tests/ — 测试、基线与验收证据（阶段 0 建立，阶段 1–8 扩充）

## 运行方式

```powershell
cd <项目根>
python -m unittest discover -s tests -v            # 全部 276 项，约 9 分钟

# 单个模块
python -m unittest discover -s tests -p "test_migration.py" -v

# 浏览器类测试需要 Chrome + Node 22（全局 WebSocket）
python -m unittest discover -s tests -p "test_reader_paging.py"
python -m unittest discover -s tests -p "test_acceptance_browser.py"

# 采集 / 刷新证据
python tests/measure_layout.py --label after        # 六种视口的布局数字与截图
python tests/capture_acceptance_shots.py            # 三窗口截图
python tests/capture_phase8_samples.py              # 中英文样例分析文件
python tests/build_delivery_docs.py --run-tests     # 重新生成 docs/ 交付文档
```

## 安全约束（所有阶段都适用）

1. 测试**不写**用户的 `data/library.json`、`data/papers/`；需要写入的测试一律把库复制到临时目录，
   再用 `baseline_helpers.local_service(data_dir=...)` 或进程内 `server.Store(tmp)` 起一次性服务。
2. 原始发布包 `论文研读助手-本地版.zip` 只读取校验，永不写入（SHA256 未变）。
3. `capture_baseline.py` 会往**临时副本**里注入 `ZZMARK-*` 标记值以证明阅读卡字段来源；
   这些标记不会进入真实库。
4. 抽取缓存写入临时 `data/extracted/`，脚本结束即删除。
5. 连续多轮全量测试后，工作副本 `data/library.json` 与 `data/papers/*.pdf` 的哈希不变。

## 文件

| 文件 | 作用 |
|---|---|
| `baseline_helpers.py` | 只读工具：`local_service` 一次性服务、token 抓取、冻结 v1 库、源码定位 |
| `card_fixtures.py` | 从真实页文本构建合法阅读卡（含 `not_found`、因果标注、变异钩子、`pages_from_prompt`） |
| `pdf_fixtures.py` | 用标准库生成合成 PDF：中文页眉页脚与负号/百分比表格、英文断词、双栏、空白、纯图像扫描件 |
| `fixtures/library-v1.json` | **冻结的出厂 v1 库**（SHA256 `3436614B83FF12E1`），迁移类测试只用它 |
| `test_baseline_docs.py` | 阶段 0：文档与实现位置锚点、`renderCard` 字段来源、fit-page 实现位置 |
| `test_phase0_readonly.py` | 阶段 0：线上安装只读、`/api/save` 载荷契约、revision 409 |
| `test_migration.py` | 阶段 1：v1→v2 迁移幂等、阅读数据保全、默认值、拒绝规则 |
| `test_storage.py` | 阶段 1：路径穿越校验、原子写入中断、并发写、大小上限、错误信息不泄漏 |
| `test_backup_restore.py` | 阶段 1：备份清单与内容、跨版本恢复、恶意归档 |
| `test_api_v2.py` | 阶段 1/5：v2 状态、AI 设置读写、分析元数据只读、`/api/save` 白名单 |
| `test_pdftext.py` | 阶段 2：中/英/双栏/空白/扫描件抽取、物理页码、数字与单位保全、伪文本层判定 |
| `test_chunk_cache.py` | 阶段 2：切块 id/页码/重叠/章节、块页码回映、缓存命中与失效 |
| `test_analysis_jobs.py` | 阶段 3/4：状态机、重启恢复、stale、幂等、并发隔离、适配器错误分类、密钥脱敏 |
| `test_analysis_api.py` | 阶段 3/5：HTTP 端到端——导入立即返回、手动运行、取消、失败重试、后台 worker |
| `test_reading_card.py` | 阶段 4：提示词规则、Schema、14 条错误码、页码修正、失败样例 |
| `test_reading_card_jobs.py` | 阶段 4：端到端门禁——伪造引文/缺字段/坏 JSON/越界页码/因果过度全部 failed，一次修复 |
| `test_service_api.py` | 阶段 5：六个接口的成功/未授权/未知论文/未就绪/失败/重试，CSP 与 Host/Origin 限制，密钥隔离，Obsidian 与 `/api/save` 回归 |
| `test_card_browser.py` | 阶段 6：真实 Chrome——打开阅读卡不请求答案，点击单栏只请求一次，证据跳页，显式复制 |
| `test_reader_paging.py` | 阶段 7：fit-page、滚轮/触控板/键盘/页码输入/边界/快速翻页/手动放大/选择批注/resize |
| `test_acceptance.py` | 阶段 8：九项一体化验收（服务端 21 例） |
| `test_acceptance_browser.py` | 阶段 8：PDF 行为 + 页码恢复 + 批注高亮 + 阅读卡不受影响（真实 Chrome） |
| `measure_layout.py` | 阶段 7/8：六种视口的布局数字与截图 |
| `serve_with_fake_provider.py` | 阶段 5：带假 Provider 的临时服务（供 curl 检查使用） |
| `api_smoke.cmd`、`fail_path_smoke.py`、`summarise_curl_report.py` | 阶段 5：curl 成功/失败路径与报告汇总 |
| `capture_baseline.py` / `baseline-ui.json` | 阶段 0/1：UI 回归基线 |
| `phase1-dom-inventory.json` | 阶段 1：控件清单，证明数据层改动未影响前端 |
| `capture_extraction_baseline.py` / `phase2-extraction-baseline.json` | 阶段 2：抽取基线 |
| `capture_jobs_baseline.py` / `phase3-jobs-baseline.json` | 阶段 3：任务基线 |
| `capture_reading_card.py` / `phase4-reading-card-sample.json` | 阶段 4：样例卡、失败日志、修复流程 |
| `phase5-curl-report.txt`、`phase5-curl-summary.json`、`phase5-curl-failure-report.txt` | 阶段 5：curl 原始输出 |
| `phase6-card-browser.json`、`phase6-card-ai-panel.png` | 阶段 6：浏览器证据与截图 |
| `phase7-paging-report.json`、`phase7-layout.json`、`phase7-layout-*.png`、`phase7-fit-page.png` | 阶段 7：分页报告、布局数字与截图 |
| `capture_phase8_samples.py` / `samples/` | 阶段 8：中英文样例分析文件与字段级页码索引 |
| `capture_acceptance_shots.py` / `phase8-window-*.png` | 阶段 8：三窗口截图 |
| `test_review_fixes.py` | 阶段 9：独立审查问题的回归（B1 缩放阈值、M1 渲染取消、M2 滚轮边界、M3 答案文件缺失、M4 输入截断、M5 上游响应体、O6 日志轮转） |
| `audit_fields.py` | 审查工具：13 字段在校验器/前端/样例各层是否一致 |
| `audit_ai_safety.py` | 审查工具：前端写字段位置、ready 后重启是否重复生成、无 Provider 是否发起请求、日志是否含正文 |
| `audit_provider_http.py` | 审查工具：本机 HTTP stub 验证真实 Ollama / OpenAI 兼容适配器链路 |
| `audit_reader_visual.py` | 审查工具：锁内滚轮、翻页时外壳滚动、快速翻页与 resize 一致性 |
| `audit_legibility.py` | 审查工具：各论文在各视口下的正文字号（判断默认缩放是否可读） |
| `audit_safety_http.py` | 审查工具：401 回显密钥、恢复幂等、备份成员与凭据 |
| `audit_stale_paths.py` | 审查工具：答案文件缺失/文件名被篡改时的状态 |
| `audit_scan_path.py` | 审查工具：扫描件走真实抽取器时的失败分支 |
| `audit_integrity.py` | 审查工具：产品文件哈希是否与 `docs/文件清单.md` 一致、线上安装是否只读 |
| `audit_recheck.py` / `audit-recheck.json`、`audit-legibility.json` | 复审：重跑全部审查探针并汇总 |
| `build_delivery_docs.py` | 阶段 8：从代码与测试结果生成 `docs/文件清单.md`、`docs/测试记录.md` |
| `phase8-reader-report.json`、`phase8-window-shots.json` | 阶段 8：浏览器证据与窗口数字 |

## 阶段 1–8 测试计划与判据

| 阶段 | 关键测试 | 通过判据 |
|---|---|---|
| 1 数据层 v2 | `test_migration.py`、`test_storage.py`、`test_backup_restore.py`、`test_api_v2.py` | 迁移两次幂等；`fields/steps/annotations/lastPage/revision` 逐字节保留；中断写入不破坏原文件；旧备份恢复后阅读卡可用 |
| 2 文本输入 | `test_pdftext.py`、`test_chunk_cache.py` | 物理页码从 1 连续；扫描件与伪文本层判为 `no_text_layer`；数字/负号/百分比/单位不变；每个 chunk 的页码能回映原文；二次抽取命中缓存、替换后不命中 |
| 3 后台任务 | `test_analysis_jobs.py`、`test_analysis_api.py` | 导入立即返回且 Provider 调用为 0；四种 FakeProvider 结果各自落到正确状态；`running` 重启后回到 `queued` 并跑到 `ready`；重复提交不重复调用；人工保存不被 worker 覆盖 |
| 4 阅读卡校验 | `test_reading_card.py`、`test_reading_card_jobs.py` | 13 字段齐全；引文可核对（≥4 条、≥60%）；缺字段/坏 JSON/越界页码/伪造引文/因果过度均阻止 `ready`；一次修复；失败写 `.failed.json`；AI 文本不进 `paper.fields` |
| 5 服务接入 | `test_service_api.py`、`api_smoke.cmd`、`fail_path_smoke.py` | 导入立即返回 `analysis.status`；状态接口不含正文；正文只在 `ready` 返回；generate 支持重试与 force；设置保存 AI 字段且绝不回显密钥；token/Host/Origin/CSP 不变；Obsidian 与 `/api/save` 回归通过 |
| 6 阅读卡 AI 参考 | `test_card_browser.py` | 打开阅读卡与切换论文不请求答案；点击单栏才请求一次并只展开该栏；证据按物理页跳转走 `goPage`；复制为显式追加；AI 展开不写入 `fields/steps/annotations`；无分析或接口失败时人工记录照常可编辑 |
| 7 PDF 布局与分页 | `test_reader_paging.py`、`measure_layout.py` | 默认 fit-page 整页可见且同页不滚动；滚轮一次手势一页并遵守防抖与边界；放大后可滚动、边界处才翻页；选择/高亮/批注与 `lastPage` 不受影响；1366×768 下 PDF 面板占工作区 ≥65%；≤930px 仍为可读的纵向布局 |
| 8 一体化验收 | `test_acceptance.py`、`test_acceptance_browser.py` | 九项检查全部通过，见 `docs/交付说明.md` |
