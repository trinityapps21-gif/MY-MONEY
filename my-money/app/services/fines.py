"""Late fines on EXTERNAL loans (borrowers, never group members).

Rule (set by the group's officials in Group rules):
  * the deadline is the borrower's promised date (if an open promise exists and
    is not earlier than the instalment's due date) or the instalment due date;
  * nothing is charged until ``fine_grace_days`` after the deadline;
  * after that the fine is charged for every started ``fine_period_days`` past
    the grace period, either a % of the late amount or a fixed amount per period,
    optionally capped at the late amount.
Paid fines are booked to the ledger as income (PENALTY, kind FINE)."""
import math
from decimal import ROUND_HALF_UP, Decimal

from app.extensions import db
from app.models import Loan, LoanFine, LoanPromise, LoanRepaymentSchedule, Transaction, utcnow
from app.services import ledger, notify
from app.services.groupcfg import get_settings


class FineError(ValueError):
    pass


def compute(settings, late_amount, days_late) -> int:
    """Fine for ``late_amount`` that is ``days_late`` days past its deadline."""
    if days_late <= settings.fine_grace_days or late_amount <= 0:
        return 0
    periods = math.ceil((days_late - settings.fine_grace_days) / max(1, settings.fine_period_days))
    if settings.fine_type == "FIXED":
        fine = settings.fine_fixed * periods
    else:
        fine = int((Decimal(late_amount) * settings.fine_rate_bp * periods / Decimal(10000))
                   .quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if settings.fine_capped:
        fine = min(fine, late_amount)
    return max(0, fine)


def deadline_for(loan, sched):
    """The date the borrower is expected to pay this instalment by."""
    promises = [p for p in loan.promises if p.status == "OPEN" and p.promise_date >= sched.due_date]
    if promises:
        return max(p.promise_date for p in promises)
    return sched.due_date


def assess_on_repayment(group, loan, user, rep, alloc, paid_on):
    """Called inside record_repayment with [(schedule_row, principal, interest)].
    Creates one UNPAID fine for the repayment when it was made late."""
    from app.services import importctx
    if importctx.active():  # historical bulk imports never create fines
        return None
    s = get_settings(group.id)
    total, late_total, worst_days, worst_deadline = 0, 0, 0, None
    for sched, p_pay, i_pay in alloc:
        late_amount = p_pay + i_pay
        dl = deadline_for(loan, sched)
        days = (paid_on - dl).days
        fine = compute(s, late_amount, days)
        if fine > 0:
            total += fine
            late_total += late_amount
            if days > worst_days:
                worst_days, worst_deadline = days, dl
    for p in loan.promises:
        if p.status == "OPEN" and rep.amount >= p.amount:
            p.status = "KEPT"
    if total <= 0:
        return None
    f = LoanFine(group_id=group.id, loan_id=loan.id, borrower_id=loan.borrower_id, repayment_id=rep.id,
                 deadline=worst_deadline, paid_on=paid_on, days_late=worst_days, late_amount=late_total,
                 amount=total, status="UNPAID",
                 reason=f"Paid {worst_days} day(s) after the deadline of {worst_deadline:%d %b %Y}")
    db.session.add(f)
    db.session.flush()
    notify.notify(notify.official_ids(group.id), group.id, "FINE", "Late payment fine added",
                  f"{loan.reference}: paid {worst_days} day(s) late. Fine {total:,} added.",
                  f"/g/{group.id}/fines/")
    return f


def settle(group, fine: LoanFine, user, today, method=None):
    """The borrower paid the fine: book it as group income."""
    if fine.group_id != group.id or fine.status != "UNPAID":
        raise FineError("This fine is not waiting for payment.")
    if fine.amount <= 0:
        raise FineError("Nothing to collect.")
    tx = ledger.post(group.id, "PENALTY", fine.amount, "IN", today, user.id,
                     f"Fine interest - {fine.loan.reference} ({fine.borrower.name})", method, None,
                     borrower_id=fine.borrower_id, loan_id=fine.loan_id, prefix="FIN", kind="FINE")
    fine.status, fine.tx_id, fine.settled_on, fine.decided_by = "PAID", tx.id, today, user.id


def waive(group, fine: LoanFine, user, reason, new_amount=None):
    """Waive the fine, or reduce it to ``new_amount`` (> 0). A reason is required."""
    if fine.group_id != group.id or fine.status != "UNPAID":
        raise FineError("Only unpaid fines can be waived or reduced.")
    if not (reason or "").strip():
        raise FineError("Give a reason.")
    if new_amount and 0 < new_amount < fine.amount:
        fine.reason = f"Reduced from {fine.amount:,} to {new_amount:,}: {reason.strip()}"[:300]
        fine.amount = new_amount
    else:
        fine.status = "WAIVED"
        fine.reason = f"Waived: {reason.strip()}"[:300]
    fine.decided_by = user.id


def void_for_repayment(rep, user, reason, today):
    """The repayment was reversed, so the fine that came from it is cancelled."""
    for f in LoanFine.query.filter_by(repayment_id=rep.id).all():
        if f.status == "PAID" and f.tx_id:
            ledger.reverse(db.session.get(Transaction, f.tx_id), user.id, reason, today)
        if f.status in ("UNPAID", "PAID"):
            f.status = "VOID"


# ------------------------------------------------------------------- overdue
def overdue_now(group, today):
    """Instalments past deadline + grace right now, with the fine accrued so far."""
    from app.services.loans import ACTIVE_STATES
    s = get_settings(group.id)
    out = []
    loans = Loan.query.filter(Loan.group_id == group.id, Loan.status.in_(list(ACTIVE_STATES)),
                              Loan.disbursement_tx_id.isnot(None)).all()
    for loan in loans:
        for sched in loan.schedule:
            bal = sched.balance
            if bal <= 0:
                continue
            dl = deadline_for(loan, sched)
            days = (today - dl).days
            if days <= 0:
                continue
            fine = compute(s, bal, days)
            out.append({"loan": loan, "sched": sched, "deadline": dl, "days": days, "balance": bal, "fine": fine,
                        "in_grace": days <= s.fine_grace_days})
    out.sort(key=lambda r: -r["days"])
    return out


def notify_overdue(group, today):
    s = get_settings(group.id)
    for r in overdue_now(group, today):
        if r["in_grace"]:
            continue
        notify.notify(notify.official_ids(group.id), group.id, "FINE", "Loan payment overdue",
                      f"{r['loan'].reference} ({r['loan'].borrower.name}) is {r['days']} days past its deadline.",
                      f"/g/{group.id}/fines/",
                      dedupe_key=f"overdue:{r['loan'].id}:{r['sched'].id}:{r['deadline']}")


def totals(group_id):
    rows = db.session.query(LoanFine.status, db.func.coalesce(db.func.sum(LoanFine.amount), 0),
                            db.func.count(LoanFine.id)).filter(LoanFine.group_id == group_id).group_by(
        LoanFine.status).all()
    d = {st: (int(a), int(c)) for st, a, c in rows}
    return {"unpaid": d.get("UNPAID", (0, 0)), "paid": d.get("PAID", (0, 0)), "waived": d.get("WAIVED", (0, 0))}
