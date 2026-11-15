"""Report builders. Each returns a neutral ``Report`` (columns, rows, summary) that
the HTML view and the PDF / Excel / CSV exporters all render, so every format
shows exactly the same numbers. All figures come from the ledger and loan tables.
Money values are integer minor units; the exporters format them."""
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import func

from app.extensions import db
from app.models import (Contribution, GroupMember, Loan, LoanRepayment, Transaction, User)
from app.services import analytics, ledger, loans as loan_svc
from app.services.savings import CATEGORY_LABELS

REPORT_TITLES = {
    "savings": "Group Savings Report",
    "member_contributions": "Member Contributions Report",
    "member_statement": "Individual Member Statement",
    "loan_disbursements": "Loan Disbursement Report",
    "loan_repayments": "Loan Repayment Report",
    "outstanding_loans": "Outstanding Loans Report",
    "overdue_loans": "Overdue Loans Report",
    "interest_income": "Interest Income Report",
    "monthly": "Monthly Financial Report",
    "annual": "Annual Financial Report",
    "statement": "Complete Group Financial Statement",
}
REPORT_HELP = {
    "savings": "Every contribution and savings deposit in the period.",
    "member_contributions": "Totals and ranking for every active member.",
    "member_statement": "Contribution history and position for one member.",
    "loan_disbursements": "Loans disbursed to borrowers in the period.",
    "loan_repayments": "Repayments received, split into principal and interest.",
    "outstanding_loans": "Principal and interest still owed on active loans.",
    "overdue_loans": "Loans with instalments past their due date.",
    "interest_income": "Interest collected, and interest still receivable.",
    "monthly": "Cash flow and position for a single month.",
    "annual": "Month-by-month cash flow for a year.",
    "statement": "Full ledger with position summary and reconciliation.",
}
TX_LABELS = {
    "CONTRIBUTION": "Contribution", "SAVINGS_ADDITIONAL": "Voluntary savings",
    "LOAN_DISBURSEMENT": "Loan disbursement", "PRINCIPAL_REPAYMENT": "Principal repayment",
    "INTEREST_REPAYMENT": "Interest repayment", "PENALTY": "Penalty", "EXPENSE": "Expense",
    "ADJUSTMENT": "Adjustment",
}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


@dataclass
class Report:
    key: str
    title: str
    period: str
    columns: list          # [(header, kind)]  kind: text | money | date | int | pct
    rows: list
    summary: list = field(default_factory=list)  # [(label, value, kind)]
    notes: list = field(default_factory=list)


def _range_label(start, end):
    if start and end:
        return f"{start:%d %b %Y} to {end:%d %b %Y}"
    if start:
        return f"From {start:%d %b %Y}"
    if end:
        return f"Up to {end:%d %b %Y}"
    return "All time"


def _clip(q, col, start, end):
    if start:
        q = q.filter(col >= start)
    if end:
        q = q.filter(col <= end)
    return q


def build(key, group, today, start=None, end=None, member_id=None, month=None, year=None) -> Report:
    fn = {
        "savings": _savings, "member_contributions": _member_contributions, "member_statement": _member_statement,
        "loan_disbursements": _disbursements, "loan_repayments": _repayments, "outstanding_loans": _outstanding,
        "overdue_loans": _overdue, "interest_income": _interest, "monthly": _monthly, "annual": _annual,
        "statement": _statement,
    }.get(key)
    if fn is None:
        raise KeyError(key)
    return fn(group, today, start, end, member_id=member_id, month=month, year=year)


def _savings(group, today, start, end, **_):
    q = Contribution.query.join(GroupMember, GroupMember.id == Contribution.member_id) \
        .join(User, User.id == GroupMember.user_id).filter(Contribution.group_id == group.id)
    q = _clip(q, Contribution.contribution_date, start, end)
    rows, reversed_ids = [], {r[0] for r in db.session.query(Transaction.reverses_id).filter(
        Transaction.group_id == group.id, Transaction.reverses_id.isnot(None))}
    for c in q.order_by(Contribution.contribution_date, Contribution.id):
        rows.append([c.contribution_date, c.receipt_no, c.member.user.full_name, CATEGORY_LABELS[c.category],
                     c.payment_method or "", c.payment_reference or "", c.amount,
                     "Reversed" if c.tx_id in reversed_ids else ""])
    t = ledger.totals(group.id, start, end)
    return Report("savings", REPORT_TITLES["savings"], _range_label(start, end),
                  [("Date", "date"), ("Receipt", "text"), ("Member", "text"), ("Category", "text"),
                   ("Method", "text"), ("Reference", "text"), ("Amount", "money"), ("Note", "text")], rows,
                  [("Savings in period (net of reversals)", t["total_savings"], "money"),
                   ("Contributions recorded", len(rows), "int"),
                   ("Total group savings (all time)", ledger.totals(group.id)["total_savings"], "money")])


def _member_contributions(group, today, start, end, **_):
    board = analytics.leaderboard(group, today)
    rows = [[r["rank"], r["name"], r["total"], r["month"], r["year"]] for r in board]
    return Report("member_contributions", REPORT_TITLES["member_contributions"], f"As at {today:%d %b %Y}",
                  [("Rank", "int"), ("Member", "text"), ("Total contributed", "money"),
                   ("This month", "money"), ("This year", "money")], rows,
                  [("Total group savings", sum(r["total"] for r in board), "money"),
                   ("Active members", len(board), "int")])


def _member_statement(group, today, start, end, member_id=None, **_):
    m = GroupMember.query.filter_by(id=member_id, group_id=group.id).first()
    if m is None:
        raise KeyError("member")
    from app.services.savings import expected_for_member
    q = Contribution.query.filter_by(group_id=group.id, member_id=m.id)
    q = _clip(q, Contribution.contribution_date, start, end)
    reversed_ids = {r[0] for r in db.session.query(Transaction.reverses_id).filter(
        Transaction.group_id == group.id, Transaction.reverses_id.isnot(None))}
    rows = [[c.contribution_date, c.receipt_no, CATEGORY_LABELS[c.category], c.payment_method or "",
             c.payment_reference or "", c.amount, "Reversed" if c.tx_id in reversed_ids else ""]
            for c in q.order_by(Contribution.contribution_date, Contribution.id)]
    total = ledger.member_savings(group.id, m.id)
    expected = expected_for_member(group, m, today)
    summary = [("Member", m.user.full_name, "text"), ("Total savings (all time)", total, "money")]
    if expected:
        summary += [("Expected contributions to date", expected, "money"),
                    ("Shortfall", max(0, expected - total), "money")]
    return Report("member_statement", f"Member Statement - {m.user.full_name}", _range_label(start, end),
                  [("Date", "date"), ("Receipt", "text"), ("Category", "text"), ("Method", "text"),
                   ("Reference", "text"), ("Amount", "money"), ("Note", "text")], rows, summary)


def _disbursements(group, today, start, end, **_):
    q = Loan.query.filter(Loan.group_id == group.id, Loan.disbursed_at.isnot(None)).join(
        Transaction, Transaction.id == Loan.disbursement_tx_id)
    q = _clip(q, Transaction.tx_date, start, end)
    rows = []
    for l in q.order_by(Transaction.tx_date, Loan.id):
        tx = db.session.get(Transaction, l.disbursement_tx_id)
        rows.append([tx.tx_date, l.reference, l.borrower.name, l.principal, l.rate_bp / 100,
                     "Flat" if l.method == "FLAT" else "Reducing", f"{l.duration} {l.duration_unit.lower()}(s)",
                     l.total_interest, l.total_repayable])
    return Report("loan_disbursements", REPORT_TITLES["loan_disbursements"], _range_label(start, end),
                  [("Date", "date"), ("Loan", "text"), ("Borrower", "text"), ("Principal", "money"),
                   ("Rate %", "pct"), ("Method", "text"), ("Term", "text"), ("Interest", "money"),
                   ("Total repayable", "money")], rows,
                  [("Loans disbursed", len(rows), "int"), ("Principal disbursed", sum(r[3] for r in rows), "money"),
                   ("Interest agreed", sum(r[7] for r in rows), "money")])


def _repayments(group, today, start, end, **_):
    q = LoanRepayment.query.filter_by(group_id=group.id)
    q = _clip(q, LoanRepayment.payment_date, start, end)
    rows = [[r.payment_date, r.receipt_no, r.loan.reference, r.borrower.name, r.amount, r.principal_part,
             r.interest_part, r.payment_method or "", "Reversed" if r.status == "REVERSED" else ""]
            for r in q.order_by(LoanRepayment.payment_date, LoanRepayment.id)]
    posted = [r for r in rows if not r[8]]
    return Report("loan_repayments", REPORT_TITLES["loan_repayments"], _range_label(start, end),
                  [("Date", "date"), ("Receipt", "text"), ("Loan", "text"), ("Borrower", "text"),
                   ("Amount", "money"), ("Principal", "money"), ("Interest", "money"), ("Method", "text"),
                   ("Note", "text")], rows,
                  [("Repayments received (excl. reversed)", sum(r[4] for r in posted), "money"),
                   ("Principal portion", sum(r[5] for r in posted), "money"),
                   ("Interest portion", sum(r[6] for r in posted), "money")])


def _active_loans(group):
    return Loan.query.filter(Loan.group_id == group.id, Loan.status.in_(loan_svc.ACTIVE_STATES),
                             Loan.disbursement_tx_id.isnot(None)).order_by(Loan.id).all()


def _outstanding(group, today, start, end, **_):
    rows = []
    for l in _active_loans(group):
        p, i = loan_svc.outstanding(l)
        nd = loan_svc.next_due(l)
        rows.append([l.reference, l.borrower.name, l.principal, p, i, nd.due_date if nd else None,
                     loan_svc.STATUS_LABELS[loan_svc.effective_status(l, today)]])
    return Report("outstanding_loans", REPORT_TITLES["outstanding_loans"], f"As at {today:%d %b %Y}",
                  [("Loan", "text"), ("Borrower", "text"), ("Disbursed", "money"), ("Principal outstanding", "money"),
                   ("Interest outstanding", "money"), ("Next due", "date"), ("Status", "text")], rows,
                  [("Principal outstanding", sum(r[3] for r in rows), "money"),
                   ("Interest receivable", sum(r[4] for r in rows), "money"),
                   ("Active loans", len(rows), "int")],
                  ["Money owed by borrowers is NOT part of available cash."])


def _overdue(group, today, start, end, **_):
    rows = []
    for l in _active_loans(group):
        late = [s for s in l.schedule if s.balance > 0 and s.due_date < today]
        if not late:
            continue
        p, _i = loan_svc.outstanding(l)
        oldest = min(s.due_date for s in late)
        rows.append([l.reference, l.borrower.name, len(late), sum(s.balance for s in late), oldest,
                     (today - oldest).days, p])
    rows.sort(key=lambda r: -r[5])
    return Report("overdue_loans", REPORT_TITLES["overdue_loans"], f"As at {today:%d %b %Y}",
                  [("Loan", "text"), ("Borrower", "text"), ("Instalments late", "int"), ("Amount overdue", "money"),
                   ("Oldest due date", "date"), ("Days overdue", "int"), ("Principal outstanding", "money")], rows,
                  [("Overdue loans", len(rows), "int"), ("Total overdue", sum(r[3] for r in rows), "money")])


def _interest(group, today, start, end, **_):
    q = LoanRepayment.query.filter(LoanRepayment.group_id == group.id, LoanRepayment.status == "POSTED",
                                   LoanRepayment.interest_part > 0)
    q = _clip(q, LoanRepayment.payment_date, start, end)
    rows = [[r.payment_date, r.receipt_no, r.loan.reference, r.interest_part]
            for r in q.order_by(LoanRepayment.payment_date, LoanRepayment.id)]
    pf = loan_svc.portfolio(group, today)
    t = ledger.totals(group.id, start, end)
    return Report("interest_income", REPORT_TITLES["interest_income"], _range_label(start, end),
                  [("Date", "date"), ("Receipt", "text"), ("Loan", "text"), ("Interest received", "money")], rows,
                  [("Interest collected in period (ledger)", t["interest_collected"], "money"),
                   ("Interest earned on all loans (agreed)", pf["interest_earned"], "money"),
                   ("Interest collected (all time)", pf["interest_collected"], "money"),
                   ("Interest receivable (still owed)", pf["interest_receivable"], "money")])


def _month_bounds(month):
    try:
        y, m = (int(x) for x in month.split("-"))
        first = date(y, m, 1)
    except (ValueError, AttributeError):
        raise KeyError("month")
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


def _tx_rows(group, start, end):
    q = Transaction.query.filter_by(group_id=group.id)
    q = _clip(q, Transaction.tx_date, start, end)
    rows = []
    for t in q.order_by(Transaction.tx_date, Transaction.id):
        rows.append([t.tx_date, t.reference, TX_LABELS.get(t.tx_type, t.tx_type), t.description or "",
                     t.amount if t.direction == "IN" else 0, t.amount if t.direction == "OUT" else 0])
    return rows


def _monthly(group, today, start, end, month=None, **_):
    first, last = _month_bounds(month or f"{today:%Y-%m}")
    t = ledger.totals(group.id, first, last)
    opening = ledger.totals(group.id, None, first - timedelta(days=1))["cash"]
    return Report("monthly", f"Monthly Financial Report - {MONTHS[first.month - 1]} {first.year}",
                  f"{first:%d %b %Y} to {last:%d %b %Y}",
                  [("Date", "date"), ("Reference", "text"), ("Type", "text"), ("Description", "text"),
                   ("Money in", "money"), ("Money out", "money")], _tx_rows(group, first, last),
                  [("Opening cash", opening, "money"), ("Contributions & savings", t["total_savings"], "money"),
                   ("Loans disbursed", t["disbursed"], "money"), ("Principal repaid", t["principal_repaid"], "money"),
                   ("Interest collected", t["interest_collected"], "money"), ("Penalties", t["penalties"], "money"),
                   ("Expenses", t["expenses"], "money"), ("Closing cash", ledger.totals(group.id, None, last)["cash"],
                                                          "money")])


def _annual(group, today, start, end, year=None, **_):
    y = int(year or today.year)
    rows = []
    for m in range(1, 13):
        first = date(y, m, 1)
        last = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
        t = ledger.totals(group.id, first, last)
        net = t["cash"]
        rows.append([MONTHS[m - 1], t["total_savings"], t["disbursed"], t["principal_repaid"],
                     t["interest_collected"], t["expenses"], net])
    tot = ledger.totals(group.id, date(y, 1, 1), date(y, 12, 31))
    return Report("annual", f"Annual Financial Report - {y}", f"1 Jan {y} to 31 Dec {y}",
                  [("Month", "text"), ("Savings", "money"), ("Disbursed", "money"), ("Principal repaid", "money"),
                   ("Interest", "money"), ("Expenses", "money"), ("Net cash movement", "money")], rows,
                  [("Total savings in year", tot["total_savings"], "money"),
                   ("Loans disbursed", tot["disbursed"], "money"),
                   ("Interest collected", tot["interest_collected"], "money"),
                   ("Net cash movement", tot["cash"], "money"),
                   ("Closing cash", ledger.totals(group.id, None, date(y, 12, 31))["cash"], "money")])


def _statement(group, today, start, end, **_):
    s = analytics.group_summary(group, today)
    t = ledger.totals(group.id, None, end) if end else ledger.totals(group.id)
    rows = _tx_rows(group, start, end)
    reconciled = sum(r[4] for r in rows) - sum(r[5] for r in rows) if not start and not end else None
    summary = [
        ("Total savings", s["total_savings"], "money"), ("Available cash", s["available_cash"], "money"),
        ("Principal lent (disbursed)", s["principal_lent"], "money"),
        ("Principal outstanding", s["principal_outstanding"], "money"),
        ("Interest collected", s["interest_collected"], "money"),
        ("Interest receivable", s["interest_receivable"], "money"),
        ("Total repayments received", s["total_repayments"], "money"), ("Expenses", s["expenses"], "money"),
    ]
    notes = ["Available cash = all money in - all money out, from the transaction ledger.",
             "Principal outstanding and interest receivable are owed by borrowers and are not cash."]
    if reconciled is not None:
        notes.append(f"Ledger reconciliation: money in minus money out equals cash "
                     f"({'matches' if reconciled == t['cash'] else 'MISMATCH'}).")
    return Report("statement", REPORT_TITLES["statement"], _range_label(start, end) if (start or end) else
                  f"As at {today:%d %b %Y}",
                  [("Date", "date"), ("Reference", "text"), ("Type", "text"), ("Description", "text"),
                   ("Money in", "money"), ("Money out", "money")], rows, summary, notes)
