"""Borrower credit history and the blacklist (both are private to one business)."""
from app.extensions import db
from app.models import Borrower, Loan, LoanRepayment, RepaymentAllocation
from app.models.core import utcnow
from app.services import loans as loan_svc


class CreditError(ValueError):
    pass


def _last_paid_dates(loan_ids):
    """schedule_id -> date of the latest posted repayment that touched it."""
    if not loan_ids:
        return {}
    rows = (db.session.query(RepaymentAllocation.schedule_id, db.func.max(LoanRepayment.payment_date))
            .join(LoanRepayment, LoanRepayment.id == RepaymentAllocation.repayment_id)
            .filter(LoanRepayment.loan_id.in_(loan_ids), LoanRepayment.status == "POSTED")
            .group_by(RepaymentAllocation.schedule_id).all())
    return {sid: d for sid, d in rows}


def history(borrower: Borrower, today) -> dict:
    loans = Loan.query.filter(Loan.borrower_id == borrower.id, Loan.disbursement_tx_id.isnot(None)).all()
    last_paid = _last_paid_dates([ln.id for ln in loans])
    due = late = max_days = 0
    for ln in loans:
        for s in ln.schedule:
            if s.due_date > today:
                continue
            due += 1
            if s.balance > 0:  # past due and still unpaid
                late += 1
                max_days = max(max_days, (today - s.due_date).days)
            elif last_paid.get(s.id) and last_paid[s.id] > s.due_date:  # paid, but after the due date
                late += 1
                max_days = max(max_days, (last_paid[s.id] - s.due_date).days)
    written_off = sum(1 for ln in loans if ln.status == "WRITTEN_OFF")
    repaid = sum(1 for ln in loans if ln.status == "FULLY_REPAID")
    active = sum(1 for ln in loans if ln.status in loan_svc.ACTIVE_STATES)
    ratio = (late / due) if due else 0
    if borrower.blacklisted:
        grade = "Blacklisted"
    elif not loans:
        grade = "New"
    elif written_off or max_days > 60 or ratio > 0.3:
        grade = "Poor"
    elif (late and ratio > 0.1) or max_days > 14:
        grade = "Watch"
    else:
        grade = "Good"
    return {"loans": len(loans), "active": active, "repaid": repaid, "written_off": written_off,
            "instalments": due, "late": late, "worst_days": max_days, "grade": grade}


def blacklist(borrower: Borrower, reason: str):
    if not (reason or "").strip():
        raise CreditError("A reason is required to blacklist a borrower.")
    borrower.blacklisted, borrower.blacklist_reason, borrower.blacklisted_at = True, reason.strip()[:300], utcnow()


def unblacklist(borrower: Borrower):
    borrower.blacklisted, borrower.blacklist_reason, borrower.blacklisted_at = False, None, None


def match_blacklisted(group_id, id_number=None, phone=None, exclude_id=None):
    """A blacklisted borrower of the same business with the same ID number or phone."""
    q = Borrower.query.filter(Borrower.group_id == group_id, Borrower.blacklisted.is_(True))
    if exclude_id:
        q = q.filter(Borrower.id != exclude_id)
    conds = []
    if (id_number or "").strip():
        conds.append(Borrower.id_number == id_number.strip())
    if (phone or "").strip():
        conds.append(Borrower.phone == phone.strip())
    return q.filter(db.or_(*conds)).first() if conds else None
