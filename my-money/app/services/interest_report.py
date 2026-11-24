"""Interest tracking for one group: what has been received, what is still expected, what is coming in and when.

Everything comes from the repayment schedule (interest_due / interest_paid per instalment), which the repayment
engine keeps in step with the ledger, so these figures reconcile with Reports -> Interest income.

Rule: the FULL-TERM interest of a loan is only treated as earned once the borrower has finished the loan
(status FULLY_REPAID). Until then the system shows what has actually been received and what is still to come,
never a running "total interest" for the loan.
"""
from datetime import timedelta

from sqlalchemy import func

from app.extensions import db
from app.models import Borrower, Loan, LoanRepaymentSchedule as S, Transaction
from app.services.loans import ACTIVE_STATES

VIEWS = {
    "upcoming": "Coming in",
    "overdue": "Overdue",
    "received": "Received",
    "all": "All dates",
    "loans": "By loan",
}
SOON_DAYS = 30


def interest_completed(loan) -> bool:
    """True once the full-term interest of this loan may be shown (the loan is finished)."""
    return loan.status == "FULLY_REPAID"


def completed_interest(loan) -> int:
    """Interest actually earned on a finished loan (agreed interest minus any early-settlement rebate)."""
    return (loan.total_interest or 0) - (loan.interest_rebate or 0) if interest_completed(loan) else 0


def _base(group_id):
    return db.session.query(S).join(Loan, Loan.id == S.loan_id).filter(
        S.group_id == group_id, Loan.group_id == group_id, Loan.disbursement_tx_id.isnot(None))


def overview(group, today):
    """The four headline figures, all in minor units."""
    unpaid = S.interest_due - S.interest_paid
    received = db.session.query(func.coalesce(func.sum(S.interest_paid), 0)).join(Loan, Loan.id == S.loan_id).filter(
        S.group_id == group.id, Loan.disbursement_tx_id.isnot(None)).scalar()
    q = db.session.query(
        func.coalesce(func.sum(unpaid), 0),
        func.coalesce(func.sum(db.case((S.due_date < today, unpaid), else_=0)), 0),
        func.coalesce(func.sum(db.case((S.due_date.between(today, today + timedelta(days=SOON_DAYS)), unpaid), else_=0)), 0),
    ).join(Loan, Loan.id == S.loan_id).filter(
        S.group_id == group.id, Loan.disbursement_tx_id.isnot(None), Loan.status.in_(ACTIVE_STATES), unpaid > 0)
    expected, overdue, soon = (int(v or 0) for v in q.one())
    nxt = db.session.query(func.min(S.due_date)).join(Loan, Loan.id == S.loan_id).filter(
        S.group_id == group.id, Loan.disbursement_tx_id.isnot(None), Loan.status.in_(ACTIVE_STATES),
        unpaid > 0, S.due_date >= today).scalar()
    return {
        "received": int(received or 0),            # interest actually collected so far
        "expected": expected,                      # still to be collected on running loans (overdue + not yet due)
        "overdue": overdue,                        # part of expected whose date has passed
        "incoming": expected - overdue,            # part of expected that is not due yet
        "soon": soon,                              # falling due in the next SOON_DAYS days
        "soon_days": SOON_DAYS,
        "next_date": nxt,
    }


def date_rows_query(group_id, view, today):
    """Instalments carrying interest, with their loan and borrower, for the 'dates' views."""
    unpaid = S.interest_due - S.interest_paid
    q = (db.session.query(S, Loan, Borrower).join(Loan, Loan.id == S.loan_id)
         .join(Borrower, Borrower.id == Loan.borrower_id)
         .filter(S.group_id == group_id, Loan.group_id == group_id, Loan.disbursement_tx_id.isnot(None)))
    if view == "received":
        return q.filter(S.interest_paid > 0).order_by(S.due_date.desc(), S.id.desc())
    if view == "overdue":
        return q.filter(Loan.status.in_(ACTIVE_STATES), unpaid > 0, S.due_date < today).order_by(S.due_date, S.id)
    if view == "all":
        return q.filter(S.interest_due > 0).order_by(S.due_date.desc(), S.id.desc())
    return q.filter(Loan.status.in_(ACTIVE_STATES), unpaid > 0, S.due_date >= today).order_by(S.due_date, S.id)  # upcoming


def row_state(s, loan, today):
    """RECEIVED / PART / OVERDUE / UPCOMING / WAIVED for one instalment's interest."""
    unpaid = s.interest_due - s.interest_paid
    if s.interest_due == 0:
        return "WAIVED" if (loan.interest_rebate or 0) else "NONE"
    if unpaid <= 0:
        return "RECEIVED"
    if loan.status not in ACTIVE_STATES:
        return "NOT_COLLECTED"          # written off
    if s.due_date < today:
        return "OVERDUE"
    return "PART" if s.interest_paid > 0 else "UPCOMING"


def loan_rows(loans, today):
    """Per-loan summary. The full-term total is shown only for finished loans."""
    out = []
    for l in loans:
        recv = sum(s.interest_paid for s in l.schedule)
        open_ = l.status in ACTIVE_STATES
        remaining = sum(s.interest_due - s.interest_paid for s in l.schedule) if open_ else 0
        nxt = min((s.due_date for s in l.schedule if s.interest_due > s.interest_paid), default=None) if open_ else None
        overdue = sum(s.interest_due - s.interest_paid for s in l.schedule
                      if open_ and s.due_date < today and s.interest_due > s.interest_paid)
        out.append({"loan": l, "received": recv, "remaining": remaining, "overdue": overdue, "next_date": nxt,
                    "total": completed_interest(l) if interest_completed(l) else None, "open": open_})
    return out


def monthly_received(group_id, today, months=6):
    """[(YYYY-MM, interest received)] for the last `months` months, oldest first, straight from the ledger
    (interest payments minus any reversals), so it agrees with the group's cash."""
    keys, y, m = [], today.year, today.month
    for _ in range(months):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    keys.reverse()
    month = func.strftime("%Y-%m", Transaction.tx_date)
    signed = db.case((Transaction.direction == "IN", Transaction.amount), else_=-Transaction.amount)
    got = dict(db.session.query(month, func.coalesce(func.sum(signed), 0)).filter(
        Transaction.group_id == group_id, Transaction.tx_type == "INTEREST_REPAYMENT", month >= keys[0]).group_by(month).all())
    return [(k, int(got.get(k, 0))) for k in keys]
