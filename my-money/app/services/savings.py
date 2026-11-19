"""Savings / contribution business logic."""
from datetime import timedelta

from app.extensions import db
from app.models import Contribution, GroupMember, SavingsWithdrawal, Transaction, utcnow
from app.services import feedlog, ledger, notify

CATEGORY_TX = {
    "WEEKLY": "CONTRIBUTION",
    "MONTHLY": "CONTRIBUTION",
    "CUSTOM": "CONTRIBUTION",
    "VOLUNTARY": "SAVINGS_ADDITIONAL",
}
CATEGORY_LABELS = {"WEEKLY": "Weekly contribution", "MONTHLY": "Monthly contribution",
                   "CUSTOM": "Custom contribution", "VOLUNTARY": "Voluntary savings"}
PAYMENT_METHODS = ["Cash", "Mobile Money", "Bank Transfer", "Cheque", "Other"]


class DuplicateError(ValueError):
    def __init__(self, message, existing=None):
        super().__init__(message)
        self.existing = existing


def record_contribution(group, user, member: GroupMember, amount, category, contribution_date,
                        payment_method=None, payment_reference=None, notes=None, idempotency_key=None):
    if member.group_id != group.id or member.status != "ACTIVE":
        raise ValueError("Select an active member of this group")
    if category not in CATEGORY_TX:
        raise ValueError("Select a valid contribution category")
    if amount <= 0:
        raise ValueError("Amount must be greater than zero")
    if idempotency_key:
        existing = Contribution.query.filter_by(group_id=group.id, idempotency_key=idempotency_key).first()
        if existing:
            raise DuplicateError("This contribution was already recorded.", existing)
    before = ledger.totals(group.id)["total_savings"]
    tx = ledger.post(
        group.id, CATEGORY_TX[category], amount, "IN", contribution_date, user.id,
        description=f"{CATEGORY_LABELS[category]} - {member.user.full_name}",
        payment_method=payment_method, payment_reference=payment_reference, member_id=member.id, prefix="CTB",
    )
    c = Contribution(
        group_id=group.id, member_id=member.id, amount=amount, category=category,
        contribution_date=contribution_date, payment_method=payment_method,
        payment_reference=payment_reference, notes=(notes or "")[:300], receipt_no=ledger.new_ref("RC"),
        tx_id=tx.id, idempotency_key=idempotency_key, recorded_by=user.id,
    )
    db.session.add(c)
    db.session.flush()
    feedlog.emit(group.id, "CONTRIBUTION", "New contribution",
                 f"{member.user.full_name} contributed.", amount, user.id, "contribution", c.id)
    feedlog.check_savings_milestone(group, before, before + amount, user.id)
    notify.notify([member.user_id], group.id, "CONTRIBUTION", "Contribution recorded",
                  f"Your contribution was recorded. Receipt {c.receipt_no}.", f"/g/{group.id}/savings/me")
    return c


def reverse_contribution(group, contribution: Contribution, user, reason, today):
    rev = ledger.reverse(contribution.tx, user.id, reason, today)
    notify.notify([contribution.member.user_id], group.id, "CONTRIBUTION", "Contribution reversed",
                  f"A contribution of yours was reversed: {reason.strip()[:120]}", f"/g/{group.id}/savings/me")
    return rev


def expected_for_member(group, member: GroupMember, today) -> int:
    """Expected contributions since the member joined, based on the group's
    contribution frequency and default amount (CUSTOM frequency has no expectation)."""
    if group.contribution_frequency == "CUSTOM" or not group.default_contribution:
        return 0
    start = max((member.joined_at or member.created_at).date(), group.created_at.date())
    if start > today:
        return 0
    if group.contribution_frequency == "WEEKLY":
        periods = (today - start).days // 7 + 1
    else:
        periods = (today.year - start.year) * 12 + (today.month - start.month) + 1
    return periods * group.default_contribution


# --------------------------------------------------------------- withdrawals
class WithdrawalError(ValueError):
    pass


def available_to_withdraw(group, member) -> int:
    """Savings the member may take out now: balance minus the group's minimum."""
    from app.services.groupcfg import get_settings
    pending = sum(w.amount for w in SavingsWithdrawal.query.filter_by(
        group_id=group.id, member_id=member.id, status="PENDING"))
    bal = ledger.member_savings(group.id, member.id)
    return max(0, bal - get_settings(group.id).min_savings_balance - pending)


def request_withdrawal(group, user, member, amount, note=None):
    if member.group_id != group.id or member.status != "ACTIVE":
        raise WithdrawalError("Select an active member of this group.")
    if amount <= 0:
        raise WithdrawalError("Amount must be greater than zero.")
    avail = available_to_withdraw(group, member)
    if amount > avail:
        from app.services.groupcfg import get_settings
        raise WithdrawalError(
            f"You can withdraw at most {avail:,} now. The group keeps a minimum balance of "
            f"{get_settings(group.id).min_savings_balance:,} in every member's savings.")
    w = SavingsWithdrawal(group_id=group.id, member_id=member.id, amount=amount, note=(note or "")[:300],
                          requested_by=user.id)
    db.session.add(w)
    db.session.flush()
    if user.id == member.user_id:
        notify.notify_officials(group.id, "WITHDRAWAL", "Withdrawal request",
                                f"{member.user.full_name} asked to withdraw savings.",
                                f"/g/{group.id}/savings/withdrawals", exclude=user.id)
    return w


def approve_withdrawal(group, w, user, today, method=None):
    if w.group_id != group.id or w.status != "PENDING":
        raise WithdrawalError("This request is no longer pending.")
    from app.services.groupcfg import get_settings
    bal = ledger.member_savings(group.id, w.member_id)
    if bal - w.amount < get_settings(group.id).min_savings_balance:
        raise WithdrawalError("This would take the member below the group's minimum savings balance.")
    if w.amount > ledger.cash_balance(group.id):
        raise WithdrawalError("The group does not have enough cash for this withdrawal.")
    tx = ledger.post(group.id, "SAVINGS_ADDITIONAL", w.amount, "OUT", today, user.id,
                     f"Savings withdrawal - {w.member.user.full_name}", method, None, member_id=w.member_id,
                     prefix="WDL", kind="SAVINGS_WITHDRAWAL")
    w.status, w.decided_by, w.decided_at, w.tx_id = "APPROVED", user.id, utcnow(), tx.id
    w.payment_method = method
    w.receipt_no = ledger.new_ref("WD")
    notify.notify([w.member.user_id], group.id, "WITHDRAWAL", "Withdrawal approved",
                  f"Your withdrawal was approved. Receipt {w.receipt_no}.", f"/g/{group.id}/savings/me")


def reject_withdrawal(group, w, user, note=None):
    if w.group_id != group.id or w.status != "PENDING":
        raise WithdrawalError("This request is no longer pending.")
    w.status, w.decided_by, w.decided_at, w.decision_note = "REJECTED", user.id, utcnow(), (note or "")[:300]
    notify.notify([w.member.user_id], group.id, "WITHDRAWAL", "Withdrawal not approved",
                  (note or "Your withdrawal request was not approved.")[:200], f"/g/{group.id}/savings/me")


def cancel_withdrawal(group, w, user):
    if w.group_id != group.id or w.status != "PENDING":
        raise WithdrawalError("This request is no longer pending.")
    w.status = "CANCELLED"


def reverse_withdrawal(group, w, user, reason, today):
    if w.group_id != group.id or w.status != "APPROVED":
        raise WithdrawalError("Only approved withdrawals can be reversed.")
    ledger.reverse(db.session.get(Transaction, w.tx_id), user.id, reason, today)
    w.status = "REVERSED"
