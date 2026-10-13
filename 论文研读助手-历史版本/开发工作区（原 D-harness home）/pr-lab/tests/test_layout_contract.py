"""Static contract for the reader's two-pane layout: a grid track for every pane.

The reported defect was the notes panel dropping to a second row (上下分页) after the drag handle
became a third child of `.workspace`. A browser measurement (`tests/ui_layout_check.py`) proves the
geometry; this test keeps the source honest without needing a browser, by checking that the
desktop grid declares exactly as many column tracks as the workspace has children, and that the
collapsed/narrow states still hide what they should.

Run:  D:\\harness home\\pr-lab\\runtime\\python.exe D:\\harness home\\pr-lab\\tests\\test_layout_contract.py
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LAYOUT = (ROOT / "static" / "reader-layout.css").read_text(encoding="utf-8")
PAGE = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def block(css, selector_pattern, opening="@media(min-width:931px){"):
    """The `{...}` body of the first rule matching `selector_pattern` inside one media block."""
    start = css.index(opening)
    depth, index = 0, start
    while index < len(css):                      # find the end of the media block
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                break
        index += 1
    media = css[start:index + 1]
    match = re.search(selector_pattern + r"\{([^}]*)\}", media)
    return match.group(1) if match else ""


def workspace_html():
    start = PAGE.index('class="workspace')
    start = PAGE.rindex("<", 0, start)
    depth, index = 0, start
    while index < len(PAGE):
        if PAGE.startswith("<div", index) or PAGE.startswith("<section", index) \
                or PAGE.startswith("<aside", index):
            depth += 1
        elif PAGE.startswith("</div>", index) or PAGE.startswith("</section>", index) \
                or PAGE.startswith("</aside>", index):
            depth -= 1
            if depth == 0:
                return PAGE[start:index]
        index += 1
    raise AssertionError("workspace block not found")


class DesktopGrid(unittest.TestCase):
    def setUp(self):
        self.tracks = block(LAYOUT, r"\.workspace")
        self.children = workspace_html()

    def test_the_grid_declares_a_track_for_every_child(self):
        """Two tracks for three children is what pushed the notes panel onto a second row."""
        template = re.search(r"grid-template-columns:([^;]+)", self.tracks)
        self.assertIsNotNone(template, "the desktop workspace grid has no column template")
        declared = template.group(1).split()
        children = len(re.findall(r'class="(?:pdf-panel|notes-panel)"|id="splitter"', self.children))
        self.assertEqual(3, children, "expected the reader, the handle and the notes panel")
        self.assertEqual(children, len(declared),
                         f"{children} panes need {children} tracks, found {len(declared)}: {declared}")

    def test_the_handle_track_sits_between_the_two_panes(self):
        template = re.search(r"grid-template-columns:([^;]+)", self.tracks).group(1).split()
        self.assertEqual("14px", template[1], template)
        self.assertIn("--pdf-split", template[0])
        self.assertIn("1fr", template[2])

    def test_the_handle_is_a_separator_with_keyboard_and_pointer_support(self):
        handle = re.search(r"<div id=\"splitter\".*?</div>", PAGE, re.S).group(0)
        self.assertIn('role="separator"', handle)
        self.assertIn('tabindex="0"', handle)
        self.assertIn("aria-valuenow", handle)
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("initSplitter", script)
        self.assertIn("paper-reader:pdf-split", script)


class CollapsedAndNarrow(unittest.TestCase):
    def test_collapsing_returns_to_a_single_track_and_hides_both(self):
        body = block(LAYOUT, r"\.workspace\.notes-collapsed")
        self.assertEqual(1, len(re.search(r"grid-template-columns:([^;]+)", body).group(1).split()))
        hidden = LAYOUT[LAYOUT.index(".workspace.notes-collapsed .notes-panel"):
                        LAYOUT.index(".workspace.notes-collapsed .pdf-toolbar")]
        self.assertIn("display:none", hidden)
        self.assertIn(".splitter", hidden)

    def test_a_narrow_viewport_hides_the_handle(self):
        narrow = LAYOUT[LAYOUT.index("@media(max-width:930px){"):]
        self.assertIn(".splitter{display:none}", narrow.replace(" ", ""))


class SidebarCollapse(unittest.TestCase):
    """The left control panel collapses from the topbar, and the reader gets its width."""

    def setUp(self):
        self.css = (ROOT / "static" / "reader-layout.css").read_text(encoding="utf-8")
        self.script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    def test_the_button_targets_an_element_that_exists(self):
        button = re.search(r"<button id=\"toggle-sidebar\".*?</button>", PAGE, re.S)
        self.assertIsNotNone(button, "the topbar has no sidebar toggle")
        self.assertIn('aria-pressed="false"', button.group(0))
        self.assertIn('aria-expanded="true"', button.group(0))
        target = re.search(r'aria-controls="([^"]+)"', button.group(0)).group(1)
        self.assertIn(f'id="{target}"', PAGE, "aria-controls must point at a real element")

    def test_collapsing_hides_the_panel_and_clears_the_narrow_screen_margin(self):
        flat = re.sub(r"\s+", "", self.css)          # compare on a whitespace-free copy
        self.assertIn("body.sidebar-collapsed.sidebar{display:none}", flat)
        self.assertIn("body.sidebar-collapsed.main{margin-left:0}", flat)

    def test_the_state_is_remembered(self):
        self.assertIn("paper-reader:sidebar-collapsed", self.script)
        self.assertIn("function initSidebar()", self.script)
        self.assertIn("initSidebar();", self.script)
        self.assertIn("localStorage.setItem(SIDEBAR_KEY", self.script)

    def test_the_control_lives_with_the_sidebar_and_a_rail_survives_collapsing(self):
        """需求：收起控件移到左侧、与侧栏一体；侧栏隐藏后必须还有入口。"""
        start = PAGE.index('<aside class="sidebar"')
        sidebar = PAGE[start:PAGE.index("</aside>", start)]
        self.assertIn('id="toggle-sidebar"', sidebar, "收起控件应当在侧栏里面")
        self.assertNotIn('id="toggle-sidebar"', PAGE[:start], "不应再留在顶栏")
        rail = re.search(r'<button id="sidebar-rail".*?</button>', PAGE, re.S)
        self.assertIsNotNone(rail, "缺少收起后仍可用的常驻把手")
        self.assertIn('aria-controls="sidebar"', rail.group(0))
        self.assertIn(".sidebar-rail{", self.css.replace(" ", "").replace("\n", ""))

    def test_both_controls_share_one_state(self):
        flat = re.sub(r"\s+", "", self.script)
        self.assertIn("for(constidof['#toggle-sidebar','#sidebar-rail'])", flat,
                      "两个入口必须切换同一份状态")


class DefaultSplit(unittest.TestCase):
    """The default split lives in three files and must be one number.

    app.js writes the value, reader-layout.css carries the fallback for the first paint, and
    index.html ships an ARIA placeholder. If they drift, the first paint and the dragged state
    disagree - which is exactly the kind of inconsistency this file exists to catch.
    """

    def numbers(self):
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "static" / "reader-layout.css").read_text(encoding="utf-8")
        page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        return (re.search(r"const SPLIT_DEFAULT=(\d+(?:\.\d+)?);", script),
                re.search(r"var\(--pdf-split,(\d+(?:\.\d+)?)%\)", css),
                re.search(r'id="splitter".*?aria-valuenow="(\d+(?:\.\d+)?)"', page, re.S))

    def test_one_default_everywhere(self):
        js, css, aria = self.numbers()
        for match, where in ((js, "app.js"), (css, "reader-layout.css"), (aria, "index.html")):
            self.assertIsNotNone(match, f"the default split is missing from {where}")
        self.assertEqual(js.group(1), css.group(1), "app.js and reader-layout.css disagree")
        self.assertEqual(js.group(1), aria.group(1), "app.js and index.html disagree")

    def test_the_default_keeps_both_panes_usable(self):
        js, _css, _aria = self.numbers()
        value = float(js.group(1))
        self.assertGreaterEqual(value, 45, "the notes panel would be starved")
        self.assertLessEqual(value, 75, "the reader pane would be starved")


if __name__ == "__main__":
    unittest.main(verbosity=2)
