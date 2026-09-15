"""同一个已确认版本输出可编辑 Word 与 PDF；不调用模型、不读取外部链接。"""

import os
from io import BytesIO
from pathlib import Path
from threading import Lock
from xml.sax.saxutils import escape

from ..schemas import CATEGORY_LABELS, InputError

FONT_LOCK = Lock()


def paragraphs(version):
    yield "title", "个人简历"
    if version["mode"] == "mock":
        yield "note", "离线流程演示 请勿直接投递"
    facts = {f["id"]: f for f in version["snapshot"]["facts"]}
    previous = None
    for block in version["content"]["blocks"]:
        fact = facts[block["fact_id"]]
        category = fact["category"]
        if category != "basic" and category != previous:
            yield "section", CATEGORY_LABELS[category]
        previous = category
        if block["heading"]:
            yield "heading", block["heading"]
        meta = fact.get("metadata", {})
        information = " | ".join(meta[k] for k in ("organization", "role", "period") if meta.get(k))
        if information and category != "basic":
            yield "meta", information
        if category == "basic":
            yield (
                "body",
                " | ".join(line.strip() for line in block["text"].splitlines() if line.strip()),
            )
            continue
        for line in block["text"].splitlines():
            if line.strip():
                yield "body", line.strip()


def docx_bytes(version):
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor

    document = Document()
    # 不继承 Word 安装环境的标题边框、主题字体或中文网格。
    for node in list(document.styles.element.iter(qn("w:pBdr"))):
        node.getparent().remove(node)
    section = document.sections[0]
    section.page_width, section.page_height = Inches(8.5), Inches(11)
    section.top_margin = section.bottom_margin = Inches(0.65)
    section.left_margin = section.right_margin = Inches(0.75)
    for name, size in (("Normal", 11), ("Title", 20), ("Heading 1", 13), ("Heading 2", 11)):
        style = document.styles[name]
        style.font.name = "SimSun"
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor(0, 0, 0)
        fonts = style.element.get_or_add_rPr().rFonts
        for attribute in list(fonts.attrib):
            if "theme" in attribute.lower():
                del fonts.attrib[attribute]
        fonts.set(qn("w:eastAsia"), "SimSun")
        style.paragraph_format.space_after = Pt(2)
        style.paragraph_format.line_spacing = 1.15
    document.styles["Title"].paragraph_format.space_after = Pt(6)
    for name in ("Heading 1", "Heading 2"):
        document.styles[name].font.bold = True
        document.styles[name].paragraph_format.space_before = Pt(6 if name == "Heading 1" else 2)
        document.styles[name].paragraph_format.keep_with_next = True
    for kind, text in paragraphs(version):
        style = {"title": "Title", "section": "Heading 1", "heading": "Heading 2"}.get(
            kind, "Normal"
        )
        p = document.add_paragraph(text, style)
        p.paragraph_format.widow_control = True
        grid = OxmlElement("w:snapToGrid")
        grid.set(qn("w:val"), "0")
        p._p.get_or_add_pPr().append(grid)
        if kind == "meta":
            p.paragraph_format.keep_with_next = True
        # 中文和长英文路径均可在行尾换行，避免 URL 把整行撑出纸面。
        wrap = OxmlElement("w:wordWrap")
        wrap.set(qn("w:val"), "0")
        p._p.get_or_add_pPr().append(wrap)
    document.core_properties.author = ""
    document.core_properties.last_modified_by = ""
    document.core_properties.title = "个人简历"
    document.core_properties.subject = ""
    document.core_properties.comments = ""
    target = BytesIO()
    document.save(target)
    return target.getvalue()


def pdf_font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    with FONT_LOCK:
        if "ResumeCJK" in pdfmetrics.getRegisteredFontNames():
            return "ResumeCJK"
        fonts = [
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/simsun.ttc",
            Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
            Path("/System/Library/Fonts/STHeiti Light.ttc"),
        ]
        for font in fonts:
            if font.is_file():
                pdfmetrics.registerFont(TTFont("ResumeCJK", str(font), subfontIndex=0))
                return "ResumeCJK"
    raise InputError(
        "PDF 导出需要中文字体。Windows 请安装宋体；Linux 请安装文泉驿正黑字体后重试。Word 仍可下载。"
    )


def pdf_bytes(version):
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.platypus import Paragraph, SimpleDocTemplate

    font = pdf_font()
    styles = {}
    for kind in ("title", "section", "heading", "meta", "body", "note"):
        size = {"title": 20, "section": 13}.get(kind, 11)
        styles[kind] = ParagraphStyle(
            kind,
            fontName=font,
            fontSize=size,
            leading=size * 1.2,
            alignment=TA_LEFT,
            wordWrap="CJK",
            splitLongWords=True,
            spaceBefore={"section": 6, "heading": 2}.get(kind, 0),
            spaceAfter=6 if kind == "title" else 2,
            keepWithNext=kind in ("title", "section", "heading", "meta"),
            allowWidows=0,
            allowOrphans=0,
        )
    target = BytesIO()
    document = SimpleDocTemplate(
        target,
        pagesize=letter,
        leftMargin=54,
        rightMargin=54,
        topMargin=46.8,
        bottomMargin=46.8,
        title="个人简历",
        author="",
    )
    story = [Paragraph(escape(text), styles[kind]) for kind, text in paragraphs(version)]
    document.build(
        story, canvasmaker=lambda *args, **kwargs: Canvas(*args, **{**kwargs, "invariant": 1})
    )
    return target.getvalue()
