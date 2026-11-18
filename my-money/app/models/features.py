"""Models for shares, savings withdrawals, ranks, contribution schedules, loan
fines, and the officials' income / expense records. Amounts are integer minor
units; rates are basis points of a percent (5.00% = 500)."""
from sqlalchemy import CheckConstraint, UniqueConstraint

from app.extensions import db
from app.models.core import utcnow


class GroupSettings(db.Model):
    """One row per group: every rule the group's own officials control."""
    __tablename__ = "group_settings"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, unique=True)
    # savings / shares
    min_savings_balance = db.Column(db.BigInteger, nullable=False, default=0)
    share_price = db.Column(db.BigInteger, nullable=False, default=10000)
    leaderboard_on = db.Column(db.Boolean, nullable=False, default=True)
    # member contribution schedule (frequency is set by the admin, the day by each member)
    contribution_period = db.Column(db.String(10), nullable=False, default="MONTHLY")
    reminder_days = db.Column(db.Integer, nullable=False, default=7)
    last_reminder_date = db.Column(db.Date)
    last_unpaid_count = db.Column(db.Integer, nullable=False, default=0)
    # loan fines (external borrowers only)
    fine_grace_days = db.Column(db.Integer, nullable=False, default=7)
    fine_period_days = db.Column(db.Integer, nullable=False, default=7)
    fine_type = db.Column(db.String(8), nullable=False, default="PERCENT")
    fine_rate_bp = db.Column(db.Integer, nullable=False, default=500)
    fine_fixed = db.Column(db.BigInteger, nullable=False, default=0)
    fine_capped = db.Column(db.Boolean, nullable=False, default=True)
    # loan interest policy (was a single platform-wide row; now per group)
    default_rate_bp = db.Column(db.Integer, nullable=False, default=1000)
    min_rate_bp = db.Column(db.Integer, nullable=False, default=500)
    max_rate_bp = db.Column(db.Integer, nullable=False, default=2000)
    flat_enabled = db.Column(db.Boolean, nullable=False, default=True)
    reducing_enabled = db.Column(db.Boolean, nullable=False, default=True)
    default_period = db.Column(db.String(10), nullable=False, default="MONTH")
    allow_custom_rates = db.Column(db.Boolean, nullable=False, default=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        CheckConstraint("contribution_period IN ('WEEKLY','MONTHLY','ANNUAL')", name="ck_gs_period"),
        CheckConstraint("fine_type IN ('PERCENT','FIXED')", name="ck_gs_fine_type"),
    )


class MemberSchedule(db.Model):
    """A member's own contribution day. WEEKLY: due_day = weekday 0-6 (Mon=0).
    MONTHLY: due_day = day of month 1-28. ANNUAL: due_month 1-12 and due_day 1-28."""
    __tablename__ = "member_schedules"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    member_id = db.Column(db.Integer, db.ForeignKey("group_members.id"), nullable=False)
    due_day = db.Column(db.Integer, nullable=False, default=1)
    due_month = db.Column(db.Integer)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    member = db.relationship("GroupMember")
    __table_args__ = (UniqueConstraint("group_id", "member_id", name="uq_member_schedule"),)


class ShareEntry(db.Model):
    """One movement of a member's shares. A member's holding is the sum of the
    units of their un-reversed entries."""
    __tablename__ = "share_entries"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    member_id = db.Column(db.Integer, db.ForeignKey("group_members.id"), nullable=False, index=True)
    kind = db.Column(db.String(14), nullable=False)  # PURCHASE / TRANSFER_OUT / TRANSFER_IN
    units = db.Column(db.Integer, nullable=False)  # signed
    unit_price = db.Column(db.BigInteger, nullable=False, default=0)
    amount = db.Column(db.BigInteger, nullable=False, default=0)
    entry_date = db.Column(db.Date, nullable=False)
    note = db.Column(db.String(300))
    tx_id = db.Column(db.Integer, db.ForeignKey("transactions.id"))
    transfer_id = db.Column(db.Integer, db.ForeignKey("share_transfers.id"))
    receipt_no = db.Column(db.String(30))
    recorded_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    reversed_at = db.Column(db.DateTime)
    reversal_reason = db.Column(db.String(300))

    member = db.relationship("GroupMember")
    __table_args__ = (CheckConstraint("kind IN ('PURCHASE','TRANSFER_OUT','TRANSFER_IN')", name="ck_share_kind"),)


class ShareTransfer(db.Model):
    __tablename__ = "share_transfers"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    seller_id = db.Column(db.Integer, db.ForeignKey("group_members.id"), nullable=False)
    buyer_id = db.Column(db.Integer, db.ForeignKey("group_members.id"), nullable=False)
    units = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.BigInteger, nullable=False, default=0)
    status = db.Column(db.String(10), nullable=False, default="PENDING")
    note = db.Column(db.String(300))
    requested_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    decided_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    decided_at = db.Column(db.DateTime)
    decision_note = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    seller = db.relationship("GroupMember", foreign_keys=[seller_id])
    buyer = db.relationship("GroupMember", foreign_keys=[buyer_id])
    __table_args__ = (
        CheckConstraint("units > 0", name="ck_transfer_units"),
        CheckConstraint("status IN ('PENDING','APPROVED','REJECTED','CANCELLED')", name="ck_transfer_status"),
    )


class ShareRank(db.Model):
    __tablename__ = "share_ranks"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    name = db.Column(db.String(40), nullable=False)
    min_value = db.Column(db.BigInteger, nullable=False, default=0)
    icon = db.Column(db.String(30), nullable=False, default="sprout")


class SavingsWithdrawal(db.Model):
    __tablename__ = "savings_withdrawals"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    member_id = db.Column(db.Integer, db.ForeignKey("group_members.id"), nullable=False, index=True)
    amount = db.Column(db.BigInteger, nullable=False)
    status = db.Column(db.String(10), nullable=False, default="PENDING")
    note = db.Column(db.String(300))
    payment_method = db.Column(db.String(40))
    requested_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    decided_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    decided_at = db.Column(db.DateTime)
    decision_note = db.Column(db.String(300))
    tx_id = db.Column(db.Integer, db.ForeignKey("transactions.id"))
    receipt_no = db.Column(db.String(30))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    member = db.relationship("GroupMember")
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_wd_amount"),
        CheckConstraint("status IN ('PENDING','APPROVED','REJECTED','CANCELLED','REVERSED')", name="ck_wd_status"),
    )


class LoanPromise(db.Model):
    """The borrower's promised payment date for a loan (used as the deadline
    for fines instead of the schedule's due date)."""
    __tablename__ = "loan_promises"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    loan_id = db.Column(db.Integer, db.ForeignKey("loans.id"), nullable=False, index=True)
    promise_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.BigInteger, nullable=False)
    note = db.Column(db.String(300))
    status = db.Column(db.String(8), nullable=False, default="OPEN")  # OPEN / KEPT / CANCELLED
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    loan = db.relationship("Loan", backref="promises")
    __table_args__ = (CheckConstraint("status IN ('OPEN','KEPT','CANCELLED')", name="ck_promise_status"),)


class LoanFine(db.Model):
    __tablename__ = "loan_fines"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    loan_id = db.Column(db.Integer, db.ForeignKey("loans.id"), nullable=False, index=True)
    borrower_id = db.Column(db.Integer, db.ForeignKey("borrowers.id"), nullable=False)
    repayment_id = db.Column(db.Integer, db.ForeignKey("loan_repayments.id"))
    deadline = db.Column(db.Date, nullable=False)
    paid_on = db.Column(db.Date, nullable=False)
    days_late = db.Column(db.Integer, nullable=False, default=0)
    late_amount = db.Column(db.BigInteger, nullable=False, default=0)
    amount = db.Column(db.BigInteger, nullable=False)
    status = db.Column(db.String(8), nullable=False, default="UNPAID")  # UNPAID / PAID / WAIVED / VOID
    reason = db.Column(db.String(300))
    tx_id = db.Column(db.Integer, db.ForeignKey("transactions.id"))
    settled_on = db.Column(db.Date)
    decided_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    loan = db.relationship("Loan", backref="fines")
    borrower = db.relationship("Borrower")
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_fine_amount"),
        CheckConstraint("status IN ('UNPAID','PAID','WAIVED','VOID')", name="ck_fine_status"),
    )


class IncomeCategory(db.Model):
    __tablename__ = "income_categories"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    name = db.Column(db.String(60), nullable=False)
    __table_args__ = (UniqueConstraint("group_id", "name", name="uq_income_cat"),)


class IncomeRecord(db.Model):
    __tablename__ = "income_records"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    category_id = db.Column(db.Integer, db.ForeignKey("income_categories.id"), nullable=False)
    amount = db.Column(db.BigInteger, nullable=False)
    income_date = db.Column(db.Date, nullable=False)
    description = db.Column(db.String(300))
    tx_id = db.Column(db.Integer, db.ForeignKey("transactions.id"), nullable=False, unique=True)
    recorded_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    category = db.relationship("IncomeCategory")
    tx = db.relationship("Transaction")
    __table_args__ = (CheckConstraint("amount > 0", name="ck_income_amount"),)


class ExpenseCategory(db.Model):
    __tablename__ = "expense_categories"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    name = db.Column(db.String(60), nullable=False)
    __table_args__ = (UniqueConstraint("group_id", "name", name="uq_expense_cat"),)


class ExpenseItem(db.Model):
    """One line of an itemised expense (e.g. venue, food, transport)."""
    __tablename__ = "expense_items"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    expense_id = db.Column(db.Integer, db.ForeignKey("expenses.id"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    amount = db.Column(db.BigInteger, nullable=False)
    __table_args__ = (CheckConstraint("amount > 0", name="ck_item_amount"),)
