"""Generate small synthetic PDFs for the extraction tests.

No third-party library is used: these are hand-written PDF files with real xref
tables and a real Identity-H CID font with a ToUnicode CMap, so the extractor is
exercised end to end (content streams, font encodings, CMaps, two-column reading
order, tables with minus signs and percentages, image-only pages).

Design note: page text is authored as plain strings. A registry pass assigns every
non-ASCII character a distinct 2-byte code, then the CMap and the content streams are
generated from that registry - so no character can be missed or silently remapped.

Fixtures
--------
zh-headers.pdf   Chinese single column, running header and page-number footer,
                 table rows with negative values and percentages
en-hyphen.pdf    English, a hyphenated line break, numeric table with % and minus
columns.pdf      English two-column layout, numbers only in the right column
blank.pdf        one truly empty page (no text operators, no images)
scanned.pdf      image-only pages -> must be diagnosed as no_text_layer
"""
from __future__ import annotations

import re
import zlib
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pdf"

FIRST_CODE = 0x0100          # codes below this stay single-byte ASCII
_DEBUG = False               # set True to trace registry codes while building fixtures


class Registry:
    """Assigns a 2-byte code to every non-ASCII character used in the document."""

    def __init__(self):
        self.codes: dict[str, int] = {}
        self.next_code = FIRST_CODE

    def collect(self, text):
        for char in text:
            if ord(char) >= 0x80 and char not in self.codes:
                self.codes[char] = self.next_code
                self.next_code += 1

    def encode(self, text):
        """Identity-H strings are sequences of 2-byte codes, ASCII included."""
        out = bytearray()
        for char in text:
            code = self.codes[char] if ord(char) >= 0x80 else ord(char)
            out += code.to_bytes(2, "big")
        return bytes(out)

    def cmap(self):
        pairs = [f"<{code:04X}> <{ord(char):04X}>" for char, code in self.codes.items()]
        body = ["/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
                "/CMapName /F2-Identity def", "/CMapType 2 def",
                "1 begincodespacerange", "<0000> <FFFF>", "endcodespacerange",
                f"{len(pairs)} beginbfchar", *pairs, "endbfchar", "endcmap",
                "CMapName currentdict /CMap defineresource pop", "end", "end"]
        return "\n".join(body).encode("latin-1")


def _escape(text):
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_content(lines, registry):
    """lines: list of (font, size, x, y, text) -> content stream bytes."""
    parts = ["BT"]
    current = None
    for font, size, x, y, text in lines:
        non_ascii = any(ord(char) >= 0x80 for char in text)
        wanted = "F2" if non_ascii else font
        if wanted != current:
            parts.append(f"/{wanted} {size} Tf")
            current = wanted
        parts.append(f"1 0 0 1 {x} {y} Tm")
        if non_ascii:
            # Non-ASCII text always goes through the Identity-H font; codes come from the
            # registry, so the CMap is guaranteed to cover every character used.
            encoded = registry.encode(text)
            if _DEBUG:
                print(f"[fixture] {text[:14]!r} -> {encoded.hex().upper()[:40]} "
                      f"(space={registry.codes.get(' ')}, one={registry.codes.get('1')})")
            parts.append(f"<{encoded.hex().upper()}> Tj")
        else:
            parts.append(f"({_escape(text)}) Tj")
    parts.append("ET")
    return "\n".join(parts).encode("latin-1")


# --- documents -------------------------------------------------------------

def zh_lines(page_no, section):
    return [
        ("F2", 9, 72, 760, "管理世界工作论文"),
        ("F2", 12, 72, 736, section),
        ("F2", 10, 72, 700, f"表 {page_no} 主要变量与描述统计"),
        ("F1", 9.5, 72, 682, "变量       均值       标准差      最小值      最大值"),
        ("F1", 9.5, 72, 666, "医疗费用     12345.67   2345.10    -820.55    98765.43"),
        ("F1", 9.5, 72, 650, "费用增长率   -3.20      5.60       -18.90     24.10"),
        ("F1", 9.5, 72, 634, "基层就诊率   32.50%     8.40%      12.10%     58.60%"),
        ("F2", 10, 72, 600, "本文利用大样本病案数据估计分级诊疗制度对医疗费用的影响"),
        ("F2", 10, 72, 584, "结果显示费用下降约 8.3%，且在 5% 水平上显著"),
        ("F1", 8, 300, 60, f"- {page_no} -"),
    ]


def zh_document():
    return [zh_lines(page_no, section) for page_no, section in
            ((1, "分级诊疗制度下的医疗控费机制"), (2, "数据来源与样本构建"), (3, "稳健性检验"))]


def en_document():
    template = [
        ("F1", 11, 72, 760, "Does the effect survive? Evidence from panel data"),
        ("F1", 10, 72, 720, "We estimate that a one percentage point increase in the"),
        ("F1", 10, 72, 704, "minimum wage reduces employment by 0.15% (s.e. 0.04), or"),
        ("F1", 10, 72, 688, "about -12,500 jobs in the average state. The estimated"),
        ("F1", 10, 72, 672, "elasticity is -0.18 with a 95% CI of [-0.26, -0.10]."),
        ("F1", 9.5, 72, 640, "Specification      Estimate    Std. Error   95% CI"),
        ("F1", 9.5, 72, 624, "Baseline           -0.150      0.040        [-0.23, -0.07]"),
        ("F1", 9.5, 72, 608, "With controls      -0.180      0.041        [-0.26, -0.10]"),
        ("F1", 10, 72, 570, "The response is concentrated in manufacturing, where the"),
        ("F1", 10, 72, 554, "informa-"),
        ("F1", 10, 72, 538, "tion on wages is measured at the establishment level."),
    ]
    return [template + [("F1", 8, 300, 60, str(page_no))] for page_no in (1, 2)]


def column_document():
    left = ["The literature offers three competing explanations for the",
            "observed pattern. The first emphasises measurement error in",
            "administrative records, the second points to selective",
            "reporting by establishments, and the third argues that",
            "compositional change drives the entire result."]
    right = ["Table 2. Decomposition of the change",
             "Component          Share     Change",
             "Composition        42.1%     -0.06",
             "Reporting          31.5%     -0.04",
             "Measurement        26.4%     -0.03"]
    pages = []
    for page_no in (1, 2):
        lines = []
        y = 730
        for text in left:
            lines.append(("F1", 10, 60, y, text))
            y -= 16
        y = 730
        for text in right:
            lines.append(("F1", 9.5, 330, y, text))
            y -= 16
        lines.append(("F1", 8, 300, 60, str(page_no)))
        pages.append(lines)
    return pages


# --- file assembly ---------------------------------------------------------

def build_objects(page_streams, registry, with_images=False):
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: ("<< /Type /Pages /Count %d /Kids [ %s ] >>"
            % (len(page_streams), " ".join(f"{20 + index * 3} 0 R"
                                           for index in range(len(page_streams))))).encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Times-Roman /Encoding /WinAnsiEncoding >>",
        4: b"<< /Type /Font /Subtype /Type0 /BaseFont /Synthetic /Encoding /Identity-H "
           b"/DescendantFonts [ 5 0 R ] /ToUnicode 6 0 R >>",
        5: b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Synthetic "
           b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
           b"/FontDescriptor 7 0 R /DW 1000 >>",
        7: b"<< /Type /FontDescriptor /FontName /Synthetic /Flags 4 "
           b"/FontBBox [0 -200 1000 900] /ItalicAngle 0 /Ascent 800 /Descent -200 "
           b"/CapHeight 700 /StemV 80 >>",
    }
    cmap = registry.cmap()
    objects[6] = (b"<< /Length %d >>" % len(cmap), cmap)
    for index, content in enumerate(page_streams):
        page_number = 20 + index * 3
        image_number = page_number + 2
        xobjects = f"/XObject << /Im{index} {image_number} 0 R >>" if with_images else ""
        objects[page_number] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> {xobjects} >> "
            f"/Contents {page_number + 1} 0 R >>").encode()
        objects[page_number + 1] = (b"<< /Length %d >>" % len(content), content)
        if with_images:
            pixels = zlib.compress(bytes([0, 128, 255]) * 200)
            objects[image_number] = (b"<< /Type /XObject /Subtype /Image /Width 10 /Height 10 "
                                     b"/ColorSpace /DeviceRGB /BitsPerComponent 8 "
                                     b"/Filter /FlateDecode /Length %d >>" % len(pixels), pixels)
    return objects


def write_pdf(path, line_pages, with_images=False):
    registry = Registry()
    # Printers embed ASCII in the CID CMap too, so mixed strings such as a table row with
    # digits decode completely. Without this the Identity-H font would drop every number.
    for code in range(0x20, 0x7F):
        registry.codes.setdefault(chr(code), code)
    registry.next_code = FIRST_CODE
    for lines in line_pages:
        for _font, _size, _x, _y, text in lines:
            registry.collect(text)
    streams = [build_content(lines, registry) for lines in line_pages]
    objects = build_objects(streams, registry, with_images=with_images)
    out = bytearray(b"%PDF-1.6\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for number in sorted(objects):
        entry = objects[number]
        body, stream = entry if isinstance(entry, tuple) else (entry, None)
        offsets[number] = len(out)
        out += b"%d 0 obj\n" % number + body
        if stream is not None:
            out += b"\nstream\n" + stream + b"\nendstream"
        out += b"\nendobj\n"
    size = max(objects) + 1
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for number in range(1, size):
        out += (b"%010d 00000 n \n" % offsets[number]) if number in offsets else b"0000000000 65535 f \n"
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref_at)
    path = Path(path)
    path.write_bytes(bytes(out))
    return path


def build_all(directory=None):
    directory = Path(directory or FIXTURES)
    directory.mkdir(parents=True, exist_ok=True)
    return {
        "zh": write_pdf(directory / "zh-headers.pdf", zh_document()),
        "en": write_pdf(directory / "en-hyphen.pdf", en_document()),
        "columns": write_pdf(directory / "columns.pdf", column_document()),
        "blank": write_pdf(directory / "blank.pdf", [[]]),
        "scanned": write_pdf(directory / "scanned.pdf", [[], []], with_images=True),
    }


if __name__ == "__main__":
    for name, path in build_all().items():
        print(f"{name:8s} {path.name:18s} {path.stat().st_size:6d} bytes")
