"""UI 契约测试（1.3.5）：不做浏览器，只读 static/ 的源文件。

五件事都必须“看得见”：
  1) 文献栏每一行都有删除控件，且点击它不会顺带选中论文；
  2) 删除对话框按 id 工作（pendingDeleteId），不是按“当前打开的论文”；
  3) AI 分析进度条与阶段中文名、以及本地静态资源的 __VERSION__ 版本戳；
  4) 原文 / 记录栏之间的拖拽分隔条：结构、localStorage 记忆、复位路径、与收起记录栏共存；
  5) AI 设置里的读图开关与图表页上限，以及排版记号（--fs-*/--font-*/--lh-*）确实生效。

这些断言只读文件、不发请求，所以既能离线跑，也能在打包前当作自检。

运行：
  D:\\harness home\\pr-lab\\runtime\\python.exe -B -m unittest discover -s tests
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"

#: 本地资源必须带版本戳：升级后 /static/ 的 max-age=3600 仍会命中旧文件，
#: 只有 URL 变化才能让浏览器换用新的 app.js / guides.js / 样式表。
VERSION_MARK = "?v=__VERSION__"

#: 允许低于 12px 的“装饰字”：键是选择器，值是字号记号。
#: body 正文、表单、读数、标签一律 >= 12px；11px 只剩下一处：
#:   · .brand small —— 侧栏品牌下方的拉丁字母广告词（PAPER READING STUDIO），
#:     8 个字母 + 全大写 + 1.2px 字距，是标志的一部分，不承载中文正文。
#: 每个视图顶部那行拉丁题签（.eyebrow，READ WITH A QUESTION）也是装饰，
#: 但它已经统一到 12px（--fs-meta），因此不在例外清单里。
TYPOGRAPHY_DECORATIVE_ALLOWLIST = {
    ".brand small": "--fs-micro",
}
#: 字号层里最小的两个记号也不能低于 12px（--fs-micro 只作装饰）。
TYPOGRAPHY_MIN_CONTENT_PX = 12.0
#: 需要 --fs-* 记号的选择器：正文与阅读内容必须是 14px 那一档。
CONTENT_SELECTORS = (".field textarea", ".annotation-card p", ".annotation-card blockquote",
                     ".card-preview", ".guide-card p", ".ai-answer")

SHEETS = ("styles.css", "reader-layout.css", "ai-reference.css", "readability.css")


def declarations_for(selector, css):
    """选择器出现在某个规则的选择器列表里时，返回那个规则的声明块（按加载顺序拼接）。

    同一个选择器可能被后面的样式表再写一次，拼接后前面的声明仍在，
    所以断言用 assertIn 而不是正则从头匹配。
    """
    out = []
    for selectors, body in rule_blocks(css):
        if selector in [one.strip() for one in selectors.split(",")]:
            out.append(body)
    return "".join(out)


def read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def read_sheets():
    """按 index.html 的加载顺序拼起来：后加载的样式表会覆盖先加载的。"""
    return "\n".join(read(name) for name in SHEETS)


def strip_comments(text):
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def type_tokens():
    """styles.css 的 :root 里声明的字号记号：{'--fs-body': 14.0, ...}。

    只收纯 px 值；带 clamp() 的记号（--fs-title）不进这个表，另有断言。
    """
    css = strip_comments(read("styles.css"))
    root = css[css.index(":root"):css.index("}", css.index(":root"))]
    return {name: float(px) for name, px in re.findall(r"(--fs-[a-z0-9-]+):([0-9.]+)px", root)}


def rule_blocks(css):
    """(选择器, 声明块) —— 够用即可：这些样式表里没有嵌套规则。"""
    body = strip_comments(css)
    return [(m.group(1).strip().split("*/")[-1].strip(), m.group(2))
            for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", body)]


def _rules_in_order():
    """按 index.html 的加载顺序列出每条 font-size 规则：(顺序, media, 选择器, 值)。

    值是记号名（--fs-*）或写死的 "11px"。
    """
    out, order = [], 0
    for name in SHEETS:
        text = strip_comments(read(name))
        for media, selectors, body in _media_blocks(text):
            match = re.search(r"font-size:\s*(var\((--fs-[a-z0-9-]+)\)|([0-9.]+px))", body)
            if not match:
                continue
            order += 1
            value = match.group(2) or match.group(3)
            for one in selectors.split(","):
                one = one.strip()
                if one:
                    out.append((order, media, one, value))
    return out


def _media_blocks(text, media=""):
    """展开 @media，产出 (媒体条件, 选择器, 声明块)。"""
    out, i = [], 0
    while True:
        brace = text.find("{", i)
        if brace == -1:
            return out
        head = text[i:brace].strip()
        if head.startswith("@media"):
            end = text.find("{", brace)
            depth, j = 1, end + 1
            while j < len(text) and depth:
                depth += (text[j] == "{") - (text[j] == "}")
                j += 1
            out += _media_blocks(text[end + 1:j - 1], head)
            i = j
            continue
        end = text.find("}", brace)
        if end == -1:
            return out
        out.append((media, head, text[brace + 1:end]))
        i = end + 1


def _media_matches(media, width):
    if not media:
        return True
    low = re.search(r"min-width:\s*(\d+)px", media)
    high = re.search(r"max-width:\s*(\d+)px", media)
    if low and width < int(low.group(1)):
        return False
    if high and width > int(high.group(1)):
        return False
    return True


def _specificity(selector):
    ids = len(re.findall(r"#[\w-]+", selector))
    classes = len(re.findall(r"\.[\w-]+|\[[^\]]+\]|:{1,2}[\w-]+", selector))
    types = len(re.findall(r"(?:^|[\s>+~])([a-z]+[a-z0-9]*)", selector))
    return (ids, classes, types, len(selector))


def winning_font_sizes(width, tokens):
    """在给定视口宽度下，每个选择器最终生效的字号（px）。

    记号与写死的像素值都算；写死的值只应出现在装饰选择器上（另有断言）。
    """
    winners = {}
    for order, media, selector, value in _rules_in_order():
        if not _media_matches(media, width):
            continue
        size = tokens.get(value) if value.startswith("--") else float(value[:-2])
        if size is None:
            continue
        key = (bool(media), _specificity(selector), order)
        if selector not in winners or key >= winners[selector][0]:
            winners[selector] = (key, size)
    return {selector: size for selector, (_, size) in winners.items()}



class AssetVersioning(unittest.TestCase):
    """index.html 里每一个本地资源引用都要带 __VERSION__。"""

    def setUp(self):
        self.html = read("index.html")

    def test_every_local_asset_carries_the_version_placeholder(self):
        refs = re.findall(r'(?:href|src)="([^"]+)"', self.html)
        local = [ref for ref in refs if ref.startswith("/static/")]
        self.assertTrue(local, "index.html 应当引用本地 static/ 资源")
        missing = [ref for ref in local if VERSION_MARK not in ref]
        self.assertEqual([], missing, "这些本地资源没有版本戳：" + "、".join(missing))

    def test_the_expected_assets_are_versioned(self):
        for name in ("styles.css", "reader-layout.css", "ai-reference.css",
                     "readability.css", "app.js", "guides.js"):
            with self.subTest(asset=name):
                self.assertIn(f"/static/{name}{VERSION_MARK}", self.html)

    def test_the_placeholder_is_never_replaced_here(self):
        """占位符必须原样保留：替换由 server.py 在响应时完成。"""
        self.assertIn("__VERSION__", self.html)
        for literal in ("?v=1.3", "?v=1.2", "?v=0."):
            self.assertNotIn(literal, self.html)

    def test_guides_js_module_specifier_is_versioned_too(self):
        """guides.js 由 app.js 以模块方式导入，模块图不经过 HTML，必须自己带版本戳。"""
        self.assertIn(f"'./guides.js{VERSION_MARK}'", read("app.js"))


class LibraryRowDeleteControl(unittest.TestCase):
    """文献栏行内删除控件：渲染在行里，且不会触发选中。"""

    def setUp(self):
        self.js = read("app.js")

    def test_renderer_builds_a_row_delete_control(self):
        body = self.js[self.js.index("function renderLibrary("):]
        body = body[:body.index("\nfunction renderHeader(")]
        self.assertIn("data-delete-paper=", body)
        self.assertIn("row-delete", body)

    def test_row_click_handler_checks_delete_before_selecting(self):
        handler = self.js[self.js.index("document.addEventListener('click'"):]
        delete_at = handler.index("closest('[data-delete-paper]')")
        select_at = handler.index("closest('[data-paper]')")
        self.assertLess(delete_at, select_at,
                        "删除控件必须先匹配，否则点击会被“选中这篇论文”吃掉")
        block = handler[delete_at:select_at]
        self.assertIn("openDeletePaper(", block)
        self.assertIn("return", block)

    def test_delete_control_is_keyboard_reachable(self):
        self.assertIn("role=\"button\" tabindex=\"0\" data-delete-paper=", self.js)
        keydown = self.js[self.js.index("document.addEventListener('keydown'"):]
        self.assertIn("closest?.('[data-delete-paper]')", keydown)
        self.assertIn("e.key==='Enter'", keydown)


class DeleteDialogTargetsAnId(unittest.TestCase):
    """确认框按 id 删除，标题来自同一行，而不是“当前打开的论文”。"""

    def setUp(self):
        self.js = read("app.js")

    def test_pending_delete_id_drives_the_dialog_and_the_request(self):
        self.assertIn("let pendingDeleteId=", self.js)
        opener = self.js[self.js.index("async function openDeletePaper("):]
        opener = opener[:opener.index("async function confirmDeletePaper(")]
        self.assertIn("pendingDeleteId=id;", opener)
        self.assertIn("$('#delete-paper-title').textContent=p.title", opener)
        confirm = self.js[self.js.index("async function confirmDeletePaper("):]
        confirm = confirm[:confirm.index("$('#delete-dialog').addEventListener('close'")]
        self.assertIn("const id=pendingDeleteId||currentId;", confirm)
        self.assertIn("api('delete',{paper_id:id})", confirm)

    def test_the_action_strip_button_still_opens_the_same_dialog(self):
        self.assertIn("$('#delete-paper').addEventListener('click',openDeletePaper);", self.js)

    def test_vault_warning_survives_in_the_confirm_text(self):
        self.assertIn("Obsidian 笔记库未改动", self.js)


class MissingSourceMarker(unittest.TestCase):
    """原文缺失的记录必须看得见，并说明可用删除控件清理。"""

    def setUp(self):
        self.js = read("app.js")

    def test_empty_source_hash_means_missing_source(self):
        body = self.js[self.js.index("function sourceMissing("):]
        body = body[:body.index("\nfunction renderLibrary(")]
        self.assertIn("meta.source_sha256", body)

    def test_the_row_shows_a_chinese_hint(self):
        body = self.js[self.js.index("function renderLibrary("):]
        body = body[:body.index("\nfunction renderHeader(")]
        self.assertIn("sourceMissing(p)", body)
        self.assertIn("原文缺失", body)
        self.assertIn("source-missing", body)


class AiProgressBar(unittest.TestCase):
    """AI 分析进度条：元素、百分比、中文阶段名、服务端 message 原样显示。"""

    STAGES = ("排队中", "抽取原文", "切分原文", "请求模型", "校验内容", "保存结果", "完成")

    def setUp(self):
        self.js = read("app.js")
        self.html = read("index.html")
        self.css = read("styles.css")

    def test_html_declares_the_element(self):
        self.assertIn('id="ai-progress"', self.html)
        self.assertIn('id="ai-progress-pct"', self.html)

    def test_the_progress_fill_block_is_gone(self):
        """需求：删掉进度条的填充块，只留阶段名 + 百分比 + 服务器消息。"""
        self.assertNotIn("ai-progress-fill", self.html)
        self.assertNotIn("ai-progress-track", self.html)
        self.assertNotIn("ai-progress-fill", self.js)
        self.assertNotIn("ai-progress-track", self.js)
        self.assertNotIn("mini-progress ai", self.js)
        self.assertNotIn(".mini-progress.ai", self.css)
        self.assertNotIn("ai-progress-fill", self.css)
        # 四步研读的进度条不属于 AI，必须保留
        self.assertIn('id="progress-bar"', self.html)
        self.assertIn("progress-track", self.html)

    def test_stage_labels_and_progress_values_match_analysis_py(self):
        for label in self.STAGES:
            with self.subTest(label=label):
                self.assertIn(label, self.js)
        for stage, value in (("queued", 0), ("extracting", 15), ("chunking", 30),
                             ("requesting", 55), ("validating", 80), ("saving", 95),
                             ("done", 100)):
            with self.subTest(stage=stage):
                self.assertRegex(self.js, rf"{stage}:{value}\b")

    def test_box_shows_percentage_stage_and_server_message(self):
        body = self.js[self.js.index("function aiProgressHTML("):]
        body = body[:body.index("function aiCardBanner(")]
        self.assertIn("aiStageLabel(meta)", body)
        self.assertIn("${pct}%", body)
        self.assertIn("esc(message)", body, "服务端 message 必须原样（转义后）显示")

    def test_bar_only_renders_while_queued_or_running(self):
        self.assertIn("const AI_ACTIVE_STATUSES=['queued','running'];", self.js)
        banner = self.js[self.js.index("function aiCardBanner("):]
        banner = banner[:banner.index("function aiPanelHTML(")]
        self.assertIn("const bar=aiActive(s)?aiProgressHTML(s):'';", banner,
                      "failed 时不能留下进度条，但要保留重试按钮")
        self.assertIn("data-ai-run=", banner)

    def test_polling_is_bounded(self):
        self.assertIn("const AI_POLL_MS=3000;", self.js)
        poll = self.js[self.js.index("function aiPollSync()"):]
        poll = poll[:poll.index("function aiCopyToField(")]
        self.assertIn("setInterval(", poll)
        self.assertIn("clearInterval(", poll)
        self.assertIn("if(aiPollTimer)return;", poll, "重排前必须先清掉旧计时器，不能叠加")
        self.assertIn("document.hidden", poll)
        self.assertIn("aiPollActive()", poll)
        self.assertIn("/api/analysis", self.js)
        self.assertIn("visibilitychange", self.js)


class SplitterContract(unittest.TestCase):
    """原文 / 记录栏之间的拖拽分隔条：结构、记忆、复位、与既有布局共存。

    分隔条只改一列宽度，所以断言集中在“看得见的东西”上：
    HTML 里有 role="separator" 的把手、app.js 把位置写进一个有名字的 localStorage 键、
    有复位路径（双击 / Home）、有键盘微调、并且不和“收起记录栏”抢同一套逻辑。
    """

    #: 位置记忆的键：paper-reader: 是本站其它持久化（草稿、上次打开的论文）已经在用的前缀。
    SPLIT_STORAGE_KEY = "paper-reader:pdf-split"

    def setUp(self):
        self.html = read("index.html")
        self.js = read("app.js")
        self.css = read_sheets()
        self.splitter_css = read("reader-layout.css")
        start = self.js.index("--- 原文 / 记录栏的可拖拽分隔")
        self.block = self.js[start:self.js.index("/* 收起记录栏：把宽度让给原文。", start)]

    def test_html_declares_a_separator_handle_between_the_two_panes(self):
        self.assertIn('id="splitter"', self.html)
        self.assertIn('class="splitter"', self.html)
        self.assertIn('role="separator"', self.html)
        self.assertIn('aria-orientation="vertical"', self.html)
        self.assertIn('tabindex="0"', self.html)
        self.assertIn('aria-label="调整原文与记录栏宽度"', self.html)
        # 把手必须夹在 PDF 面板与记录栏之间，顺序错了网格列就对不上。
        pdf_at = self.html.index('class="pdf-panel"')
        split_at = self.html.index('id="splitter"')
        notes_at = self.html.index('class="notes-panel"')
        self.assertLess(pdf_at, split_at)
        self.assertLess(split_at, notes_at)
        self.assertIn('aria-controls="pdf-scroll notes-content"', self.html)

    def test_handle_has_a_drag_affordance_and_a_resize_cursor(self):
        for marker in ("cursor:col-resize", "touch-action:none", ".splitter-grip",
                       ".splitter.dragging"):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.splitter_css)
        self.assertIn('<span class="splitter-grip"', self.html, "视觉把手是分隔条里的一个 span")

    def test_position_is_persisted_under_a_documented_key(self):
        self.assertIn(f"const SPLIT_KEY='{self.SPLIT_STORAGE_KEY}';", self.block)
        self.assertIn("localStorage.setItem(SPLIT_KEY", self.block)
        self.assertIn("localStorage.getItem(SPLIT_KEY", self.block)
        # 存的是百分数：窗口变宽时两栏按同一比例一起变，不会像像素值那样越拉越偏。
        self.assertIn("const SPLIT_DEFAULT=58;", self.block)
        self.assertIn("--pdf-split", self.block)
        self.assertIn("--pdf-split", self.splitter_css, "网格列宽必须读这个变量")

    def test_the_two_pane_grid_has_a_track_for_the_handle(self):
        """网格必须有“原文 | 分隔条 | 记录栏”三条轨道。

        只写两条轨道时，第三个网格项（分隔条）会被自动排到第二行，
        记录栏跟着落到第二行、原文面板被挤成 0 高——页面看起来就是没有原文，
        而“分隔条存在、拖动也生效”的断言全都照常通过。所以这里钉住轨道数。
        """
        grid = declarations_for(".workspace", self.splitter_css)
        columns = re.search(r"grid-template-columns:([^;}]+)", grid)
        self.assertIsNotNone(columns, "分隔条所在的断点里必须显式写 grid-template-columns")
        tracks = columns.group(1).split()
        self.assertEqual(3, len(tracks), "轨道应当是：原文 / 分隔条 / 记录栏，实得：" + columns.group(1))
        self.assertIn("var(--pdf-split,58%)", tracks[0])
        self.assertEqual("14px", tracks[1], "分隔条轨道宽度要等于 .splitter 自己的宽度")
        self.assertIn("minmax(0,1fr)", tracks[2])

    def test_minimums_are_enforced_and_match_the_aria_attributes(self):
        self.assertIn("const SPLIT_MIN_PDF=420;", self.block)
        self.assertIn("const SPLIT_MIN_NOTES=320;", self.block)
        self.assertIn("function clampSplitRatio(", self.block)
        self.assertIn('aria-valuemin="420"', self.html)
        self.assertIn('aria-valuenow="58"', self.html)
        # aria-valuenow / aria-valuetext 随拖动更新，读屏软件才跟得上。
        self.assertIn("setAttribute('aria-valuenow'", self.block)
        self.assertIn("setAttribute('aria-valuetext'", self.block)

    def test_pointer_keyboard_and_double_click_paths_exist(self):
        self.assertIn("pointerdown", self.block)
        self.assertIn("pointermove", self.block)
        self.assertIn("pointerup", self.block)
        self.assertIn("pointercancel", self.block)
        self.assertIn("setPointerCapture(", self.block)
        self.assertIn("addEventListener('dblclick'", self.block)
        self.assertIn("ArrowLeft", self.block)
        self.assertIn("ArrowRight", self.block)
        self.assertIn("document.body.classList.add('splitter-active')", self.block)

    def test_reset_path_restores_the_default_split(self):
        self.assertIn("function resetSplit(", self.block)
        self.assertIn("applySplit(SPLIT_DEFAULT,", self.block)
        self.assertIn("resetSplit(true)", self.block, "双击与 Home 都走同一个复位函数")

    def test_arrow_keys_do_not_turn_the_pdf_page(self):
        """方向键在分隔条上必须只调宽度：document 上还有“方向键翻页”的快捷键。"""
        self.assertIn("e.stopPropagation()", self.block)
        keydown = self.js[self.js.index("--- 原文 / 记录栏的可拖拽分隔"):]
        keydown = keydown[:keydown.index("/* 收起记录栏")]
        self.assertIn("handle.addEventListener('keydown'", keydown)
        self.assertIn("e.preventDefault();e.stopPropagation();", keydown)

    def test_it_coexists_with_the_notes_collapse_toggle(self):
        toggle = self.js[self.js.index("function toggleNotesPanel("):]
        toggle = toggle[:toggle.index("$('#toggle-notes').addEventListener")]
        self.assertIn("notes-collapsed", toggle)
        self.assertIn("paper-reader:notes-collapsed", toggle)
        # 收起时隐藏分隔条（CSS），展开后仍然用同一个 --pdf-split。
        self.assertIn(".workspace.notes-collapsed .splitter{display:none}", self.splitter_css)

    def test_pdf_fit_still_follows_a_width_change(self):
        """拖动只改宽度，重新适配仍由既有的 ResizeObserver 负责。"""
        observe = self.js.index("new ResizeObserver(")
        self.assertIn("$('#pdf-scroll')", self.js[observe:observe + 200])
        self.assertIn("renderPage()", self.js[observe:observe + 200])
        self.assertIn("renderPage();", self.block, "拖完/按键后要重新按“适合页面”渲染一次")

    def test_the_splitter_is_hidden_in_the_narrow_single_pane_layout(self):
        narrow = self.splitter_css[self.splitter_css.index("@media(max-width:930px)"):]
        self.assertIn(".splitter{display:none}", narrow)


class AiVisionSettings(unittest.TestCase):
    """AI 设置里的读图开关与“最多发送图表页数”（服务端已支持同名字段）。"""

    def setUp(self):
        self.html = read("index.html")
        self.js = read("app.js")
        self.css = read("styles.css")

    def test_html_declares_both_controls_with_bounds(self):
        self.assertIn('id="ai-vision"', self.html)
        self.assertIn('id="ai-max-figures"', self.html)
        vision = self.html[self.html.index('id="ai-vision"'):]
        vision = vision[:vision.index("</label>")]
        self.assertIn('type="checkbox"', vision)
        self.assertIn("读图与图表分析", vision)
        self.assertIn("多模态", vision)
        figures = self.html[self.html.index('id="ai-max-figures"'):]
        figures = figures[:figures.index(">")]
        self.assertIn('type="number"', figures)
        self.assertIn('min="0"', figures)
        self.assertIn('max="8"', figures)
        self.assertIn("最多发送图表页数", self.html)
        # 一句说明必须把代价讲清楚：图片会发给同一个模型服务、更慢更贵、只支持多模态。
        start = self.html.index('id="ai-max-figures"')
        help_text = self.html[start:start + 700]
        for word in ("图片", "更慢", "更贵", "多模态", "关闭"):
            with self.subTest(word=word):
                self.assertIn(word, help_text)

    def test_settings_are_loaded_from_the_returned_ai_block(self):
        loader = self.js[self.js.index("async function loadAiSettings()"):]
        loader = loader[:loader.index("/* Credential state is shown")]
        self.assertIn("ai.vision", loader)
        self.assertIn("ai.max_figure_pages", loader)
        self.assertIn("$('#ai-vision').checked", loader)
        self.assertIn("$('#ai-max-figures').value", loader)

    def test_both_fields_are_in_the_settings_payload(self):
        saver = self.js[self.js.index("async function saveAiSettings()"):]
        saver = saver[:saver.index("function openMetadata(")]
        payload = saver[saver.index("api('settings',"):saver.index(");", saver.index("api('settings',"))]
        self.assertIn("vision:", payload)
        self.assertIn("max_figure_pages:", payload)
        self.assertIn("auto_analyze:$('#ai-auto-analyze').checked", payload,
                      "新字段必须和既有字段在同一次提交里")

    def test_the_page_cap_is_clamped_to_zero_through_eight(self):
        self.assertIn("const AI_FIGURE_PAGES_MIN=0, AI_FIGURE_PAGES_MAX=8", self.js)
        self.assertIn("function figurePagesValue(", self.js)
        body = self.js[self.js.index("function figurePagesValue("):]
        body = body[:body.index("async function loadAiSettings()")]
        self.assertIn("Math.min(AI_FIGURE_PAGES_MAX,Math.max(AI_FIGURE_PAGES_MIN", body)

    def test_the_number_row_keeps_a_readable_size(self):
        block = declarations_for(".settings-number", self.css)
        self.assertIn("var(--fs-meta)", block)
        # 数字输入与它前面的说明在同一行，且和别的控件一样有统一高度。
        self.assertIn("align-items:center", block)
        self.assertIn("font-variant-numeric:tabular-nums",
                      declarations_for(".settings-number input", self.css))


class TypographyScale(unittest.TestCase):
    """排版记号：字号层、字体栈、行高与“正文不小于 12px”。"""

    def setUp(self):
        self.css = read("styles.css")
        self.sheets = read_sheets()
        self.tokens = type_tokens()
        self.js = read("app.js")

    def test_the_token_block_declares_the_font_stacks(self):
        for name, needle in (("--font-ui", "system-ui"), ("--font-ui", '"Segoe UI"'),
                             ("--font-ui", '"Microsoft YaHei UI"'), ("--font-ui", '"Microsoft YaHei"'),
                             ("--font-ui", '"PingFang SC"'), ("--font-ui", '"Noto Sans CJK SC"'),
                             ("--font-ui", "sans-serif"),
                             ("--font-mono", "ui-monospace"), ("--font-mono", "Consolas"),
                             ("--font-serif-cjk", '"STSong"'), ("--font-serif-latin", "Georgia")):
            with self.subTest(token=name, part=needle):
                self.assertIn(f"{name}:", self.css)
        stack = re.search(r"--font-ui:([^;]+);", self.css).group(1)
        for needed in ("Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
                       "Noto Sans CJK SC", "sans-serif"):
            with self.subTest(family=needed):
                self.assertIn(needed, stack)
        self.assertTrue(stack.rstrip().endswith("sans-serif"), "字体栈必须以通用族收尾")
        self.assertIn("font-family:var(--font-ui)", self.css)

    def test_the_type_scale_is_declared_as_tokens(self):
        for name in ("--fs-body", "--fs-meta", "--fs-sm", "--fs-micro", "--fs-lg",
                     "--fs-h3", "--fs-h2"):
            with self.subTest(token=name):
                self.assertIn(name, self.tokens)
        # 大标题用 clamp()，随视口在 16–20px 之间；下限也不低于 16px。
        title = re.search(r"--fs-title:clamp\((\d+)px,[^)]*?,(\d+)px\)", self.css)
        self.assertIsNotNone(title, "--fs-title 应当是 clamp(min, 视口, max)")
        self.assertGreaterEqual(float(title.group(1)), 16.0)
        self.assertGreaterEqual(float(title.group(2)), float(title.group(1)))

    def test_body_text_and_reading_content_are_at_least_12px(self):
        self.assertGreaterEqual(self.tokens["--fs-body"], 13.0, "正文至少要 13px 才不挤")
        self.assertGreaterEqual(self.tokens["--fs-meta"], TYPOGRAPHY_MIN_CONTENT_PX)
        self.assertGreaterEqual(self.tokens["--fs-sm"], TYPOGRAPHY_MIN_CONTENT_PX)
        self.assertGreaterEqual(self.tokens["--fs-lg"], TYPOGRAPHY_MIN_CONTENT_PX)

    def test_reading_content_uses_the_body_size(self):
        for selector in CONTENT_SELECTORS:
            with self.subTest(selector=selector):
                self.assertIn("font-size:var(--fs-body)", declarations_for(selector, self.sheets))

    def test_the_only_sub_12px_size_is_the_registered_decoration(self):
        """装饰字只有一个来源：.brand small 写死 11px，其余都必须走 --fs-* 记号。"""
        for selector, token in TYPOGRAPHY_DECORATIVE_ALLOWLIST.items():
            with self.subTest(selector=selector):
                block = declarations_for(selector, self.css)
                self.assertRegex(block, r"font-size:(11px|var\(%s\))" % re.escape(token))
                self.assertLess(self.tokens[token], TYPOGRAPHY_MIN_CONTENT_PX,
                                "清单里的记号本身就应该小于 12px，否则它不该在清单里")

    def test_chinese_paragraphs_get_a_loose_line_height(self):
        base = float(re.search(r"--lh-base:([0-9.]+);", self.css).group(1))
        paragraph = float(re.search(r"--lh-paragraph:([0-9.]+);", self.css).group(1))
        self.assertGreaterEqual(base, 1.6)
        self.assertLessEqual(paragraph, 1.85)
        self.assertGreater(paragraph, base)
        for selector in (".teacher-note", ".annotation-card p", ".ai-answer"):
            with self.subTest(selector=selector):
                self.assertIn("line-height:var(--lh-paragraph)",
                              declarations_for(selector, self.sheets))

    def test_the_heading_scale_is_one_set_of_tokens(self):
        for selector, token in (("h1", "--fs-title"), (".modal-header h2", "--fs-h2"),
                                (".card-preview h2", "--fs-h3")):
            with self.subTest(selector=selector):
                self.assertIn(f"font-size:var({token})", declarations_for(selector, self.css))

    def test_numbers_are_tabular(self):
        for selector in ("#progress-label", ".ai-progress-pct", ".chip", ".save-status",
                         ".ribbon-label", ".settings-number input"):
            with self.subTest(selector=selector):
                self.assertIn("font-variant-numeric:tabular-nums",
                              declarations_for(selector, self.css))

    def test_control_heights_and_radii_come_from_tokens(self):
        for name in ("--control-h", "--control-h-sm", "--radius-chip", "--radius-control"):
            with self.subTest(token=name):
                self.assertIn(name + ":", self.css)
        self.assertIn("min-height:var(--control-h)", self.css)
        self.assertIn("border-radius:var(--radius-chip)", self.css)

    def test_no_rule_leaves_content_text_below_12px(self):
        """字号只从记号来；最终生效值低于 12px 的只允许是清单里的装饰字。

        这里按真实层叠（加载顺序 + 优先级 + @media）算出“胜出”的那条规则，
        所以在各种常见宽度下都必须与清单一致。
        """
        for width in (1920, 1600, 1440, 1280, 1180, 1000, 931, 900, 700, 580, 560, 480):
            winners = winning_font_sizes(width, self.tokens)
            small = {selector for selector, size in winners.items()
                     if size < TYPOGRAPHY_MIN_CONTENT_PX}
            with self.subTest(width=width):
                self.assertEqual(set(TYPOGRAPHY_DECORATIVE_ALLOWLIST), small,
                                 "只有清单里的装饰字可以小于 12px；正文与标签都必须 >= 12px")

    def test_no_stylesheet_hardcodes_a_pixel_font_size(self):
        """所有字号都要走记号，页面里不应再有 9px/10px 这样的散装数字。

        唯一例外：.brand small 那行拉丁广告词写死 11px，并在清单里登记。
        """
        for name in SHEETS:
            with self.subTest(sheet=name):
                literals = re.findall(r"font-size:\s*([0-9.]+px)", read(name))
                self.assertEqual(["11px"] if name == "styles.css" else [], literals,
                                 "只有 .brand small（装饰字）可以写死像素字号")

    def test_app_js_does_not_inline_font_size_or_family(self):
        """app.js 拼 HTML 字符串，字号必须在样式表里决定。"""
        self.assertNotRegex(self.js, r"font-size")
        self.assertNotRegex(self.js, r"font-family")
        self.assertNotRegex(self.js, r"fontSize")

    def test_the_monospace_stack_is_used_for_numeric_inputs(self):
        self.assertIn(".page-controls input,.settings-number input{font-family:var(--font-mono)}",
                      self.css)


if __name__ == "__main__":
    unittest.main(verbosity=2)
