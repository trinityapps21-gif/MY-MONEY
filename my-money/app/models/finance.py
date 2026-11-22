"""Financial models. All amounts are integers in minor currency units
(UGX has no minor unit, so 1 unit = 1 UGX). Rates are stored as basis points of
a percent: 10.00% = 1000."""
from sqlalchemy import CheckConstraint, UniqueConstraint

from app.extensions import db
from app.models.core import utcnow

TX_IN_TYPES = ("CONTRIBUTION", "SAVINGS_ADDITIONAL", "PRINCIPAL_REPAYMENT", "INTEREST_REPAYMENT", "PENALTY", "FEE")
TX_OUT_TYPES = ("LOAN_DISBURSEMENT", "EXPENSE")
TX_TYPES = TX_IN_TYPES + TX_OUT_TYPES + ("ADJUSTMENT",)


class Borrower(db.Model):
    __tablename__ = "borrowers"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    name = db.Column(db.String(150), nullable=False)
    phone = db.Column(db.String(30))
    location = db.Column(db.String(150))
    category = db.Column(db.String(40), nullable=False, default="Individual")
    id_number = db.Column(db.String(60))
    registered_on = db.Column(db.Date, nullable=False)
    notes = db.Column(db.Text)
    # KYC (lending businesses)
    date_of_birth = db.Column(db.Date)
    address = db.Column(db.String(250))
    occupation = db.Column(db.String(120))
    employer = db.Column(db.String(150))
    monthly_income = db.Column(db.BigInteger)
    next_of_kin_name = db.Column(db.String(150))
    next_of_kin_phone = db.Column(db.String(30))
    next_of_kin_relation = db.Column(db.String(60))
    officer_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    blacklisted = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    blacklist_reason = db.Column(db.String(300))
    blacklisted_at = db.Column(db.DateTime)
    portal_nonce = db.Column(db.String(16))  # change it to revoke every link already sent to this borrower
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class Transaction(db.Model):
    """Append-only ledger. Rows are never deleted or edited; corrections are
    reversal rows (is_reversal=True, opposite direction, reverses_id set)."""
    __tablename__ = "transactions"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False)
    reference = db.Column(db.String(40), nullable=False)
    tx_type = db.Column(db.String(30), nullable=False)
    direction = db.Column(db.String(3), nullable=False)
    amount = db.Column(db.BigInteger, nullable=False)
    tx_date = db.Column(db.Date, nullable=False)
    description = db.Column(db.String(300))
    payment_method = db.Column(db.String(40))
    payment_reference = db.Column(db.String(80))
    member_id = db.Column(db.Integer, db.ForeignKey("group_members.id"))
    borrower_id = db.Column(db.Integer, db.ForeignKey("borrowers.id"))
    loan_id = db.Column(db.Integer, db.ForeignKey("loans.id", use_alter=True, name="fk_tx_loan"))
    is_reversal = db.Column(db.Boolean, nullable=False, default=False)
    reverses_id = db.Column(db.Integer, db.ForeignKey("transactions.id"), unique=True)
    reversal_reason = db.Column(db.String(300))
    # Sub-classification of rows that share a base tx_type: SHARE_PURCHASE, SAVINGS_WITHDRAWAL, INCOME, FINE.
    kind = db.Column(db.String(20))
    recorded_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    member = db.relationship("GroupMember")
    borrower = db.relationship("Borrower")
    recorder = db.relationship("User", foreign_keys=[recorded_by])

    __table_args__ = (
        UniqueConstraint("group_id", "reference", name="uq_tx_group_ref"),
        CheckConstraint("amount > 0", name="ck_tx_amount"),
        CheckConstraint("direction IN ('IN','OUT')", name="ck_tx_direction"),
        CheckConstraint(
            "tx_type IN ('CONTRIBUTION','SAVINGS_ADDITIONAL','PRINCIPAL_REPAYMENT','INTEREST_REPAYMENT',"
            "'PENALTY','FEE','LOAN_DISBURSEMENT','EXPENSE','ADJUSTMENT')",
            name="ck_tx_type",
        ),
        db.Index("ix_tx_group_date", "group_id", "tx_date"),
        db.Index("ix_tx_group_type", "group_id", "tx_type"),
    )


class Contribution(db.Model):
    __tablename__ = "contributions"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    member_id = db.Column(db.Integer, db.ForeignKey("group_members.id"), nullable=False, index=True)
    amount = db.Column(db.BigInteger, nullable=False)
    category = db.Column(db.String(12), nullable=False, default="MONTHLY")
    contribution_date = db.Column(db.Date, nullable=False)
    payment_method = db.Column(db.String(40))
    payment_reference = db.Column(db.String(80))
    notes = db.Column(db.String(300))
    receipt_no = db.Column(db.String(30), nullable=False)
    tx_id = db.Column(db.Integer, db.ForeignKey("transactions.id"), nullable=False, unique=True)
    idempotency_key = db.Column(db.String(64))
    recorded_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    member = db.relationship("GroupMember")
    tx = db.relationship("Transaction")
    recorder = db.relationship("User", foreign_keys=[recorded_by])

    __table_args__ = (
        UniqueConstraint("group_id", "idempotency_key", name="uq_contrib_idem"),
        CheckConstraint("amount > 0", name="ck_contrib_amount"),
        CheckConstraint("category IN ('WEEKLY','MONTHLY','CUSTOM','VOLUNTARY')", name="ck_contrib_cat"),
    )


class Loan(db.Model):
    __tablename__ = "loans"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    borrower_id = db.Column(db.Integer, db.ForeignKey("borrowers.id"), nullable=False, index=True)
    reference = db.Column(db.String(20), nullable=False)
    principal = db.Column(db.BigInteger, nullable=False)
    rate_bp = db.Column(db.Integer, nullable=False)
    rate_period = db.Column(db.String(10), nullable=False, default="MONTH")
    method = db.Column(db.String(10), nullable=False)
    duration = db.Column(db.Integer, nullable=False)
    duration_unit = db.Column(db.String(10), nullable=False, default="MONTH")
    frequency = db.Column(db.String(10), nullable=False, default="MONTHLY")
    disbursement_date = db.Column(db.Date, nullable=False)
    first_repayment_date = db.Column(db.Date, nullable=False)
    purpose = db.Column(db.String(300))
    notes = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False, default="PENDING_APPROVAL")
    total_interest = db.Column(db.BigInteger, nullable=False)
    total_repayable = db.Column(db.BigInteger, nullable=False)
    approvals_required = db.Column(db.Integer, nullable=False, default=1)
    # lending-business terms, copied at creation like every other term
    product_id = db.Column(db.Integer, db.ForeignKey("loan_products.id"))
    fee_processing = db.Column(db.BigInteger, nullable=False, default=0, server_default="0")
    fee_insurance = db.Column(db.BigInteger, nullable=False, default=0, server_default="0")
    fee_tx_id = db.Column(db.Integer, db.ForeignKey("transactions.id", use_alter=True, name="fk_loan_fee_tx"))
    penalty_type = db.Column(db.String(10), nullable=False, default="NONE", server_default="NONE")
    penalty_value = db.Column(db.BigInteger, nullable=False, default=0, server_default="0")
    penalty_grace_days = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    # Late-fine terms, frozen from the group's rules when the loan is created (see services/fine_terms.py). Changing the
    # group's rules later only affects loans created afterwards; this loan always keeps the terms it was agreed on.
    fine_grace_days = db.Column(db.Integer, nullable=False, default=7, server_default="7")
    fine_period_days = db.Column(db.Integer, nullable=False, default=7, server_default="7")
    fine_type = db.Column(db.String(8), nullable=False, default="PERCENT", server_default="PERCENT")
    fine_rate_bp = db.Column(db.Integer, nullable=False, default=500, server_default="500")
    fine_fixed = db.Column(db.BigInteger, nullable=False, default=0, server_default="0")
    fine_capped = db.Column(db.Boolean, nullable=False, default=True, server_default="1")
    officer_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    interest_rebate = db.Column(db.BigInteger, nullable=False, default=0, server_default="0")  # waived on early settlement
    refinance_of_id = db.Column(db.Integer, db.ForeignKey("loans.id", use_alter=True, name="fk_loan_refinance"))
    rejection_reason = db.Column(db.String(300))
    write_off_reason = db.Column(db.String(300))
    disbursement_tx_id = db.Column(db.Integer, db.ForeignKey("transactions.id"), unique=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    approved_at = db.Column(db.DateTime)
    disbursed_at = db.Column(db.DateTime)
    closed_at = db.Column(db.DateTime)

    borrower = db.relationship("Borrower")
    schedule = db.relationship(
        "LoanRepaymentSchedule", backref="loan", order_by="LoanRepaymentSchedule.number", cascade="all, delete-orphan"
    )
    approvals = db.relationship("LoanApproval", backref="loan")
    creator = db.relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        UniqueConstraint("group_id", "reference", name="uq_loan_ref"),
        CheckConstraint("principal > 0", name="ck_loan_principal"),
        CheckConstraint("rate_bp BETWEEN 0 AND 100000", name="ck_loan_rate"),
        CheckConstraint("method IN ('FLAT','REDUCING')", name="ck_loan_method"),
        CheckConstraint("rate_period IN ('WEEK','MONTH','YEAR')", name="ck_loan_rate_period"),
        CheckConstraint("duration_unit IN ('WEEK','MONTH')", name="ck_loan_unit"),
        CheckConstraint("frequency IN ('WEEKLY','MONTHLY')", name="ck_loan_freq"),
        CheckConstraint(
            "status IN ('DRAFT','PENDING_APPROVAL','APPROVED','ACTIVE','PARTIALLY_REPAID',"
            "'FULLY_REPAID','OVERDUE','REJECTED','WRITTEN_OFF')",
            name="ck_loan_status",
        ),
    )


class LoanApproval(db.Model):
    __tablename__ = "loan_approvals"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    loan_id = db.Column(db.Integer, db.ForeignKey("loans.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    decision = db.Column(db.String(10), nullable=False)
    comment = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    user = db.relationship("User")

    __table_args__ = (
        UniqueConstraint("loan_id", "user_id", name="uq_loan_approver"),
        CheckConstraint("decision IN ('APPROVED','REJECTED')", name="ck_approval_decision"),
    )


class LoanRepaymentSchedule(db.Model):
    __tablename__ = "loan_repayment_schedules"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    loan_id = db.Column(db.Integer, db.ForeignKey("loans.id"), nullable=False, index=True)
    number = db.Column(db.Integer, nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    principal_due = db.Column(db.BigInteger, nullable=False)
    interest_due = db.Column(db.BigInteger, nullable=False)
    principal_paid = db.Column(db.BigInteger, nullable=False, default=0)
    interest_paid = db.Column(db.BigInteger, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("loan_id", "number", name="uq_schedule_number"),
        CheckConstraint("principal_due >= 0 AND interest_due >= 0", name="ck_sched_due"),
        CheckConstraint(
            "principal_paid >= 0 AND interest_paid >= 0 AND principal_paid <= principal_due "
            "AND interest_paid <= interest_due",
            name="ck_sched_paid",
        ),
    )

    @property
    def total_due(self):
        return self.principal_due + self.interest_due

    @property
    def total_paid(self):
        return self.principal_paid + self.interest_paid

    @property
    def balance(self):
        return self.total_due - self.total_paid


class LoanRepayment(db.Model):
    __tablename__ = "loan_repayments"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    loan_id = db.Column(db.Integer, db.ForeignKey("loans.id"), nullable=False, index=True)
    borrower_id = db.Column(db.Integer, db.ForeignKey("borrowers.id"), nullable=False)
    amount = db.Column(db.BigInteger, nullable=False)
    principal_part = db.Column(db.BigInteger, nullable=False, default=0)
    interest_part = db.Column(db.BigInteger, nullable=False, default=0)
    payment_date = db.Column(db.Date, nullable=False)
    payment_method = db.Column(db.String(40))
    payment_reference = db.Column(db.String(80))
    notes = db.Column(db.String(300))
    status = db.Column(db.String(10), nullable=False, default="POSTED")
    receipt_no = db.Column(db.String(30), nullable=False)
    idempotency_key = db.Column(db.String(64))
    principal_tx_id = db.Column(db.Integer, db.ForeignKey("transactions.id"))
    interest_tx_id = db.Column(db.Integer, db.ForeignKey("transactions.id"))
    recorded_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    reversed_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    reversed_at = db.Column(db.DateTime)
    reversal_reason = db.Column(db.String(300))

    loan = db.relationship("Loan", backref="repayments")
    borrower = db.relationship("Borrower")
    recorder = db.relationship("User", foreign_keys=[recorded_by])
    allocations = db.relationship("RepaymentAllocation", backref="repayment")

    __table_args__ = (
        UniqueConstraint("group_id", "idempotency_key", name="uq_repay_idem"),
        UniqueConstraint("group_id", "payment_reference", name="uq_repay_payref"),
        CheckConstraint("amount > 0", name="ck_repay_amount"),
        CheckConstraint("status IN ('POSTED','REVERSED')", name="ck_repay_status"),
    )


class RepaymentAllocation(db.Model):
    """How a repayment was applied to schedule rows (needed to reverse it)."""
    __tablename__ = "repayment_allocations"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    repayment_id = db.Column(db.Integer, db.ForeignKey("loan_repayments.id"), nullable=False, index=True)
    schedule_id = db.Column(db.Integer, db.ForeignKey("loan_repayment_schedules.id"), nullable=False)
    principal = db.Column(db.BigInteger, nullable=False, default=0)
    interest = db.Column(db.BigInteger, nullable=False, default=0)


class Expense(db.Model):
    __tablename__ = "expenses"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    category = db.Column(db.String(60), nullable=False)
    description = db.Column(db.String(300))
    amount = db.Column(db.BigInteger, nullable=False)
    expense_date = db.Column(db.Date, nullable=False)
    tx_id = db.Column(db.Integer, db.ForeignKey("transactions.id"), nullable=False, unique=True)
    recorded_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    __table_args__ = (CheckConstraint("amount > 0", name="ck_expense_amount"),)
