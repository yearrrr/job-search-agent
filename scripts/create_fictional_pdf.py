"""使用 reportlab 生成公开虚构测试 PDF；不读取个人目录、配置或密钥。"""

from pathlib import Path
from shutil import copyfile
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "src/job_search_agent/sample_data"
OUTPUT = ROOT / "output/pdf/fictional-resume.pdf"


def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    pdfmetrics.registerFont(TTFont("Chinese", "C:/Windows/Fonts/simsun.ttc", subfontIndex=0))
    body = ParagraphStyle(
        "body",
        fontName="Chinese",
        fontSize=10,
        leading=16,
        textColor=colors.HexColor("#253d35"),
        spaceAfter=8,
        wordWrap="CJK",
        alignment=TA_LEFT,
    )
    heading = ParagraphStyle(
        "heading", parent=body, fontSize=12, leading=18, spaceBefore=10, spaceAfter=6
    )
    title = ParagraphStyle("title", parent=body, fontSize=23, leading=30, spaceAfter=12)
    warning = ParagraphStyle(
        "warning", parent=body, fontSize=9, leading=14, textColor=colors.HexColor("#87642c")
    )
    story = []
    for line in (SAMPLES / "fictional-resume.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            story.extend(
                [
                    Paragraph(escape(line[2:]), title),
                    HRFlowable(width="100%", thickness=1, color=colors.HexColor("#78966a")),
                    Spacer(1, 4 * mm),
                ]
            )
        elif line.startswith("## "):
            story.append(Paragraph(escape(line[3:]), heading))
        elif line:
            story.append(Paragraph(escape(line), warning if line.startswith("虚构测试") else body))

    def footer(canvas, document):
        canvas.setFont("Chinese", 8)
        canvas.setFillColor(colors.HexColor("#72816a"))
        canvas.drawString(
            19 * mm, 12 * mm, "虚构测试材料 | 2027 届 AI 应用开发 / AI Agent 工程师方向"
        )
        canvas.drawRightString(191 * mm, 12 * mm, f"{document.page}")

    SimpleDocTemplate(
        str(OUTPUT),
        pagesize=(210 * mm, 297 * mm),
        rightMargin=19 * mm,
        leftMargin=19 * mm,
        topMargin=17 * mm,
        bottomMargin=20 * mm,
        title="虚构简历 - 林知遥",
        author="Fictional test fixture",
    ).build(story, onFirstPage=footer, onLaterPages=footer)
    copyfile(OUTPUT, SAMPLES / OUTPUT.name)
    print("Fictional PDF created and copied to packaged samples.")


if __name__ == "__main__":
    main()
