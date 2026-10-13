# 1.3.1 AI 安全升级 — 独立审查与整改报告

本文件记录**阶段 7（独立发布前安全审查）**及其整改。审查由独立子代理在独立上下文中执行、只读、未复用实施者结论；本文件由实施方在收到审查报告后编写，逐条对账。

审查基线：`D:\harness home\paper-reader`（工作副本）
审查方实测修订：`analysis.py 0C61D9ADF6804551`、`providers.py 68ACA54252432AD1`、`reading_card.py B7323060CC31A670`、`server.py 44D9E655825F1D7E`、`static/app.js 4E03CEE56F42AA29`、`secret_guard.py 5265E34BECEC9AAB`、`job_policy.py A242EA99BBEDB29D`、`secure_transport.py E8886C345448B65E`

---

## 1 审查结论与整改状态

| 审查判定 | 项 | 整改 |
|---|---|---|
| 阻塞发布 | 无 | — |
| 必须修复 | 1. 晚到/被取代代次覆盖新答案，且同篇可并发二次计费 | ✅ 已修复并加回归测试 |
| 必须修复 | 2. 暂停/停用/未批准时仍发带 Key 的健康检查与模型列表 | ✅ 已修复并加回归测试 |
| 必须修复 | 3. 近似引文在界面与逐字引文无法区分 | ✅ 已修复（界面标记 + 提醒 + 样式） |
| 必须修复 | 4. 文档 4 处承诺与实现不符 + 1 个不存在的功能 | ✅ 文档已改（实现或删除承诺） |
| 必须修复 | 5. 安全套件在被审修订上是红的 | ✅ 该断言是我在有提示词增补后漏改的测试；已同步并全绿，见 §3 |
| 可后续优化 | 死代码（`approve_target`/`target_approved`/`outbound.json`） | ✅ 已接入生产路径（保存目标即确认），不再是死代码 |
| 可后续优化 | `CompletionRequest.FORBIDDEN_KEYS` 不可达守卫 | ✅ 已删除，改为"类型形状即守卫"的测试 |
| 可后续优化 | 自适应重发不计费 | ✅ 重发现在各自占用额度，额度不足即不重发 |
| 可后续优化 | 修复提示词超输入预算 | ✅ 坏 JSON 与正文共享同一预算（`REPAIR_JSON_SHARE`） |
| 可后续优化 | 代理变量无生产调用者 | ✅ 文档改为"直连、忽略代理"，不再承诺报告 |
| 可后续优化 | 用量/价格累计估算无实现 | ✅ 文档改为"只按次数记账，金额以服务商控制台为准" |
| 可后续优化 | 诊断包功能不存在 | ✅ 文档删除该表述，报告改标"不适用" |
| 可后续优化 | 重试 reservation 记成 `kind="first"` | ⏳ 未改（仅影响账本可读性；重试本身有独立 reservation） |
| 可后续优化 | `verify_analysis` 不强制 `source_sha256` 存在 | ⏳ 未改（缺字段视为旧格式并降级为"未验证"，不透传信任） |
| 可后续优化 | `session.BOOTSTRAP_USED` 不可达 | ⏳ 未改（无行为影响） |
| 可后续优化 | `truncated` 误报（页不连续） | ✅ 界面文案改为"本次只送入了原文的一部分"，不再声明具体页码区间 |
| 可后续优化 | `X-Reader-Token` 遗留通道未删除 | ⏳ 保留（迁移期兼容；页面已不再分发该令牌） |

---

## 2 逐条问题的整改证据

### 修复 1：晚到代次不得覆盖新答案

审查给出可复现步骤：并发 ≥2、首调用阻塞、飞行中 force → 2 次 POST、旧答案覆盖新答案。

根因（审查定位正确）：`decide_send` 只在**发送前**查 `job_is_current`，而写答案与写状态不查。实测中还发现第二层原因：旧任务在**后**完成的 `validating` 状态会覆盖新任务的 `ready`。

整改（`analysis.py`）：
- `_superseded(paper_id, context)`：统一的"我是否已被取代"判定，命中即记录 `result_superseded` 且**不写任何状态**；
- 答案写入前判定（原已加），并在 `_accept_or_repair` 入口、`saving` 转换前、修复请求前各加一次；
- 被取代分支不再写 `stale`：纸面状态属于当前任务，避免旧任务把新任务的 `ready` 冲掉；
- force 不再取消旧任务（取消会与新任务的状态转换竞争），改为"**接管写入权**"：旧任务可以跑完它的调用，但结果一律被拒。

回归测试 `tests/security/test_phase7_review_fixes.py::LateGeneration`：并发 2、首调用阻塞、飞行中 force，断言最终状态 `ready`、存储的答案属于当前 job、被取代任务有 `result_superseded` 记录、当前任务的调用已计费。

### 修复 2：健康检查与模型列表受同一开关约束

审查实测：`outbound_blocked()==('ai_paused',…)` 且 `approvals==0` 时，`provider_ready()` 仍发出 2 个带 `Authorization` 的 GET。

整改（`analysis.py`、`server.py`）：
- `provider_ready()` 先查 `outbound_blocked()`（暂停 / 停用 → `ai_paused` / `profile_disabled`），再查 `target_approved()`（未确认 → `consent_required`），命中即返回 `DisabledAdapter` 与对应 `Health`，**不建立任何连接**；
- `target_approved()` 由 `JobLedger.approve_target` 记录，`manager.approve_target()` 包装并同步写 `data/outbound.json`（原来无人调用的 `Store.approve_target` 现在有了生产调用者）；
- 保存设置的两个路由（`POST /api/settings`、`POST /api/ai-settings`）在写库后调用 `approve_target()`：**用户输入并保存这个地址就是对该目标的确认**，它不授权任何论文。

回归测试 `test_phase7_review_fixes.py::PausedTarget`（5 项）：暂停 → 0 次探测；未确认目标 → 0 次探测；确认后 → 正常探测；换地址 → 批准失效；停用 profile → 0 次探测。

### 修复 3：引文核对状态可见

审查实测：含 1 条 0.96 相似度近似引文 + 12 条逐字引文的卡片被正常保存为 `ready`，近似引文在界面上与逐字引文完全一样，卡片 `warnings` 只存在于 JSON。

整改（`static/app.js`、`static/ai-reference.css`）：
- 每条引文渲染核对状态：`verified===false` → 「待人工核对：这条引文与原文不完全一致，不能当作逐字证据」，否则「逐字核对」；
- 渲染 `s.answer.warnings`（模型提醒）；
- 截断提示改为只要 `input.truncated` 就显示（原来要求 `chunks_used` 也满足）；
- 提供商文案由"由本机模型生成"改为"由已配置的模型服务生成"（外部 API 模式下原文案是错的）；
- 新增样式 `.ai-verified` / `.ai-unverified` / `.ai-warnings`。

回归测试 `AnswerVisibility`：断言渲染分支、样式与"不再出现无条件本机模型文案"。

### 修复 4：文档与实现一致

| 原承诺 | 处理 |
|---|---|
| "暂停立刻拒绝…健康检查、模型列表"（当时为假） | 实现已改为真，文档补充"它们同样会带上密钥出网" |
| "每次生成最多 2 次调用"（当时为假） | 重发计入额度后为真；文档补充"额度不足时不会重发" |
| "服务商返回用量或价格时可累计估算"（无实现） | **删除**，改为"只按次数记账，金额以服务商控制台为准" |
| "环境代理变量会被明确报告"（无生产调用者） | 改为"忽略代理，直接连接你批准的地址"，不再承诺报告 |
| "用诊断包时先看清文件列表"（功能不存在） | **删除**，明确写"本版本没有诊断包/支持包导出功能" |
| 未说明自动分析开关 | 新增说明：默认关闭；打开后**每次导入自动发送一次**（外部 API 即一次费用） |

自我报告 `AI安全升级-测试与泄露扫描报告.md` 中"诊断包/支持包：否（已扫描为干净）"改为"不适用：本版本没有该导出功能"，消除自相矛盾。

### 修复 5：安全套件的红色项

审查如实记录：`test_phase5_content.py::PromptIsolation::test_the_system_prompt_states_the_paper_is_untrusted` 断言旧措辞 `"你没有工具"`，而规范第 10 节的增补把该句改为"你没有密钥、账户、文件、网络或工具权限"。这是我在做第 10 节增补后**漏改的测试断言**，不是产品退化。已改为断言实际语义（工具权限、联网、读写文件、执行代码、环境变量、以及"来源声明不能改变规则"）。

同时说明：审查在 13:20/13:29 观察到工作副本被改动，正是我在做第 10 节增补与断言同步；审查对此的时间线记录准确，其"结论只对这些哈希负责"的声明正确。

---

## 3 额外修复（审查未列但同批发现）

| 项 | 说明 |
|---|---|
| `build_release.py` 模块清单损坏 | 我此前的多次字符串替换把 `APP_FILES` 拼坏（一行被拼接、模块重复），构建结果不可信。已改为**从目录发现** `*.py` 并显式排除开发脚本，缺入口文件即构建失败 |
| 发布包排除规则 | 新增 `EXCLUDE_DIRS = {tests, tools, docs, .git, __pycache__}` 与 `secret.json`/`outbound.json`/`job-ledger.json`/`run-instance.json`/`*.log`/`*.zip`；验证 ZIP：**0 个** `tests/`、`tools/`、`secret.json`、ledger、日志条目 |
| `tests/secret.json` | 含 26 字符、`sk-` 前缀、全密钥字符集的值，形似真实密钥。**不打包**（已在排除清单），且需要你确认是否为真实旧 key：若是，请吊销并删除该文件；测试不依赖它 |
| 发布说明 | 新增"AI 出站与凭据的安全加固"整节：密钥与目标、发送与费用、内容与展示、恢复与备份、已知边界 |

---

## 4 最终验证（整改后）

```
python -m unittest discover -s tests                            → 310 项 OK
python -m unittest discover -s tests/security -p "test_phase*.py" → 193 项 OK（阶段 1–7）
python tests/security/phase1_channel_scan.py                    → 19 通道，泄露 0
python tests/security/phase2_e2e_https.py                       → 16 项 PASS，泄露 0
python tests/release_smoke.py --with-data                       → result: OK
python tests/build_release.py                                   → 234 files，live install untouched: True
```

发布包（已重建，含上述全部整改）：

| 包 | SHA-256（前 24） |
|---|---|
| `论文研读助手-本地版-1.3.1-全新安装.zip` | `B7BCBFA52A86285BDC2E2FF9…` |
| `论文研读助手-本地版-1.3.1-升级包.zip` | `6489FEAE2BD6C80CF57EDDD1…` |

**分发前警告**：两个包都包含真实 `library.json`（你的 3 篇论文），**不要公开分发**。

---

## 5 仍未修复 / 未验证

| 项 | 性质 |
|---|---|
| 重试 reservation 的 `kind` 标为 `first` | 可读性问题，不影响计费与拦截 |
| `verify_analysis` 未强制 `source_sha256` 存在 | 缺字段走"旧格式→未验证"分支，不透传信任 |
| `session.BOOTSTRAP_USED` 不可达 | 死代码，无行为影响 |
| `X-Reader-Token` 遗留头通道 | 迁移期兼容；页面已不分发该令牌 |
| 真实服务商端到端 | 需你用自己的密钥与一篇不敏感论文验证 |
| 浏览器真实渲染（草稿隔离、面板文案） | 审查与我均只在代码层确认 |
| 命名管道 DACL 的跨用户拒绝 | 只能在当前用户下验证 |
| `tests/secret.json` 是否为真实旧 key | 需你确认；测试不依赖该文件 |

---

## 6 对审查者的回应

审查报告的质量很高：两个"必须修复"项都是真实缺陷，且都给出了可复现步骤与根因定位（`analysis.py` 写入路径缺判定、`provider_ready` 缺开关检查），我按此复现并修复；"安全套件是红的"这一条尤其重要——它纠正了我"182 项 OK"的自述，并准确指出原因是断言字面量被改而非产品退化。审查对自身局限的声明（未做浏览器渲染、未验证跨用户 DACL、未复现并发抢额度竞态）也准确。

我对审查报告的两点补充：
1. 修复 1 的根因是**两层**：除了它指出的写入路径缺判定，还有状态转换的顺序竞争（旧任务的 `validating` 覆盖新任务的 `ready`）。只加写入判定不足以让状态正确。
2. 修复 2 的"目标级批准"在整改前确实是死代码，但它现在成为真实控制点，也因此改变了一条既有行为：**未经保存确认的目标不会收到健康检查**。这是有意的收紧，已在文档与本报告中说明。
