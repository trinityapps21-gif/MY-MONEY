"""Models used by lending businesses: borrower documents, guarantors,
collateral, loan products and late-payment penalties."""
from sqlalchemy import CheckConstraint, UniqueConstraint

from app.extensions import db
from app.models.core import utcnow

DOC_TYPES = {
    "PHOTO": "Passport photo", "ID_FRONT": "National ID (front)", "ID_BACK": "National ID (back)",
    "ADDRESS": "Proof of address / LC letter", "INCOME": "Proof of income", "AGREEMENT": "Loan agreement",
    "OTHER": "Other document",
}
MEDIA_CATEGORIES = {
    "ID_FRONT": "National ID card (front)", "ID_BACK": "National ID card (back)",
    "DRIVE_PERMIT": "Driving permit", "PASSPORT": "Passport",
    "CLIENT_PHOTO": "Client photo (passport-size)", "ASSET_PHOTO": "Security / collateral asset photo",
    "OTHER": "Other document",
}
IDENTITY_CATEGORIES = ("ID_FRONT", "DRIVE_PERMIT", "PASSPORT")  # any one of these proves who the client is
COLLATERAL_STATUS = {"HELD": "Held", "RELEASED": "Released", "SEIZED": "Seized / sold"}
FEE_TYPES = ("NONE", "FLAT", "PERCENT")


class BorrowerDocument(db.Model):
    __tablename__ = "borrower_documents"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    borrower_id = db.Column(db.Integer, db.ForeignKey("borrowers.id"), nullable=False, index=True)
    doc_type = db.Column(db.String(12), nullable=False, default="OTHER")
    file_id = db.Column(db.Integer, db.ForeignKey("stored_files.id"), nullable=False)
    note = db.Column(db.String(200))
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    file = db.relationship("StoredFile")

    __table_args__ = (CheckConstraint(
        "doc_type IN ('PHOTO','ID_FRONT','ID_BACK','ADDRESS','INCOME','AGREEMENT','OTHER')", name="ck_bdoc_type"),)


class Guarantor(db.Model):
    __tablename__ = "guarantors"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    loan_id = db.Column(db.Integer, db.ForeignKey("loans.id"), nullable=False, index=True)
    name = db.Column(db.String(150), nullable=False)
    phone = db.Column(db.String(30))
    id_number = db.Column(db.String(60))
    relationship = db.Column(db.String(60))
    address = db.Column(db.String(250))
    occupation = db.Column(db.String(120))
    file_id = db.Column(db.Integer, db.ForeignKey("stored_files.id"))  # ID scan / signed form
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    file = db.relationship("StoredFile")
    loan = db.relationship("Loan", backref=db.backref("guarantors", order_by="Guarantor.id"))


class Collateral(db.Model):
    __tablename__ = "collaterals"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    loan_id = db.Column(db.Integer, db.ForeignKey("loans.id"), nullable=False, index=True)
    kind = db.Column(db.String(60), nullable=False)  # e.g. Land title, Vehicle logbook, Electronics
    description = db.Column(db.String(300))
    estimated_value = db.Column(db.BigInteger, nullable=False, default=0)
    status = db.Column(db.String(10), nullable=False, default="HELD")
    file_id = db.Column(db.Integer, db.ForeignKey("stored_files.id"))
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    file = db.relationship("StoredFile")
    loan = db.relationship("Loan", backref=db.backref("collaterals", order_by="Collateral.id"))

    __table_args__ = (CheckConstraint("status IN ('HELD','RELEASED','SEIZED')", name="ck_collateral_status"),
                      CheckConstraint("estimated_value >= 0", name="ck_collateral_value"))


class LoanProduct(db.Model):
    """A saved set of loan terms, fees and penalty rules. Loans copy these at
    creation, so editing a product never changes an existing loan."""
    __tablename__ = "loan_products"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(300))
    method = db.Column(db.String(10), nullable=False, default="REDUCING")
    rate_bp = db.Column(db.Integer, nullable=False)
    rate_period = db.Column(db.String(10), nullable=False, default="MONTH")
    duration = db.Column(db.Integer, nullable=False, default=3)
    duration_unit = db.Column(db.String(10), nullable=False, default="MONTH")
    frequency = db.Column(db.String(10), nullable=False, default="MONTHLY")
    min_amount = db.Column(db.BigInteger, nullable=False, default=0)
    max_amount = db.Column(db.BigInteger, nullable=False, default=0)  # 0 = no maximum
    processing_fee_type = db.Column(db.String(8), nullable=False, default="NONE")
    processing_fee_value = db.Column(db.BigInteger, nullable=False, default=0)  # minor units, or bp for PERCENT
    insurance_fee_type = db.Column(db.String(8), nullable=False, default="NONE")
    insurance_fee_value = db.Column(db.BigInteger, nullable=False, default=0)
    penalty_type = db.Column(db.String(8), nullable=False, default="NONE")
    penalty_value = db.Column(db.BigInteger, nullable=False, default=0)
    penalty_grace_days = db.Column(db.Integer, nullable=False, default=0)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("group_id", "name", name="uq_product_name"),
        CheckConstraint("method IN ('FLAT','REDUCING')", name="ck_product_method"),
        CheckConstraint("processing_fee_type IN ('NONE','FLAT','PERCENT')", name="ck_product_pf"),
        CheckConstraint("insurance_fee_type IN ('NONE','FLAT','PERCENT')", name="ck_product_if"),
        CheckConstraint("penalty_type IN ('NONE','FLAT','PERCENT')", name="ck_product_penalty"),
    )


class LoanPenalty(db.Model):
    """One late-payment penalty per overdue instalment (never compounding)."""
    __tablename__ = "loan_penalties"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    loan_id = db.Column(db.Integer, db.ForeignKey("loans.id"), nullable=False, index=True)
    schedule_id = db.Column(db.Integer, db.ForeignKey("loan_repayment_schedules.id"), nullable=False)
    amount = db.Column(db.BigInteger, nullable=False)
    paid = db.Column(db.BigInteger, nullable=False, default=0)
    waived = db.Column(db.BigInteger, nullable=False, default=0)
    waive_reason = db.Column(db.String(300))
    assessed_on = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    loan = db.relationship("Loan", backref=db.backref("penalties", order_by="LoanPenalty.id"))

    @property
    def balance(self):
        return self.amount - self.paid - self.waived

    __table_args__ = (UniqueConstraint("schedule_id", name="uq_penalty_schedule"),
                      CheckConstraint("paid + waived <= amount", name="ck_penalty_bounds"))


# ------------------------------------------------------------------ phase 2: collections, applications
FOLLOWUP_KINDS = {"CALL": "Phone call", "VISIT": "Field visit", "SMS": "SMS / message", "OTHER": "Other"}
APPLICATION_STATUS = {"SUBMITTED": "Submitted", "UNDER_REVIEW": "Under review", "APPROVED": "Approved",
                      "REJECTED": "Rejected", "CONVERTED": "Loan created"}


class FollowUp(db.Model):
    """A call, visit or message to a borrower about a loan, with an optional promise to pay."""
    __tablename__ = "follow_ups"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    loan_id = db.Column(db.Integer, db.ForeignKey("loans.id"), nullable=False, index=True)
    borrower_id = db.Column(db.Integer, db.ForeignKey("borrowers.id"), nullable=False)
    kind = db.Column(db.String(8), nullable=False, default="CALL")
    note = db.Column(db.String(500), nullable=False)
    promise_date = db.Column(db.Date)
    promise_amount = db.Column(db.BigInteger)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    author = db.relationship("User", foreign_keys=[created_by])
    loan = db.relationship("Loan", backref=db.backref("follow_ups", order_by="FollowUp.id.desc()"))

    __table_args__ = (CheckConstraint("kind IN ('CALL','VISIT','SMS','OTHER')", name="ck_followup_kind"),)


class LoanApplication(db.Model):
    __tablename__ = "loan_applications"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    borrower_id = db.Column(db.Integer, db.ForeignKey("borrowers.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("loan_products.id"))
    amount = db.Column(db.BigInteger, nullable=False)
    purpose = db.Column(db.String(300))
    status = db.Column(db.String(12), nullable=False, default="SUBMITTED")
    officer_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    # appraisal
    monthly_income = db.Column(db.BigInteger)
    monthly_expenses = db.Column(db.BigInteger)
    appraisal_notes = db.Column(db.Text)
    recommended_amount = db.Column(db.BigInteger)
    decision_reason = db.Column(db.String(300))
    decided_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    decided_at = db.Column(db.DateTime)
    loan_id = db.Column(db.Integer, db.ForeignKey("loans.id"))
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    borrower = db.relationship("Borrower")
    product = db.relationship("LoanProduct")
    officer = db.relationship("User", foreign_keys=[officer_id])

    __table_args__ = (
        CheckConstraint("status IN ('SUBMITTED','UNDER_REVIEW','APPROVED','REJECTED','CONVERTED')",
                        name="ck_application_status"),
        CheckConstraint("amount > 0", name="ck_application_amount"),
    )


# ------------------------------------------------------------------ phase 3: capital
class Investor(db.Model):
    __tablename__ = "investors"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    name = db.Column(db.String(150), nullable=False)
    phone = db.Column(db.String(30))
    notes = db.Column(db.String(300))
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class CapitalEntry(db.Model):
    """Money the owner or an investor puts into the business, or takes out."""
    __tablename__ = "capital_entries"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    investor_id = db.Column(db.Integer, db.ForeignKey("investors.id"))  # null = the owner
    kind = db.Column(db.String(12), nullable=False)  # CAPITAL_IN, WITHDRAWAL
    amount = db.Column(db.BigInteger, nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    note = db.Column(db.String(300))
    tx_id = db.Column(db.Integer, db.ForeignKey("transactions.id"))
    reversed = db.Column(db.Boolean, nullable=False, default=False)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    investor = db.relationship("Investor")

    __table_args__ = (CheckConstraint("kind IN ('CAPITAL_IN','WITHDRAWAL')", name="ck_capital_kind"),
                      CheckConstraint("amount > 0", name="ck_capital_amount"))


class ClientMedia(db.Model):
    """Pictures and scans a lender keeps about a client: ID card, driving permit, passport, the client's photo,
    and photos of the asset held as security. Files live at ImageKit; only the reference is stored here."""
    __tablename__ = "client_media"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    borrower_id = db.Column(db.Integer, db.ForeignKey("borrowers.id"), nullable=False, index=True)
    loan_id = db.Column(db.Integer, db.ForeignKey("loans.id"), index=True)          # set for asset photos of a loan
    collateral_id = db.Column(db.Integer, db.ForeignKey("collaterals.id"))           # optional: which asset
    category = db.Column(db.String(20), nullable=False)
    file_id = db.Column(db.Integer, db.ForeignKey("stored_files.id"), nullable=False)
    caption = db.Column(db.String(200))
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    file = db.relationship("StoredFile")
