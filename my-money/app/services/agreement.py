"""Loan agreement PDF for lending businesses: parties, terms, fees, penalty, schedule,
guarantors, collateral and signature lines. It is a standard template, not legal advice:
the business should have it reviewed by a lawyer."""
import io

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.models import Collateral, Guarantor
from app.services import documents as doc
from app.services.money import fmt
from app.services.timeutil import to_local

PERIOD = {"WEEK": "per week", "MONTH": "per month", "YEAR": "per year"}
UNIT = {"WEEK": "weeks", "MONTH": "months"}
FREQ = {"WEEKLY": "weekly", "MONTHLY": "monthly"}


def build(group, loan, generated_at, generated_by) -> bytes:
    st = doc._styles()
    st["h2"] = ParagraphStyle("h2", parent=st["title"], fontSize=11, spaceBefore=4, spaceAfter=3)
    buf = io.BytesIO()
    page = A4
    d = SimpleDocTemplate(buf, pagesize=page, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=12 * mm,
                          bottomMargin=18 * mm, title=f"Loan agreement {loan.reference}", author=doc.BRAND)
    width = page[0] - 36 * mm
    m = lambda v: fmt(v, group.currency, group.currency_decimals)  # noqa: E731
    b = loan.borrower
    fees = (loan.fee_processing or 0) + (loan.fee_insurance or 0)
    local = to_local(generated_at, group.timezone)
    story = [doc._header(group, width, st, f"Loan {loan.reference}"), Spacer(1, 4 * mm),
             Paragraph("LOAN AGREEMENT", st["title"]),
             Paragraph(f"Agreement no. {loan.reference} &middot; Prepared {local:%d %b %Y} by {generated_by}", st["small"]),
             Spacer(1, 4 * mm)]

    def kv(rows, col=0.38):
        t = Table([[Paragraph(f"<b>{k}</b>", st["cell"]), Paragraph(v, st["cell"])] for k, v in rows],
                  colWidths=[width * col, width * (1 - col)])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                               ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        return t

    story += [Paragraph("1. The parties", st["h2"]),
              kv([("Lender", group.name),
                  ("Borrower", f"{b.name}" + (f" (ID {b.id_number})" if b.id_number else "")),
                  ("Borrower phone", b.phone or "-"), ("Borrower address", b.address or b.location or "-")]),
              Spacer(1, 3 * mm), Paragraph("2. The loan", st["h2"])]
    terms = [("Amount lent (principal)", m(loan.principal)),
             ("Interest", f"{loan.rate_bp / 100:g}% {PERIOD[loan.rate_period]} "
                          f"({'flat' if loan.method == 'FLAT' else 'reducing balance'})"),
             ("Term", f"{loan.duration} {UNIT[loan.duration_unit]}, repaid {FREQ[loan.frequency]}"),
             ("Disbursement date", f"{loan.disbursement_date:%d %b %Y}"),
             ("First repayment", f"{loan.first_repayment_date:%d %b %Y}"),
             ("Total interest", m(loan.total_interest)), ("Total to repay", m(loan.total_repayable))]
    if fees:
        terms += [("Fees deducted at disbursement",
                   f"{m(fees)} (processing {m(loan.fee_processing)}, insurance {m(loan.fee_insurance)})"),
                  ("Amount received by the borrower", m(loan.principal - fees))]
    if loan.penalty_type != "NONE":
        pen = f"{loan.penalty_value / 100:g}% of the late instalment" if loan.penalty_type == "PERCENT" \
            else f"{m(loan.penalty_value)} per late instalment"
        terms.append(("Late payment penalty", f"{pen}, charged once per instalment after {loan.penalty_grace_days} "
                                              "day(s) grace"))
    if loan.purpose:
        terms.append(("Purpose", loan.purpose))
    story += [kv(terms), Spacer(1, 3 * mm), Paragraph("3. Repayment schedule", st["h2"])]

    rows = [["#", "Due date", "Principal", "Interest", "Instalment"]]
    for s in loan.schedule:
        rows.append([str(s.number), f"{s.due_date:%d %b %Y}", m(s.principal_due), m(s.interest_due), m(s.total_due)])
    t = Table(rows, colWidths=[width * 0.08, width * 0.2, width * 0.24, width * 0.24, width * 0.24], repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#101C36")),
                           ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTSIZE", (0, 0), (-1, -1), 8),
                           ("ALIGN", (2, 0), (-1, -1), "RIGHT"), ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                           ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F5FA")])]))
    story.append(t)

    guarantors = Guarantor.query.filter_by(loan_id=loan.id).all()
    collaterals = Collateral.query.filter_by(loan_id=loan.id).all()
    if guarantors:
        story += [Spacer(1, 3 * mm), Paragraph("4. Guarantors", st["h2"]),
                  kv([(g.name, f"{g.relationship or 'Guarantor'}; phone {g.phone or '-'}; ID {g.id_number or '-'}")
                      for g in guarantors])]
    if collaterals:
        story += [Spacer(1, 3 * mm), Paragraph("5. Security (collateral)", st["h2"]),
                  kv([(c.kind, f"{c.description or ''} Estimated value {m(c.estimated_value)}") for c in collaterals])]

    clauses = [
        "The Borrower will repay the loan in the instalments shown above, on or before each due date.",
        "Each payment is applied first to interest and then to principal, starting with the oldest unpaid instalment.",
        "If an instalment is paid late, the penalty stated in section 2 (if any) applies. Penalties are not charged on penalties.",
        "The Borrower may settle the loan early. On early settlement, interest on instalments that are not yet due may be waived by the Lender.",
        "If the Borrower fails to pay, the Lender may demand the full balance, call on any guarantor, and use the security listed above, as the law allows.",
        "Any security is returned when the loan is fully repaid.",
        "This agreement is governed by the laws of the country where the Lender is licensed.",
    ]
    story += [Spacer(1, 3 * mm), Paragraph("Terms", st["h2"])] + \
             [Paragraph(f"{i}. {c}", st["cell"]) for i, c in enumerate(clauses, 1)]

    def sig(label):
        return [Paragraph("<br/><br/>______________________________", st["cell"]),
                Paragraph(f"{label}<br/>Date: ____________", st["small"])]

    sig_rows = [[sig(f"Borrower: {b.name}"), sig("For the Lender")]]
    for g in guarantors:
        sig_rows.append([sig(f"Guarantor: {g.name}"), sig("Witness")])
    sigs = Table(sig_rows, colWidths=[width / 2, width / 2])
    sigs.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [Spacer(1, 5 * mm), KeepTogether([Paragraph("Signatures", st["h2"]), sigs])]
    foot = doc._footer_factory(group, "")
    d.build(story, onFirstPage=foot, onLaterPages=foot)
    return buf.getvalue()
