"""CSV / Excel / PDF rendering for reports and PDF receipts.

Every document carries the MY MONEY brand, the Emtrixz Technology Company line,
and the group's own logo, name and currency."""
import csv
import io
from datetime import date, datetime
from decimal import Decimal

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab import rl_config
from reportlab.graphics import renderPDF
from reportlab.graphics.shapes import Drawing, Line, PolyLine, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.services import storage
from app.services.money import from_minor, fmt
from app.services.timeutil import to_local

rl_config.invariant = 1  # byte-identical PDFs for identical content (stable checksums)

NAVY, DEEP, BLUE = colors.HexColor("#101C36"), colors.HexColor("#172747"), colors.HexColor("#315A91")
GREY, BORDER = colors.HexColor("#F3F5F8"), colors.HexColor("#E4E8EF")
BRAND = "MY MONEY"
COMPANY = "Developed and Powered by Emtrixz Technology Company."


# --------------------------------------------------------------------- helpers
def _plain(value, kind, group):
    """Value as a native spreadsheet/CSV cell."""
    if value is None or value == "":
        return ""
    if kind == "money":
        return float(from_minor(value, group.currency_decimals)) if group.currency_decimals else int(value)
    if kind == "pct":
        return float(value)
    return value


def _text(value, kind, group):
    if value is None or value == "":
        return ""
    if kind == "money":
        return fmt(value, group.currency, group.currency_decimals, prefix=False)
    if kind == "date" and isinstance(value, (date, datetime)):
        return value.strftime("%d %b %Y")
    if kind == "pct":
        return f"{value:g}%"
    return str(value)


def _filename(report, ext):
    safe = "".join(c if c.isalnum() else "_" for c in report.title).strip("_").lower()
    return f"{safe}.{ext}"


def _logo_reader(group):
    from app.models import StoredFile
    from app.extensions import db
    if not group.logo_file_id:
        return None
    try:
        f = db.session.get(StoredFile, group.logo_file_id)
        return ImageReader(io.BytesIO(storage.read_bytes(f))) if f and not f.deleted_at else None
    except Exception:
        return None


# ------------------------------------------------------------------------- CSV
def to_csv(report, group) -> bytes:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([BRAND, COMPANY])
    w.writerow([group.name, report.title, report.period, group.currency])
    w.writerow([])
    for label, value, kind in report.summary:
        w.writerow([label, _plain(value, kind, group)])
    if report.summary:
        w.writerow([])
    w.writerow([h for h, _ in report.columns])
    for row in report.rows:
        w.writerow([_plain(v, k, group) if k != "date" else (v.isoformat() if v else "")
                    for v, (_, k) in zip(row, report.columns)])
    return ("﻿" + out.getvalue()).encode("utf-8")  # BOM so Excel opens UTF-8 correctly


# ----------------------------------------------------------------------- Excel
def to_xlsx(report, group) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = report.key[:30]
    navy = PatternFill("solid", fgColor="101C36")
    ws.append([BRAND])
    ws["A1"].font = Font(bold=True, size=16, color="101C36")
    ws.append([COMPANY])
    ws.append([group.name])
    ws["A3"].font = Font(bold=True, size=12)
    ws.append([report.title, report.period, f"Currency: {group.currency}"])
    ws.append([])
    for label, value, kind in report.summary:
        ws.append([label, _plain(value, kind, group)])
        ws.cell(ws.max_row, 1).font = Font(bold=True)
        if kind == "money":
            ws.cell(ws.max_row, 2).number_format = "#,##0" if not group.currency_decimals else "#,##0.00"
    ws.append([])
    header_row = ws.max_row + 1
    ws.append([h for h, _ in report.columns])
    for c in range(1, len(report.columns) + 1):
        cell = ws.cell(header_row, c)
        cell.font, cell.fill = Font(bold=True, color="FFFFFF"), navy
    for row in report.rows:
        ws.append([_plain(v, k, group) if k != "date" else v for v, (_, k) in zip(row, report.columns)])
        r = ws.max_row
        for c, (_, k) in enumerate(report.columns, 1):
            cell = ws.cell(r, c)
            if k == "money":
                cell.number_format = "#,##0" if not group.currency_decimals else "#,##0.00"
            elif k == "date":
                cell.number_format = "DD MMM YYYY"
    for n, (h, _) in enumerate(report.columns, 1):
        width = max([len(str(h))] + [len(_text(r[n - 1], report.columns[n - 1][1], group)) for r in report.rows[:200]])
        ws.column_dimensions[get_column_letter(n)].width = min(max(width + 3, 12), 45)
    ws.column_dimensions["A"].width = max(ws.column_dimensions["A"].width or 0, 34)
    ws.freeze_panes = ws.cell(header_row + 1, 1)
    for n in report.notes:
        ws.append([n])
    ws.append([])
    ws.append([f"{BRAND} - {COMPANY}"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------------- PDF
def _brand_mark(size=11 * mm):
    """The MY MONEY logo (static/img/logo.png); falls back to a drawn mark if the file is missing."""
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "img", "logo.png")
    if os.path.exists(path):
        return Image(path, size, size)
    d = Drawing(size, size)
    d.add(Rect(0, 0, size, size, rx=2.2 * mm, ry=2.2 * mm, fillColor=NAVY, strokeColor=NAVY))
    return d


def _styles():
    ss = getSampleStyleSheet()
    return {
        "brand": ParagraphStyle("brand", parent=ss["Normal"], fontName="Helvetica-Bold", fontSize=15, textColor=NAVY),
        "company": ParagraphStyle("company", parent=ss["Normal"], fontSize=7.5, textColor=BLUE),
        "group": ParagraphStyle("group", parent=ss["Normal"], fontName="Helvetica-Bold", fontSize=13, textColor=NAVY,
                                alignment=2),
        "small": ParagraphStyle("small", parent=ss["Normal"], fontSize=8, textColor=colors.HexColor("#555555")),
        "title": ParagraphStyle("title", parent=ss["Heading1"], fontName="Helvetica-Bold", fontSize=15,
                                textColor=NAVY, spaceAfter=2),
        "cell": ParagraphStyle("cell", parent=ss["Normal"], fontSize=8, leading=10),
        "cellr": ParagraphStyle("cellr", parent=ss["Normal"], fontSize=8, leading=10, alignment=2),
        "note": ParagraphStyle("note", parent=ss["Normal"], fontSize=8, textColor=colors.HexColor("#444444"),
                               leading=11),
    }


def _header(group, width, st, subtitle=None):
    brand = Table([[_brand_mark(), [Paragraph(BRAND, st["brand"]), Paragraph(COMPANY, st["company"])]]],
                  colWidths=[14 * mm, None])
    brand.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    right = [Paragraph(group.name, st["group"])]
    if subtitle:
        right.append(Paragraph(subtitle, ParagraphStyle("r", parent=st["small"], alignment=2)))
    logo = _logo_reader(group)
    cells = [[brand, right, Image(logo, 14 * mm, 14 * mm) if logo else ""]]
    t = Table(cells, colWidths=[width * 0.48, width * 0.40, width * 0.12 if logo else 1])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (2, 0), (2, 0), "RIGHT"),
                           ("LINEBELOW", (0, 0), (-1, 0), 1.2, NAVY), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return t


def _footer_factory(group, tz_note):
    def draw(canvas, doc):
        canvas.saveState()
        w, _h = doc.pagesize
        canvas.setStrokeColor(BORDER)
        canvas.line(doc.leftMargin, 14 * mm, w - doc.rightMargin, 14 * mm)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#555555"))
        canvas.drawString(doc.leftMargin, 10 * mm, f"{BRAND} - {COMPANY}")
        canvas.drawRightString(w - doc.rightMargin, 10 * mm, f"{group.name}  |  Page {doc.page}")
        canvas.restoreState()
    return draw


def to_pdf(report, group, generated_at, generated_by) -> bytes:
    st = _styles()
    wide = len(report.columns) > 6
    page = landscape(A4) if wide else A4
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm,
                            bottomMargin=20 * mm, title=report.title, author=BRAND)
    width = page[0] - 28 * mm
    local = to_local(generated_at, group.timezone)
    story = [_header(group, width, st, f"Currency: {group.currency}"), Spacer(1, 4 * mm),
             Paragraph(report.title, st["title"]),
             Paragraph(f"{report.period}  |  Generated {local:%d %b %Y %H:%M} by {generated_by}", st["small"]),
             Spacer(1, 4 * mm)]
    if report.summary:
        srows = [[Paragraph(l, st["cell"]), Paragraph(f"<b>{_text(v, k, group) if k != 'money' else fmt(v, group.currency, group.currency_decimals)}</b>", st["cellr"])]
                 for l, v, k in report.summary]
        s = Table(srows, colWidths=[width * 0.6, width * 0.4], hAlign="LEFT")
        s.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), GREY), ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
                               ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        story += [s, Spacer(1, 5 * mm)]
    data = [[Paragraph(f"<b>{h}</b>", ParagraphStyle("h", parent=st["cell"], textColor=colors.white,
                                                      alignment=2 if k in ("money", "int", "pct") else 0))
             for h, k in report.columns]]
    for row in report.rows:
        data.append([Paragraph(_text(v, k, group), st["cellr"] if k in ("money", "int", "pct") else st["cell"])
                     for v, (_, k) in zip(row, report.columns)])
    if not report.rows:
        data.append([Paragraph("No records for this period.", st["cell"])] + [""] * (len(report.columns) - 1))
    t = Table(data, repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), NAVY), ("GRID", (0, 0), (-1, -1), 0.3, BORDER),
                           ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, GREY]),
                           ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 3),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story.append(t)
    for n in report.notes:
        story += [Spacer(1, 2 * mm), Paragraph(n, st["note"])]
    foot = _footer_factory(group, "")
    doc.build(story, onFirstPage=foot, onLaterPages=foot)
    return buf.getvalue()


# --------------------------------------------------------------------- receipts
def receipt_pdf(group, title, receipt_no, fields, amount_minor, generated_at, extra_note=None) -> bytes:
    """One-page A5 receipt. ``fields`` is a list of (label, value)."""
    st = _styles()
    page = (148 * mm, 210 * mm)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=10 * mm,
                            bottomMargin=20 * mm, title=f"{title} {receipt_no}", author=BRAND)
    width = page[0] - 24 * mm
    story = [_header(group, width, st), Spacer(1, 5 * mm),
             Paragraph(title.upper(), st["title"]), Paragraph(f"No. {receipt_no}", st["small"]), Spacer(1, 4 * mm)]
    amt = Table([[Paragraph("AMOUNT", st["small"]),
                  Paragraph(f"<b>{fmt(amount_minor, group.currency, group.currency_decimals)}</b>",
                            ParagraphStyle("a", parent=st["title"], fontSize=17, alignment=2))]],
                colWidths=[width * 0.3, width * 0.7])
    amt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), GREY), ("BOX", (0, 0), (-1, -1), 0.6, NAVY),
                             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 8),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story += [amt, Spacer(1, 4 * mm)]
    rows = [[Paragraph(f"<b>{l}</b>", st["cell"]), Paragraph(str(v) if v not in (None, "") else "-", st["cell"])]
            for l, v in fields]
    t = Table(rows, colWidths=[width * 0.36, width * 0.64])
    t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.3, BORDER), ("TOPPADDING", (0, 0), (-1, -1), 4),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(t)
    if extra_note:
        story += [Spacer(1, 4 * mm), Paragraph(extra_note, st["note"])]
    local = to_local(generated_at, group.timezone)
    story += [Spacer(1, 6 * mm), Paragraph(f"Receipt issued {local:%d %b %Y %H:%M}. This is a computer-generated "
                                           "receipt from the group's records.", st["small"])]
    foot = _footer_factory(group, "")
    doc.build(story, onFirstPage=foot, onLaterPages=foot)
    return buf.getvalue()


# ------------------------------------------------------------ loan schedule PDF
def loan_schedule_pdf(group, loan, generated_at, fine_rule_text=None, fines=None) -> bytes:
    """Payment schedule an official can download and give to the borrower."""
    st = _styles()
    page = A4
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm,
                            bottomMargin=20 * mm, title=f"Payment schedule {loan.reference}", author=BRAND)
    width = page[0] - 28 * mm
    cur, dec = group.currency, group.currency_decimals
    m = lambda v: fmt(v, cur, dec)  # noqa: E731
    local = to_local(generated_at, group.timezone)
    b = loan.borrower
    story = [_header(group, width, st, f"Currency: {cur}"), Spacer(1, 4 * mm),
             Paragraph("Loan Payment Schedule", st["title"]),
             Paragraph(f"Loan {loan.reference}  |  Generated {local:%d %b %Y %H:%M}", st["small"]), Spacer(1, 4 * mm)]
    rate = f"{loan.rate_bp / 100:g}% per {loan.rate_period.lower()}"
    info = [
        ["Borrower", b.name, "Loan amount", m(loan.principal)],
        ["Phone", b.phone or "-", "Interest", f"{rate} ({'Flat' if loan.method == 'FLAT' else 'Reducing balance'})"],
        ["Location", b.location or "-", "Total interest", m(loan.total_interest)],
        ["Disbursed", f"{loan.disbursement_date:%d %b %Y}", "Total to repay", m(loan.total_repayable)],
        ["Instalments", f"{len(loan.schedule)} ({loan.frequency.lower()})", "Purpose", loan.purpose or "-"],
    ]
    t = Table([[Paragraph(f"<b>{r[0]}</b>", st["cell"]), Paragraph(str(r[1]), st["cell"]),
                Paragraph(f"<b>{r[2]}</b>", st["cell"]), Paragraph(str(r[3]), st["cell"])] for r in info],
              colWidths=[width * 0.14, width * 0.36, width * 0.16, width * 0.34])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), GREY), ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
                           ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story += [t, Spacer(1, 5 * mm)]
    head = ["#", "Due date", "Principal", "Interest", "Instalment", "Paid", "Balance due"]
    data = [[Paragraph(f"<b>{h}</b>", ParagraphStyle("h", parent=st["cell"], textColor=colors.white,
                                                      alignment=0 if i < 2 else 2)) for i, h in enumerate(head)]]
    tp = ti = tt = tpaid = tbal = 0
    for s in loan.schedule:
        due, paid, bal = s.total_due, s.total_paid, s.balance
        tp, ti, tt, tpaid, tbal = tp + s.principal_due, ti + s.interest_due, tt + due, tpaid + paid, tbal + bal
        data.append([Paragraph(str(s.number), st["cell"]), Paragraph(f"{s.due_date:%d %b %Y}", st["cell"]),
                     Paragraph(m(s.principal_due), st["cellr"]), Paragraph(m(s.interest_due), st["cellr"]),
                     Paragraph(m(due), st["cellr"]), Paragraph(m(paid), st["cellr"]), Paragraph(m(bal), st["cellr"])])
    data.append([Paragraph("<b>Total</b>", st["cell"]), "", Paragraph(f"<b>{m(tp)}</b>", st["cellr"]),
                 Paragraph(f"<b>{m(ti)}</b>", st["cellr"]), Paragraph(f"<b>{m(tt)}</b>", st["cellr"]),
                 Paragraph(f"<b>{m(tpaid)}</b>", st["cellr"]), Paragraph(f"<b>{m(tbal)}</b>", st["cellr"])])
    sch = Table(data, repeatRows=1, colWidths=[width * x for x in (0.06, 0.15, 0.17, 0.14, 0.17, 0.14, 0.17)])
    sch.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), NAVY), ("GRID", (0, 0), (-1, -1), 0.3, BORDER),
                             ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, GREY]),
                             ("BACKGROUND", (0, -1), (-1, -1), GREY), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                             ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story.append(sch)
    open_fines = [f for f in (fines or []) if f.status == "UNPAID"]
    if open_fines:
        story += [Spacer(1, 4 * mm), Paragraph(
            "<b>Late-payment fines still to pay:</b> " + ", ".join(
                f"{m(f.amount)} ({f.days_late} days late)" for f in open_fines), st["note"])]
    if fine_rule_text:
        story += [Spacer(1, 3 * mm), Paragraph(fine_rule_text, st["note"])]
    story += [Spacer(1, 3 * mm), Paragraph(
        "Please pay each instalment on or before its due date. Keep your receipt for every payment. "
        f"For questions contact {group.name}{(' on ' + group.phone) if group.phone else ''}.", st["note"])]
    foot = _footer_factory(group, "")
    doc.build(story, onFirstPage=foot, onLaterPages=foot)
    return buf.getvalue()
