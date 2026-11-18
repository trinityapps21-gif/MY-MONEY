"""Lending-business rules: fees, late-payment penalties and the interest-rate cap.

* Fees are worked out once, when a loan is created, and collected at
  disbursement (ledger type FEE). The borrower pays them out of the loan.
* A late fine is charged once per overdue instalment, after the grace days, and
  never compounds. When a payment is recorded, fines are cleared first (ledger
  type PENALTY) and the rest goes to the instalments. A fine can be waived with
  a reason.
* The rate cap compares the loan's rate, converted to a monthly rate, with the
  lower of the platform cap and the business's own cap.
"""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from app.extensions import db
from app.models import Group, Loan, LoanPenalty
from app.services import importctx, ledger


class LenderError(ValueError):
    pass


def _pct(amount: int, bp: int) -> int:
    return int((Decimal(amount) * Decimal(bp) / Decimal(10000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fee_amount(kind: str, value: int, principal: int) -> int:
    if kind == "FLAT":
        return int(value)
    if kind == "PERCENT":
        return _pct(principal, int(value))
    return 0


def monthly_rate_bp(rate_bp: int, period: str) -> Decimal:
    if period == "WEEK":
        return Decimal(rate_bp) * Decimal(52) / Decimal(12)
    if period == "YEAR":
        return Decimal(rate_bp) / Decimal(12)
    return Decimal(rate_bp)


def effective_cap_bp(group: Group, platform_cap_bp):
    """Lowest cap that applies to this business (% per month x100), or None."""
    if not group.is_lender:
        return None
    caps = [c for c in (platform_cap_bp, group.rate_cap_bp) if c]
    return min(caps) if caps else None


def check_rate_cap(group: Group, rate_bp: int, period: str, platform_cap_bp):
    cap = effective_cap_bp(group, platform_cap_bp)
    if cap is None:
        return
    if monthly_rate_bp(rate_bp, period) > cap:
        raise LenderError(f"The rate is above the maximum allowed for lenders ({cap / 100:g}% per month). "
                          "Lower the rate to continue.")


def penalty_amount(loan: Loan, balance: int) -> int:
    if loan.penalty_type == "FLAT":
        return int(loan.penalty_value)
    if loan.penalty_type == "PERCENT":
        return _pct(balance, int(loan.penalty_value))
    return 0


def accrue_penalties(group: Group, today, loan: Loan = None) -> int:
    """Assess penalties for overdue instalments. Safe to run repeatedly: the
    unique (schedule) key means an instalment is penalised only once. Returns
    how many penalties were created."""
    if not group.is_lender or importctx.active():
        return 0
    q = Loan.query.filter(Loan.group_id == group.id, Loan.penalty_type != "NONE",
                          Loan.status.in_(("ACTIVE", "PARTIALLY_REPAID", "OVERDUE")),
                          Loan.disbursement_tx_id.isnot(None))
    if loan is not None:
        q = q.filter(Loan.id == loan.id)
    created = 0
    for ln in q.all():
        have = {p.schedule_id for p in ln.penalties}
        for s in ln.schedule:
            if s.id in have or s.balance <= 0:
                continue
            if s.due_date + timedelta(days=ln.penalty_grace_days) >= today:
                continue
            amt = penalty_amount(ln, s.balance)
            if amt > 0:
                db.session.add(LoanPenalty(group_id=group.id, loan_id=ln.id, schedule_id=s.id, amount=amt,
                                           assessed_on=today))
                created += 1
    if created:
        db.session.commit()
    return created


def penalties_due(loan: Loan) -> int:
    return sum(p.balance for p in loan.penalties)


def collect_penalty(group, loan: Loan, user, amount: int, paid_on, method=None, reference=None):
    due = penalties_due(loan)
    if amount <= 0:
        raise LenderError("Enter an amount greater than zero.")
    if amount > due:
        raise LenderError("The amount is more than the penalties owed on this loan.")
    left = amount
    for p in loan.penalties:
        take = min(left, p.balance)
        p.paid += take
        left -= take
        if left == 0:
            break
    return ledger.post(group.id, "PENALTY", amount, "IN", paid_on, user.id,
                       description=f"Late-payment penalty {loan.reference}", payment_method=method,
                       payment_reference=reference, borrower_id=loan.borrower_id, loan_id=loan.id, prefix="PEN")


def amount_due(loan: Loan, today) -> dict:
    """What the borrower owes right now: instalments already due + fines. If nothing is due yet,
    `total` is the next instalment so the borrower knows what is coming."""
    from app.services import loans as loan_svc
    due_items = [s for s in loan.schedule if s.balance > 0 and s.due_date <= today]
    inst = sum(s.balance for s in due_items)
    fines = penalties_due(loan)
    nxt = loan_svc.next_due(loan)
    upcoming = 0 if due_items or nxt is None else nxt.balance
    return {"instalments": inst, "fines": fines, "overdue": bool(due_items), "upcoming": upcoming,
            "total": inst + fines + upcoming, "next": nxt}


def fine_reason(loan: Loan, p, group_currency_fmt) -> str:
    """Plain-words explanation of one fine, for the borrower's page."""
    s = next((x for x in loan.schedule if x.id == p.schedule_id), None)
    num = f"instalment {s.number}" if s else "an instalment"
    due = s.due_date.strftime("%d %b %Y") if s else ""
    if loan.penalty_type == "PERCENT":
        rule = f"{int(loan.penalty_value) / 100:g}% of the unpaid amount"
    else:
        rule = group_currency_fmt(int(loan.penalty_value))
    return (f"Late fine: {num} was due on {due} and was still unpaid {loan.penalty_grace_days} day(s) later "
            f"(grace period). The fine is {rule}, added once.")


def record_payment(group, loan: Loan, user, amount: int, paid_on, method=None, reference=None, notes=None,
                   idempotency_key=None):
    """One payment from a borrower: fines are cleared first, the rest goes to the instalments.
    Returns (repayment_or_None, fine_part). Raises LenderError / LoanError / DuplicateError."""
    from app.services import loans as loan_svc
    if amount <= 0:
        raise LenderError("Enter an amount greater than zero.")
    fines = penalties_due(loan) if group.is_lender else 0
    fine_part = min(amount, fines)
    rest = amount - fine_part
    if rest:
        rep = loan_svc.record_repayment(group, loan, user, rest, paid_on, method, reference, notes, idempotency_key)
    else:
        rep = None
        if loan.status not in loan_svc.ACTIVE_STATES:
            raise LenderError("Payments can only be recorded on active loans.")
    if fine_part:
        collect_penalty(group, loan, user, fine_part, paid_on, method, reference)
    return rep, fine_part


def waive_penalties(loan: Loan, reason: str):
    if not (reason or "").strip():
        raise LenderError("A reason is required to waive penalties.")
    n = 0
    for p in loan.penalties:
        if p.balance > 0:
            p.waived += p.balance
            p.waive_reason = reason.strip()[:300]
            n += 1
    if not n:
        raise LenderError("There are no penalties owed on this loan.")
    return n


def parse_charge(kind: str, raw, decimals: int) -> int:
    """Form value -> stored value: minor units for FLAT, basis points for PERCENT, 0 for NONE."""
    from app.services.money import parse_rate_bp, to_minor
    if kind not in ("NONE", "FLAT", "PERCENT"):
        raise ValueError("Select a valid fee or penalty type.")
    if kind == "NONE":
        return 0
    raw = (raw or "").strip() or "0"
    v = to_minor(raw, decimals) if kind == "FLAT" else parse_rate_bp(raw)
    if v < 0:
        raise ValueError("Fees and penalties cannot be negative.")
    return v
