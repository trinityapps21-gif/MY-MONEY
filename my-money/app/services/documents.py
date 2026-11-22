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
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

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


def late_fee_notice_pdf(group, loan, a, notice_date) -> bytes:
    """Formal 'Overdue Loan & Late Fine Notice' for a borrower. `a` is a late_fees.assess() result."""
    st = _styles()
    page = A4
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=12 * mm,
                            bottomMargin=20 * mm, title=f"Overdue Loan & Late Fine Notice {loan.reference}", author=BRAND)
    width = page[0] - 32 * mm
    cur, dec = group.currency, group.currency_decimals
    m = lambda v: fmt(v, cur, dec)  # noqa: E731
    b = loan.borrower
    from xml.sax.saxutils import escape as esc
    cell, cellr = st["cell"], st["cellr"]
    big = ParagraphStyle("big", parent=cell, fontName="Helvetica-Bold", fontSize=10.5, leading=13)
    bigr = ParagraphStyle("bigr", parent=big, alignment=2)
    sec = ParagraphStyle("sec", parent=st["title"], fontSize=10.5, textColor=NAVY, spaceBefore=6, spaceAfter=3)

    story = [_header(group, width, st), Spacer(1, 4 * mm),
             Paragraph("Overdue Loan &amp; Late Fine Notice", st["title"]), Spacer(1, 2 * mm)]
    head = Table([[Paragraph("<b>Company Name</b>", cell), Paragraph(esc(group.name), cell),
                   Paragraph("<b>Notice Date</b>", cell), Paragraph(f"{notice_date:%d %B %Y}", cell)],
                  [Paragraph("<b>Loan Reference ID</b>", cell), Paragraph(esc(loan.reference), cell),
                   Paragraph("<b>Currency</b>", cell), Paragraph(cur, cell)]],
                 colWidths=[width * 0.20, width * 0.32, width * 0.18, width * 0.30])
    head.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), GREY), ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
                              ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    story += [head, Paragraph("Borrower Details", sec)]
    contact = " | ".join(esc(x) for x in (b.phone, getattr(b, "email", None), b.location) if x) or "-"
    bt = Table([[Paragraph("<b>Full Name</b>", cell), Paragraph(esc(b.name), cell)],
                [Paragraph("<b>Contact Info</b>", cell), Paragraph(contact, cell)]],
               colWidths=[width * 0.20, width * 0.80])
    bt.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, BORDER), ("TOPPADDING", (0, 0), (-1, -1), 4),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("BACKGROUND", (0, 0), (0, -1), GREY)]))
    story += [bt, Paragraph("Repayment Breakdown", sec)]
    from app.services import fine_terms
    lt = a["terms"]
    gd, pd_ = a["grace_days"], a["period_days"]
    if lt.fine_type == "FIXED":
        tier = f"{m(lt.fine_fixed)} every {pd_} day(s) after grace (currently {a['blocks']} charged)"
    else:
        tier = f"{lt.fine_rate_bp / 100:g}% every {pd_} day(s) after grace (currently {a['tier_pct']:g}% applied)"
    rows = [("Original Repayment Amount", m(a["amount"])),
            ("Original Due Date", f"{a['due_date']:%d %B %Y}"),
            (f"Grace Period End Date (+{gd} Days)", f"{a['grace_end']:%d %B %Y}"),
            ("Current Days Past Due", f"{a['days_overdue']} day(s)"),
            ("Late Fine Rate Applied", tier),
            ("Accumulated Late Fine", m(a["fine"]))]
    data = [[Paragraph(f"<b>{k}</b>", cell), Paragraph(v, cellr)] for k, v in rows]
    data.append([Paragraph("TOTAL AMOUNT PAYABLE", big), Paragraph(m(a["total"]), bigr)])
    bd = Table(data, colWidths=[width * 0.55, width * 0.45])
    bd.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, BORDER), ("ROWBACKGROUNDS", (0, 0), (-1, -2), [colors.white, GREY]),
                            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#FFF4D6")), ("LINEABOVE", (0, -1), (-1, -1), 1.2, NAVY),
                            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                            ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story += [bd]
    if a["capped"]:
        story += [Spacer(1, 2 * mm), Paragraph("<b>The maximum late fine (the original late amount) has been reached.</b>", st["note"])]
    story += [Paragraph("Policy Reminder", sec), Paragraph(esc(fine_terms.describe(lt, cur, dec)), st["note"])]
    pay = esc((group.pay_instructions or "").strip()).replace("\n", "<br/>") or esc(
        f"Please contact {group.name}" + (f" on {group.phone}" if getattr(group, "phone", None) else "") + " for payment details.")
    nxt = (f" The next penalty increase applies on <b>{a['next_increase']:%d %B %Y}</b>." if a["next_increase"] else "")
    story += [Paragraph("Payment Instructions", sec), Paragraph(pay, st["note"]), Spacer(1, 2 * mm),
              Paragraph(f"<b>Please settle the total amount payable immediately</b> to avoid the next penalty increase.{nxt} "
                        "Keep your receipt for every payment.", st["note"])]
    foot = _footer_factory(group, "")
    doc.build(story, onFirstPage=foot, onLaterPages=foot)
    return buf.getvalue()


def late_fee_statement_pdf(group, stmt) -> bytes:
    """'Overdue Loan Fine Statement': everything a borrower needs to understand why they are paying a late fine.

    `stmt` is a late_fees.statement() result. The page shows the principal before the fine, the fine, the new
    principal after the fine, the policy that produced it, and the working for every overdue instalment."""
    from xml.sax.saxutils import escape as esc
    from app.services.money import rate_pct
    st = _styles()
    page = A4
    buf = io.BytesIO()
    loan, b, today = stmt["loan"], stmt["borrower"], stmt["date"]
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=12 * mm,
                            bottomMargin=20 * mm, title=f"Overdue Loan Fine Statement {loan.reference}", author=BRAND)
    width = page[0] - 32 * mm
    cur, dec = group.currency, group.currency_decimals
    m = lambda v: fmt(v, cur, dec)  # noqa: E731
    cell, cellr, note = st["cell"], st["cellr"], st["note"]
    big = ParagraphStyle("sbig", parent=cell, fontName="Helvetica-Bold", fontSize=10.5, leading=13)
    bigr = ParagraphStyle("sbigr", parent=big, alignment=2)
    sec = ParagraphStyle("ssec", parent=st["title"], fontSize=10.5, textColor=NAVY, spaceBefore=8, spaceAfter=3)
    head_c = ParagraphStyle("shead", parent=cell, fontName="Helvetica-Bold", textColor=colors.white)
    head_r = ParagraphStyle("sheadr", parent=head_c, alignment=2)
    muted = ParagraphStyle("smuted", parent=note, fontSize=7.5, textColor=colors.HexColor("#666666"))
    from app.services import fine_terms
    lt, tinfo = stmt["terms"], stmt["terms_info"]
    fixed = lt.fine_type == "FIXED"
    gd, pd_ = lt.fine_grace_days, max(1, lt.fine_period_days)
    started = stmt["mode"] == "STARTED"
    kind = "started" if started else "completed"
    per = {"WEEK": "per week", "MONTH": "per month", "YEAR": "per year"}.get(loan.rate_period, "")

    story = [_header(group, width, st), Spacer(1, 4 * mm),
             Paragraph("Overdue Loan Fine Statement", st["title"]),
             Paragraph(f"Statement date {today:%d %B %Y}  |  Loan {esc(loan.reference)}  |  Currency {cur}", st["small"]),
             Spacer(1, 3 * mm)]

    # ---- who and which loan
    contact = " | ".join(esc(x) for x in (b.phone, getattr(b, "email", None), b.location) if x) or "-"
    info = Table([
        [Paragraph("<b>Borrower</b>", cell), Paragraph(esc(b.name), cell), Paragraph("<b>Loan Reference</b>", cell), Paragraph(esc(loan.reference), cell)],
        [Paragraph("<b>Contact</b>", cell), Paragraph(contact, cell), Paragraph("<b>Loan Amount</b>", cell), Paragraph(m(loan.principal), cell)],
        [Paragraph("<b>Lender</b>", cell), Paragraph(esc(group.name), cell), Paragraph("<b>Interest Rate</b>", cell),
         Paragraph(f"{rate_pct(loan.rate_bp)}% {per}".strip(), cell)],
        [Paragraph("<b>Disbursed</b>", cell), Paragraph(f"{loan.disbursement_date:%d %B %Y}", cell), Paragraph("<b>Paid To Date</b>", cell),
         Paragraph(m(stmt["paid_to_date"]), cell)],
    ], colWidths=[width * 0.16, width * 0.34, width * 0.18, width * 0.32])
    info.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, BORDER), ("BACKGROUND", (0, 0), (0, -1), GREY),
                              ("BACKGROUND", (2, 0), (2, -1), GREY), ("TOPPADDING", (0, 0), (-1, -1), 4),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    story += [info, Paragraph("Summary: before and after the fine", sec)]

    # ---- the headline: principal before -> fine -> principal after
    summ = Table([
        [Paragraph("<b>Initial principal due (before the fine)</b>", cell), Paragraph(m(stmt["principal_before"]), cellr)],
        [Paragraph("<b>Late fine added</b>", cell), Paragraph("+ " + m(stmt["fine"]), cellr)],
        [Paragraph("NEW PRINCIPAL DUE (after the fine)", big), Paragraph(m(stmt["principal_after"]), bigr)],
    ], colWidths=[width * 0.60, width * 0.40])
    summ.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, BORDER), ("ROWBACKGROUNDS", (0, 0), (-1, 1), [colors.white, GREY]),
                              ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#FFF4D6")), ("LINEABOVE", (0, -1), (-1, -1), 1.2, NAVY),
                              ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                              ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story += [summ, Spacer(1, 1.5 * mm), Paragraph(
        "\"Principal due\" is the amount still unpaid on the overdue instalment(s), including each instalment's own interest. "
        "It is the amount the fine is charged on.", muted), Spacer(1, 2 * mm)]
    whole = Table([
        [Paragraph("Whole loan: balance still owed before the fine", cell), Paragraph(m(stmt["balance_before"]), cellr)],
        [Paragraph("Whole loan: balance still owed after the fine", cell), Paragraph("<b>" + m(stmt["balance_after"]) + "</b>", cellr)],
    ], colWidths=[width * 0.60, width * 0.40])
    whole.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, BORDER), ("BACKGROUND", (0, 0), (-1, -1), GREY),
                               ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story += [whole, Paragraph("Why this fine applies", sec)]

    # ---- the reason, in plain words
    n = len(stmt["lines"])
    story += [Paragraph(
        f"{esc(group.name)} is owed <b>{n}</b> instalment{'s' if n != 1 else ''} on loan {esc(loan.reference)} that "
        f"{'were' if n != 1 else 'was'} not paid by the agreed due date{'s' if n != 1 else ''}. The longest is "
        f"<b>{stmt['longest_days']} day(s)</b> overdue. Under the lender's late-payment policy:", note),
        Spacer(1, 1.5 * mm),
        Paragraph("&bull; " + esc(fine_terms.describe(lt, cur, dec)) +
                  (f" A period is charged {'as soon as it starts' if started else 'once its ' + str(pd_) + ' days are complete'}."
                   if not fine_terms.is_free(lt) else ""), note),
        Paragraph("&bull; Once an instalment is paid in full, no further fine is added on it.", note),
        ]
    if tinfo["older"]:
        since = f" on {tinfo['since']:%d %B %Y}" if tinfo["since"] else ""
        story += [Spacer(1, 1.5 * mm), Paragraph(
            f"<b>Please note:</b> {esc(group.name)} changed its late-fine terms{since}. This loan keeps the terms it was "
            "created with (shown above); the newer terms apply only to loans created after the change.", note)]
    story.append(Paragraph("How each fine was calculated", sec))

    # ---- per-instalment table
    hdr = [Paragraph("Inst.", head_c), Paragraph("Due date", head_c), Paragraph("Days late", head_r), Paragraph("Unpaid amount", head_r),
           Paragraph("Blocks", head_r), Paragraph("Fine rate", head_r), Paragraph("Fine", head_r), Paragraph("New amount due", head_r)]
    data = [hdr]
    for x in stmt["lines"]:
        rate = f"{x['tier_pct']:g}%" + (" (cap)" if x["capped"] else "")
        data.append([Paragraph(f"#{x['number']}", cell), Paragraph(f"{x['due_date']:%d %b %Y}", cell),
                     Paragraph(str(x["days_overdue"]), cellr), Paragraph(m(x["amount"]), cellr), Paragraph(str(x["blocks"]), cellr),
                     Paragraph(rate, cellr), Paragraph(m(x["fine"]), cellr), Paragraph("<b>" + m(x["total"]) + "</b>", cellr)])
    data.append([Paragraph("<b>Total</b>", cell), "", "", Paragraph("<b>" + m(stmt["principal_before"]) + "</b>", cellr), "", "",
                 Paragraph("<b>" + m(stmt["fine"]) + "</b>", cellr), Paragraph("<b>" + m(stmt["principal_after"]) + "</b>", cellr)])
    cw = [0.07, 0.15, 0.09, 0.19, 0.08, 0.11, 0.14, 0.17]
    tbl = Table(data, colWidths=[width * c for c in cw], repeatRows=1)
    tbl.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), NAVY), ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
                             ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, GREY]),
                             ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#FFF4D6")), ("LINEABOVE", (0, -1), (-1, -1), 1.0, NAVY),
                             ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                             ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story += [tbl, Spacer(1, 2.5 * mm), Paragraph("<b>The working, instalment by instalment</b>", note), Spacer(1, 1 * mm)]
    for x in stmt["lines"]:
        if x["blocks"] == 0:
            first = x["next_increase"]
            why = (f"still inside the {gd}-day grace period" if x["days_overdue"] <= gd
                   else f"past the grace period, but no full {pd_}-day period has been completed yet")
            if fine_terms.is_free(lt):
                why = "this loan's terms charge no late fine"
            work = (f"{m(x['amount'])}: no fine yet ({why})." +
                    (f" The first fine applies from <b>{first:%d %B %Y}</b>." if first else ""))
        else:
            unit = f"{kind} {pd_}-day period{'s' if x['blocks'] != 1 else ''}"
            if fixed:
                work = f"{m(lt.fine_fixed)} &times; {x['blocks']} {unit} = <b>{m(x['fine'])}</b>"
            else:
                work = (f"{m(x['amount'])} &times; {lt.fine_rate_bp / 100:g}% &times; {x['blocks']} {unit} "
                        f"= {x['raw_pct']:g}% = <b>{m(x['fine'])}</b>")
            work += " (limited to the cap, the unpaid amount)." if x["capped"] else "."
        story.append(Paragraph(f"&bull; <b>Instalment #{x['number']}</b> (due {x['due_date']:%d %b %Y}, grace ended {x['grace_end']:%d %b %Y}): {work}", note))

    # ---- what happens next, how to pay
    story.append(Paragraph("What happens next", sec))
    nxt = stmt["next_increase"]
    story.append(Paragraph(
        (f"The next penalty increase applies on <b>{nxt:%d %B %Y}</b> if the overdue amount is still unpaid. " if nxt else
         "The maximum fine has been reached on every overdue instalment, so it will not grow any further. ") +
        "The figures above are correct as at the statement date and change as more days pass or payments are made.", note))
    pay = esc((group.pay_instructions or "").strip()).replace("\n", "<br/>") or esc(
        f"Please contact {group.name}" + (f" on {group.phone}" if getattr(group, "phone", None) else "") + " for payment details.")
    story.append(KeepTogether([Paragraph("How to pay", sec), Paragraph(pay, note), Spacer(1, 2 * mm), Paragraph(
        f"<b>Please settle the new principal due of {m(stmt['principal_after'])} as soon as possible</b> to avoid further fines. "
        "Keep your receipt for every payment. If you believe any figure on this statement is wrong, or you have already "
        f"paid, contact {esc(group.name)}{(' on ' + esc(group.phone)) if getattr(group, 'phone', None) else ''} with your payment receipt.", note)]))
    foot = _footer_factory(group, "")
    doc.build(story, onFirstPage=foot, onLaterPages=foot)
    return buf.getvalue()
