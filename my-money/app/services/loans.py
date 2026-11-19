"""Lending business logic: creation, approval, disbursement, repayment, reversal.

Repayment allocation (documented, deterministic): a payment is applied to the
OLDEST unpaid instalment first; within an instalment INTEREST is settled before
PRINCIPAL. Surplus carries on to the next instalment (early repayment). A payment
larger than the total outstanding is rejected. Allocations are stored per
repayment so a reversal can restore the schedule exactly.

Terms (rate, period, method, duration, frequency) are copied onto each loan at
creation; later changes to the platform interest policy never touch them.
"""
from datetime import timedelta

from sqlalchemy import func

from app.extensions import db
from app.models import utcnow
from app.models import (Borrower, InterestSettings, Loan, LoanApproval, LoanRepayment, LoanRepaymentSchedule,
                        RepaymentAllocation, Transaction)
from app.models import LoanProduct
from app.services import lender
from app.services.timeutil import local_today
from app.services import feedlog, importctx, interest, ledger, notify
from app.services.savings import DuplicateError

ACTIVE_STATES = ("ACTIVE", "PARTIALLY_REPAID", "OVERDUE")
STATUS_LABELS = {
    "DRAFT": "Draft", "PENDING_APPROVAL": "Pending approval", "APPROVED": "Approved", "ACTIVE": "Active",
    "PARTIALLY_REPAID": "Partially repaid", "FULLY_REPAID": "Repaid", "OVERDUE": "Overdue",
    "REJECTED": "Rejected", "WRITTEN_OFF": "Written off",
}


class LoanError(ValueError):
    pass


def get_interest_settings():
    """The current group's own interest policy (set by its officials)."""
    from flask import g, has_request_context
    if has_request_context() and getattr(g, "group", None) is not None:
        from app.services.groupcfg import get_settings
        return get_settings(g.group.id)
    s = db.session.get(InterestSettings, 1)
    if s is None:
        s = InterestSettings(id=1)
        db.session.add(s)
        db.session.flush()
    return s


def platform_lender_cap():
    """Platform-wide maximum monthly rate for lending businesses (basis points); None = no cap."""
    row = db.session.get(InterestSettings, 1)
    return row.lender_cap_bp if row is not None else 280


def validate_terms(method, rate_bp, rate_period, group=None):
    s = get_interest_settings()
    if group is not None and not importctx.active():
        try:
            lender.check_rate_cap(group, rate_bp, rate_period, platform_lender_cap())
        except lender.LenderError as e:
            raise LoanError(str(e))
    if method == "FLAT" and not s.flat_enabled:
        raise LoanError("Flat interest is currently disabled by the group officials.")
    if method == "REDUCING" and not s.reducing_enabled:
        raise LoanError("Reducing balance interest is currently disabled by the group officials.")
    lender_biz = group is not None and group.is_lender  # lenders price through products and the lender cap
    if not lender_biz and not s.allow_custom_rates and (rate_bp != s.default_rate_bp or rate_period != s.default_period):
        raise LoanError("The group requires the default interest rate and period.")
    if lender_biz:
        if rate_bp > s.max_rate_bp and not (platform_lender_cap() or group.rate_cap_bp):
            raise LoanError(f"Interest rate must be at most {s.max_rate_bp / 100:g}%.")
        return
    if not (s.min_rate_bp <= rate_bp <= s.max_rate_bp):
        raise LoanError(f"Interest rate must be between {s.min_rate_bp / 100:g}% and {s.max_rate_bp / 100:g}%.")


def get_product(group, product_id):
    if not product_id:
        return None
    p = LoanProduct.query.filter_by(id=product_id, group_id=group.id, active=True).first()
    if p is None:
        raise LoanError("Select a loan product from this business.")
    return p


def charges(group, data):
    """Fees and penalty terms for a new loan: from the chosen product, else the business defaults."""
    prod = get_product(group, data.get("product_id")) if group.is_lender else None
    principal = data["principal"]
    if prod:
        if principal < prod.min_amount or (prod.max_amount and principal > prod.max_amount):
            hi = f" to {prod.max_amount:,}" if prod.max_amount else " or more"
            raise LoanError(f"This product lends {prod.min_amount:,}{hi}.")
        return dict(product_id=prod.id,
                    fee_processing=lender.fee_amount(prod.processing_fee_type, prod.processing_fee_value, principal),
                    fee_insurance=lender.fee_amount(prod.insurance_fee_type, prod.insurance_fee_value, principal),
                    penalty_type=prod.penalty_type, penalty_value=prod.penalty_value,
                    penalty_grace_days=prod.penalty_grace_days)
    if importctx.active():  # historical loans: never invent penalties for old records
        return dict(product_id=None, fee_processing=0, fee_insurance=0, penalty_type="NONE", penalty_value=0,
                    penalty_grace_days=0)
    return dict(product_id=None, fee_processing=0, fee_insurance=0, penalty_type=group.penalty_type,
                penalty_value=group.penalty_value, penalty_grace_days=group.penalty_grace_days)


def preview(data, group=None) -> interest.ScheduleResult:
    validate_terms(data["method"], data["rate_bp"], data["rate_period"], group)
    if data["first_repayment_date"] < data["disbursement_date"]:
        raise LoanError("First repayment date cannot be before the disbursement date.")
    return interest.build_schedule(
        data["principal"], data["rate_bp"], data["rate_period"], data["method"], data["duration"],
        data["duration_unit"], data["frequency"], data["first_repayment_date"],
    )


def create_loan(group, user, data, save_draft=False) -> Loan:
    borrower = Borrower.query.filter_by(id=data["borrower_id"], group_id=group.id).first()
    if borrower is None:
        raise LoanError("Select a borrower from this " + ("business." if group.is_lender else "group."))
    if borrower.blacklisted and not importctx.active():
        raise LoanError("This borrower is blacklisted" + (f": {borrower.blacklist_reason}" if borrower.blacklist_reason
                                                         else "."))
    result = preview(data, group)
    extra = charges(group, data)
    old = None
    if data.get("refinance_of_id"):
        old = Loan.query.filter_by(id=data["refinance_of_id"], group_id=group.id, borrower_id=borrower.id).first()
        if old is None or old.status not in ACTIVE_STATES:
            raise LoanError("The loan being refinanced must be an open loan of this borrower.")
        if data["principal"] < settlement_quote(old, local_today(group.timezone))["payoff"]:
            raise LoanError("The new principal must at least cover what the borrower still owes on the old loan.")
    n = Loan.query.filter_by(group_id=group.id).count() + 1
    loan = Loan(
        group_id=group.id, borrower_id=borrower.id, reference=f"LN-{n:04d}", principal=data["principal"],
        rate_bp=data["rate_bp"], rate_period=data["rate_period"], method=data["method"],
        duration=data["duration"], duration_unit=data["duration_unit"], frequency=data["frequency"],
        disbursement_date=data["disbursement_date"], first_repayment_date=data["first_repayment_date"],
        purpose=(data.get("purpose") or "")[:300], notes=data.get("notes"),
        status="DRAFT" if save_draft else "PENDING_APPROVAL", total_interest=result.total_interest,
        total_repayable=result.total_repayable, approvals_required=group.loan_approvals_required,
        created_by=user.id, officer_id=data.get("officer_id") or borrower.officer_id,
        refinance_of_id=old.id if old else None, **extra,
    )
    db.session.add(loan)
    db.session.flush()
    for i in result.instalments:
        db.session.add(LoanRepaymentSchedule(group_id=group.id, loan_id=loan.id, number=i.number,
                                             due_date=i.due_date, principal_due=i.principal,
                                             interest_due=i.interest))
    if not save_draft:
        notify.notify(notify.official_ids(group.id, exclude=user.id), group.id, "LOAN", "Loan awaiting approval",
                      f"{loan.reference} needs approval.", f"/g/{group.id}/loans/{loan.id}")
    return loan


def submit_for_approval(group, loan, user):
    if loan.status != "DRAFT":
        raise LoanError("Only draft loans can be submitted.")
    loan.status = "PENDING_APPROVAL"
    notify.notify(notify.official_ids(group.id, exclude=user.id), group.id, "LOAN", "Loan awaiting approval",
                  f"{loan.reference} needs approval.", f"/g/{group.id}/loans/{loan.id}")


def approve(group, loan, user, comment=None) -> bool:
    """Record an approval. Returns True once the loan reaches APPROVED."""
    if loan.status != "PENDING_APPROVAL":
        raise LoanError("This loan is not awaiting approval.")
    if LoanApproval.query.filter_by(loan_id=loan.id, user_id=user.id).first():
        raise LoanError("You have already recorded a decision on this loan.")
    if loan.approvals_required > 1 and user.id == loan.created_by:
        raise LoanError("The official who created the loan cannot also provide a required approval.")
    db.session.add(LoanApproval(group_id=group.id, loan_id=loan.id, user_id=user.id, decision="APPROVED",
                                comment=(comment or "")[:300]))
    db.session.flush()
    count = LoanApproval.query.filter_by(loan_id=loan.id, decision="APPROVED").count()
    if count >= loan.approvals_required:
        loan.status = "APPROVED"
        loan.approved_at = utcnow()
        feedlog.emit(group.id, "LOAN_APPROVED", "Loan approved", "A new loan was approved.", loan.principal,
                     user.id, "loan", loan.id)
        notify.notify(notify.official_ids(group.id), group.id, "LOAN", "Loan approved",
                      f"{loan.reference} is approved and ready for disbursement.", f"/g/{group.id}/loans/{loan.id}")
        return True
    return False


def reject(group, loan, user, reason):
    if loan.status != "PENDING_APPROVAL":
        raise LoanError("This loan is not awaiting approval.")
    if not (reason or "").strip():
        raise LoanError("A reason is required to reject a loan.")
    db.session.add(LoanApproval(group_id=group.id, loan_id=loan.id, user_id=user.id, decision="REJECTED",
                                comment=reason.strip()[:300]))
    loan.status = "REJECTED"
    loan.rejection_reason = reason.strip()[:300]


def disburse(group, loan, user, disbursed_on):
    """Only an APPROVED loan can be disbursed, and only once a real ledger
    disbursement exists is it treated as lent out."""
    if loan.status != "APPROVED":
        raise LoanError("Only approved loans can be disbursed.")
    if loan.disbursement_tx_id:
        raise LoanError("This loan has already been disbursed.")
    if loan.refinance_of_id:  # settle the old loan first: its payoff is cash coming in
        old = db.session.get(Loan, loan.refinance_of_id)
        quote = settlement_quote(old, disbursed_on)
        if loan.principal < quote["payoff"]:
            raise LoanError(f"The old loan now needs {quote['payoff']:,} to settle, more than this loan's principal. "
                            "Create a new refinancing loan.")
        settle_early(group, old, user, disbursed_on, method=f"Refinanced into {loan.reference}")
    cash = ledger.cash_balance(group.id)
    if loan.principal > cash and not importctx.active():  # historical loans predate imported savings
        raise LoanError("Insufficient available cash to disburse this loan.")
    tx = ledger.post(group.id, "LOAN_DISBURSEMENT", loan.principal, "OUT", disbursed_on, user.id,
                     description=f"Loan disbursement {loan.reference}", borrower_id=loan.borrower_id,
                     loan_id=loan.id, prefix="DSB")
    loan.disbursement_tx_id = tx.id
    fees = (loan.fee_processing or 0) + (loan.fee_insurance or 0)
    if fees > 0:
        loan.fee_tx_id = ledger.post(group.id, "FEE", fees, "IN", disbursed_on, user.id,
                                     description=f"Loan fees {loan.reference} (processing/insurance)",
                                     borrower_id=loan.borrower_id, loan_id=loan.id, prefix="FEE").id
    loan.status = "ACTIVE"
    loan.disbursed_at = utcnow()
    feedlog.emit(group.id, "LOAN_DISBURSED", "Loan disbursed", "Funds were lent out.", loan.principal,
                 user.id, "loan", loan.id)
    notify.notify(notify.official_ids(group.id), group.id, "LOAN", "Loan disbursed",
                  f"{loan.reference} was disbursed.", f"/g/{group.id}/loans/{loan.id}")
    return tx


def reverse_disbursement(group, loan, user, reason, today):
    if loan.status != "ACTIVE" or not loan.disbursement_tx_id:
        raise LoanError("Only an active loan with no repayments can have its disbursement reversed.")
    if loan.refinance_of_id:
        raise LoanError("A refinancing loan cannot be reversed because it settled an older loan. "
                        "Write it off or reverse the settlement repayment on the old loan first.")
    if LoanRepayment.query.filter_by(loan_id=loan.id, status="POSTED").first():
        raise LoanError("Reverse the repayments first.")
    tx = db.session.get(Transaction, loan.disbursement_tx_id)
    try:
        ledger.reverse(tx, user.id, reason, today)
    except ledger.LedgerError as e:
        raise LoanError(str(e))
    if loan.fee_tx_id:
        ledger.reverse(db.session.get(Transaction, loan.fee_tx_id), user.id, reason, today)
        loan.fee_tx_id = None
    loan.disbursement_tx_id = None
    loan.status = "APPROVED"
    loan.disbursed_at = None


def settlement_quote(loan, today) -> dict:
    """What it takes to close a loan today: the principal still owed plus interest on instalments
    already due. Interest on instalments not yet due is waived (an early-settlement rebate)."""
    principal = interest_now = rebate = 0
    for s in loan.schedule:
        principal += s.principal_due - s.principal_paid
        unpaid_interest = s.interest_due - s.interest_paid
        if s.due_date <= today:
            interest_now += unpaid_interest
        else:
            rebate += unpaid_interest
    return {"principal": principal, "interest": interest_now, "rebate": rebate, "payoff": principal + interest_now}


def settle_early(group, loan, user, today, method="Early settlement"):
    """Close an open loan: waive interest on instalments not yet due, then record one repayment for the
    rest. The repayment goes through the normal engine, so the ledger and reports stay consistent."""
    if loan.status not in ACTIVE_STATES:
        raise LoanError("Only an open loan can be settled.")
    q = settlement_quote(loan, today)
    for s in loan.schedule:
        if s.due_date > today and s.interest_due > s.interest_paid:
            s.interest_due = s.interest_paid
    loan.interest_rebate = (loan.interest_rebate or 0) + q["rebate"]
    if q["payoff"] > 0:
        record_repayment(group, loan, user, q["payoff"], today, payment_method=method,
                         notes="Early settlement" + (f" (interest rebate {q['rebate']})" if q["rebate"] else ""),
                         skip_recent_check=True)
    else:
        refresh_status(loan)
    return q


def write_off(group, loan, user, reason):
    if loan.status not in ACTIVE_STATES:
        raise LoanError("Only active loans can be written off.")
    if not (reason or "").strip():
        raise LoanError("A reason is required to write off a loan.")
    loan.status = "WRITTEN_OFF"
    loan.write_off_reason = reason.strip()[:300]
    loan.closed_at = utcnow()


# ------------------------------------------------------------------ balances
def outstanding(loan):
    """(principal_outstanding, interest_outstanding) from the schedule."""
    if loan.status in ("DRAFT", "PENDING_APPROVAL", "APPROVED", "REJECTED") or not loan.disbursement_tx_id:
        return 0, 0
    p = sum(s.principal_due - s.principal_paid for s in loan.schedule)
    i = sum(s.interest_due - s.interest_paid for s in loan.schedule)
    return p, i


def next_due(loan):
    for s in loan.schedule:
        if s.balance > 0:
            return s
    return None


def is_overdue(loan, today):
    if loan.status not in ACTIVE_STATES:
        return False
    return any(s.balance > 0 and s.due_date < today for s in loan.schedule)


def effective_status(loan, today):
    return "OVERDUE" if is_overdue(loan, today) else loan.status


def refresh_status(loan):
    if loan.status not in ("ACTIVE", "PARTIALLY_REPAID", "OVERDUE", "FULLY_REPAID"):
        return
    remaining = sum(s.balance for s in loan.schedule)
    paid = sum(s.total_paid for s in loan.schedule)
    if remaining == 0:
        if loan.status != "FULLY_REPAID":
            loan.closed_at = utcnow()
        loan.status = "FULLY_REPAID"
    else:
        loan.closed_at = None
        loan.status = "PARTIALLY_REPAID" if paid > 0 else "ACTIVE"


# --------------------------------------------------------------- repayments
def record_repayment(group, loan, user, amount, paid_on, payment_method=None, payment_reference=None,
                     notes=None, idempotency_key=None, skip_recent_check=False):
    if loan.group_id != group.id:
        raise LoanError("Loan not found.")
    if loan.status not in ACTIVE_STATES:
        raise LoanError("Repayments can only be recorded on active loans.")
    if amount <= 0:
        raise LoanError("Repayment amount must be greater than zero.")
    if idempotency_key:
        existing = LoanRepayment.query.filter_by(group_id=group.id, idempotency_key=idempotency_key).first()
        if existing:
            raise DuplicateError("This repayment was already recorded.", existing)
    if payment_reference:
        same_ref = LoanRepayment.query.filter_by(group_id=group.id, payment_reference=payment_reference).first()
        if same_ref:
            raise DuplicateError("A repayment with this reference number already exists.", same_ref)
    recent = None if (importctx.active() or skip_recent_check) else LoanRepayment.query.filter(
        LoanRepayment.loan_id == loan.id, LoanRepayment.amount == amount, LoanRepayment.payment_date == paid_on,
        LoanRepayment.status == "POSTED",
        LoanRepayment.created_at >= utcnow() - timedelta(minutes=2),
    ).first()
    if recent:
        raise DuplicateError("An identical repayment was recorded moments ago. Possible duplicate.", recent)

    p_out, i_out = outstanding(loan)
    if amount > p_out + i_out:
        raise LoanError("The amount is more than the total outstanding balance on this loan.")

    left = amount
    alloc, p_total, i_total = [], 0, 0
    for s in loan.schedule:
        if left == 0:
            break
        i_pay = min(left, s.interest_due - s.interest_paid)
        left -= i_pay
        p_pay = min(left, s.principal_due - s.principal_paid)
        left -= p_pay
        if i_pay or p_pay:
            s.interest_paid += i_pay
            s.principal_paid += p_pay
            alloc.append((s, p_pay, i_pay))
            p_total += p_pay
            i_total += i_pay

    ref = ledger.new_ref("RP")
    rep = LoanRepayment(
        group_id=group.id, loan_id=loan.id, borrower_id=loan.borrower_id, amount=amount, principal_part=p_total,
        interest_part=i_total, payment_date=paid_on, payment_method=payment_method,
        payment_reference=payment_reference or None, notes=(notes or "")[:300], receipt_no=ref,
        idempotency_key=idempotency_key, recorded_by=user.id,
    )
    db.session.add(rep)
    db.session.flush()
    desc = f"Repayment {loan.reference} ({ref})"
    if i_total:
        rep.interest_tx_id = ledger.post(group.id, "INTEREST_REPAYMENT", i_total, "IN", paid_on, user.id, desc,
                                         payment_method, payment_reference, borrower_id=loan.borrower_id,
                                         loan_id=loan.id, prefix="INT").id
    if p_total:
        rep.principal_tx_id = ledger.post(group.id, "PRINCIPAL_REPAYMENT", p_total, "IN", paid_on, user.id, desc,
                                          payment_method, payment_reference, borrower_id=loan.borrower_id,
                                          loan_id=loan.id, prefix="PRN").id
    for s, p_pay, i_pay in alloc:
        db.session.add(RepaymentAllocation(group_id=group.id, repayment_id=rep.id, schedule_id=s.id,
                                           principal=p_pay, interest=i_pay))
    was = loan.status
    refresh_status(loan)
    feedlog.emit(group.id, "REPAYMENT", "Repayment received", "A loan repayment was received.", amount,
                 user.id, "repayment", rep.id)
    if loan.status == "FULLY_REPAID" and was != "FULLY_REPAID":
        feedlog.emit(group.id, "LOAN_REPAID", "Loan fully repaid", "A loan has been fully repaid.",
                     loan.total_repayable, user.id, "loan", loan.id)
    notify.notify(notify.official_ids(group.id, exclude=user.id), group.id, "REPAYMENT", "Repayment recorded",
                  f"{loan.reference}: repayment recorded.", f"/g/{group.id}/loans/{loan.id}")
    from app.services import fines
    # lending businesses use their own late-fine rule (services/lender.py); savings groups use services/fines.py
    rep.assessed_fine = None if group.is_lender else fines.assess_on_repayment(group, loan, user, rep, alloc, paid_on)
    return rep


def reverse_repayment(group, rep: LoanRepayment, user, reason, today):
    if rep.group_id != group.id or rep.status != "POSTED":
        raise LoanError("Only posted repayments can be reversed.")
    if not (reason or "").strip():
        raise LoanError("A reason is required to reverse a repayment.")
    for txid in (rep.interest_tx_id, rep.principal_tx_id):
        if txid:
            ledger.reverse(db.session.get(Transaction, txid), user.id, reason, today)
    for a in rep.allocations:
        s = db.session.get(LoanRepaymentSchedule, a.schedule_id)
        s.principal_paid -= a.principal
        s.interest_paid -= a.interest
    from app.services import fines
    fines.void_for_repayment(rep, user, reason, today)
    rep.status = "REVERSED"
    rep.reversed_by = user.id
    rep.reversed_at = utcnow()
    rep.reversal_reason = reason.strip()[:300]
    refresh_status(rep.loan)


# ----------------------------------------------------------------- portfolio
def portfolio(group, today):
    """Lending analytics. Principal/interest outstanding come from schedules;
    cash-side figures come from the ledger, and the two reconcile."""
    t = ledger.totals(group.id)
    loans = Loan.query.filter(Loan.group_id == group.id, Loan.disbursement_tx_id.isnot(None)).all()
    active = [l for l in loans if l.status in ACTIVE_STATES]
    p_out = sum(outstanding(l)[0] for l in active)
    i_out = sum(outstanding(l)[1] for l in active)
    written_off = sum(sum(s.principal_due - s.principal_paid for s in l.schedule)
                      for l in loans if l.status == "WRITTEN_OFF")
    return {
        "loan_count": len(loans),
        "principal_disbursed": t["disbursed"],
        "principal_outstanding": p_out,
        "principal_written_off": written_off,
        "principal_repaid": t["principal_repaid"],
        # Full-term interest counts as EARNED only once the borrower has finished the loan. For loans still
        # running, see "interest_collected" (received so far) and the Interest page (expected / incoming).
        "interest_earned": sum(l.total_interest - (l.interest_rebate or 0) for l in loans if l.status == "FULLY_REPAID"),
        "interest_collected": t["interest_collected"],
        "interest_receivable": i_out,
        "total_repayments": t["total_repayments"],
        "active_loans": len(active),
        "overdue_loans": sum(1 for l in active if is_overdue(l, today)),
        "fully_repaid_loans": sum(1 for l in loans if l.status == "FULLY_REPAID"),
        "active_borrowers": len({l.borrower_id for l in active}),
    }
