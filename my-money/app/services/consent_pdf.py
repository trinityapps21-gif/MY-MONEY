"""Builds the PDF copy of a completed loan consent form."""
import io
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _txt(value):
    shown = str(value) if value not in (None, "") else "-"
    return escape(shown).replace("\n", "<br/>")


def _answer_html(value):
    if isinstance(value, list):
        return "<br/>".join("&bull; " + escape(str(v)) for v in value) if value else "-"
    return _txt(value)


def build_pdf(group, form, response, submitted_at):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm,
                            bottomMargin=20 * mm, title=form.title, author=group.name)
    base = getSampleStyleSheet()
    brand = ParagraphStyle("brand", parent=base["Normal"], fontSize=9, textColor=colors.HexColor("#6b7280"))
    title = ParagraphStyle("title", parent=base["Title"], fontSize=18, leading=22, alignment=0, spaceAfter=6)
    body = ParagraphStyle("body", parent=base["Normal"], fontSize=10, leading=14)
    q_style = ParagraphStyle("q", parent=body, fontName="Helvetica-Bold", spaceBefore=8, spaceAfter=2)
    a_style = ParagraphStyle("a", parent=body, leftIndent=6)

    story = [Paragraph(_txt(group.name), brand), Paragraph(_txt(form.title), title)]
    if form.intro:
        story += [Paragraph(_txt(form.intro), body), Spacer(1, 8)]

    when = submitted_at.strftime("%d %b %Y, %H:%M") if submitted_at else "-"
    meta = Table([["Reference", response.reference], ["Submitted", when]], colWidths=[28 * mm, None])
    meta.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#6b7280")),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story += [meta, Spacer(1, 6)]

    for item in response.answers:
        story.append(KeepTogether([
            Paragraph(_txt(item.get("label")), q_style),
            Paragraph(_answer_html(item.get("value")), a_style),
        ]))

    def footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#6b7280"))
        canvas.drawString(20 * mm, 12 * mm, f"{group.name}  |  Ref {response.reference}")
        canvas.drawRightString(A4[0] - 20 * mm, 12 * mm, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
