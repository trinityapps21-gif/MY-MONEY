"""Platform, identity and tenant models."""
from datetime import datetime, timezone

from flask_login import UserMixin
from sqlalchemy import CheckConstraint, UniqueConstraint

from app.extensions import db


def utcnow() -> datetime:
    """Naive UTC timestamp (all timestamps are stored in UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


OFFICIAL_ROLES = ("ADMIN", "TREASURER", "VICE_CHAIR")
ROLE_LABELS = {
    "ADMIN": "Group Administrator",
    "TREASURER": "Treasurer",
    "VICE_CHAIR": "Vice Chairperson",
    "MEMBER": "Member",
}


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(30), unique=True, nullable=False)
    email = db.Column(db.String(255), unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_platform_admin = db.Column(db.Boolean, nullable=False, default=False)
    status = db.Column(db.String(20), nullable=False, default="ACTIVE")
    avatar_file_id = db.Column(db.Integer, db.ForeignKey("stored_files.id", use_alter=True, name="fk_user_avatar"))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_login_at = db.Column(db.DateTime)
    # Single-use account recovery code (only a hash is stored; the code is shown once at first sign-in).
    recovery_code_hash = db.Column(db.String(255))
    recovery_code_created_at = db.Column(db.DateTime)

    __table_args__ = (CheckConstraint("status IN ('ACTIVE','SUSPENDED')", name="ck_user_status"),)

    @property
    def is_active(self):  # Flask-Login
        return self.status == "ACTIVE"

    @property
    def initials(self):
        parts = [p for p in self.full_name.split() if p]
        return ("".join(p[0] for p in parts[:2]) or "?").upper()

    @property
    def first_name(self):
        return self.full_name.split()[0] if self.full_name else ""


class PasswordResetRequest(db.Model):
    """A member asked for help with a forgotten password. A group official checks
    that it is really that person, then sends a reset link (or rejects the request).
    The reset link is only valid for an APPROVED request."""
    __tablename__ = "password_reset_requests"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    status = db.Column(db.String(12), nullable=False, default="PENDING")  # PENDING/APPROVED/REJECTED/COMPLETED
    note = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    decided_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    decided_group_id = db.Column(db.Integer, db.ForeignKey("groups.id"))
    decided_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)

    user = db.relationship("User", foreign_keys=[user_id])
    decider = db.relationship("User", foreign_keys=[decided_by])

    __table_args__ = (
        CheckConstraint("status IN ('PENDING','APPROVED','REJECTED','COMPLETED')", name="ck_reset_status"),
    )


class ImportBatch(db.Model):
    """One uploaded file of existing records (CSV / Excel / PDF). Rows are validated
    into a preview first; nothing touches the ledger until an official confirms."""
    __tablename__ = "import_batches"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    kind = db.Column(db.String(20), nullable=False)  # members / contributions / loans / repayments
    filename = db.Column(db.String(200), nullable=False)
    source_file_id = db.Column(db.Integer, db.ForeignKey("stored_files.id"))
    status = db.Column(db.String(12), nullable=False, default="PREVIEW")  # PREVIEW/APPLIED/UNDONE/CANCELLED
    total_rows = db.Column(db.Integer, nullable=False, default=0)
    ok_rows = db.Column(db.Integer, nullable=False, default=0)
    error_rows = db.Column(db.Integer, nullable=False, default=0)
    applied_rows = db.Column(db.Integer, nullable=False, default=0)
    rows_json = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    applied_at = db.Column(db.DateTime)
    undone_at = db.Column(db.DateTime)
    undone_by = db.Column(db.Integer, db.ForeignKey("users.id"))

    creator = db.relationship("User", foreign_keys=[created_by])
    source_file = db.relationship("StoredFile", foreign_keys=[source_file_id])

    __table_args__ = (
        CheckConstraint("status IN ('PREVIEW','APPLIED','UNDONE','CANCELLED')", name="ck_import_status"),
        CheckConstraint("kind IN ('members','contributions','loans','repayments')", name="ck_import_kind"),
    )


class ImportItem(db.Model):
    """Records created by an import, so the whole batch can be undone."""
    __tablename__ = "import_items"
    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("import_batches.id"), nullable=False, index=True)
    entity_type = db.Column(db.String(20), nullable=False)  # member / contribution / loan / repayment / borrower
    entity_id = db.Column(db.Integer, nullable=False)
    detail = db.Column(db.String(100))


class GroupRole(db.Model):
    __tablename__ = "group_roles"
    code = db.Column(db.String(20), primary_key=True)
    name = db.Column(db.String(60), nullable=False)
    is_official = db.Column(db.Boolean, nullable=False, default=False)
    description = db.Column(db.String(255))


class Group(db.Model):
    __tablename__ = "groups"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    location = db.Column(db.String(120))
    phone = db.Column(db.String(30))
    email = db.Column(db.String(255))
    registration_number = db.Column(db.String(60))
    currency = db.Column(db.String(3), nullable=False, default="UGX")
    currency_decimals = db.Column(db.Integer, nullable=False, default=0)
    contribution_frequency = db.Column(db.String(10), nullable=False, default="MONTHLY")
    default_contribution = db.Column(db.BigInteger, nullable=False, default=0)
    timezone = db.Column(db.String(60), nullable=False, default="Africa/Kampala")
    logo_file_id = db.Column(db.Integer, db.ForeignKey("stored_files.id", use_alter=True, name="fk_group_logo"))
    status = db.Column(db.String(20), nullable=False, default="ACTIVE")
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    loan_approvals_required = db.Column(db.Integer, nullable=False, default=1)
    # Business type is chosen once at creation and never changes: SAVINGS_GROUP or LENDING.
    business_type = db.Column(db.String(20), nullable=False, default="SAVINGS_GROUP", server_default="SAVINGS_GROUP")
    require_client_media = db.Column(db.Boolean, nullable=False, default=False, server_default="0")  # lenders: ID + photos before disbursing
    rate_cap_bp = db.Column(db.Integer)  # optional own cap, % per month x100 (stricter than the platform cap)
    penalty_type = db.Column(db.String(10), nullable=False, default="NONE", server_default="NONE")
    penalty_value = db.Column(db.BigInteger, nullable=False, default=0, server_default="0")
    penalty_grace_days = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    pay_instructions = db.Column(db.String(400))  # shown to borrowers: where to send mobile money, bank details...
    feed_visible_types = db.Column(
        db.String(300),
        nullable=False,
        default="CONTRIBUTION,LOAN_APPROVED,LOAN_DISBURSED,REPAYMENT,LOAN_REPAID,MEMBER_APPROVED,ANNOUNCEMENT,MILESTONE",
    )
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    owner = db.relationship("User", foreign_keys=[owner_id])

    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE','SUSPENDED')", name="ck_group_status"),
        CheckConstraint("contribution_frequency IN ('WEEKLY','MONTHLY','CUSTOM')", name="ck_group_freq"),
        CheckConstraint("loan_approvals_required BETWEEN 1 AND 3", name="ck_group_approvals"),
        CheckConstraint("default_contribution >= 0", name="ck_group_contrib"),
        CheckConstraint("business_type IN ('SAVINGS_GROUP','LENDING')", name="ck_group_btype"),
        CheckConstraint("penalty_type IN ('NONE','FLAT','PERCENT')", name="ck_group_penalty"),
    )

    @property
    def is_lender(self):
        return self.business_type == "LENDING"

    @property
    def visible_types(self):
        return {t for t in (self.feed_visible_types or "").split(",") if t}


class GroupMember(db.Model):
    __tablename__ = "group_members"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    role = db.Column(db.String(20), db.ForeignKey("group_roles.code"), nullable=False, default="MEMBER")
    status = db.Column(db.String(20), nullable=False, default="PENDING")
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    joined_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    group = db.relationship("Group", backref="memberships")
    user = db.relationship("User", foreign_keys=[user_id], backref="memberships")

    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="uq_group_user"),
        CheckConstraint("status IN ('PENDING','ACTIVE','REJECTED','SUSPENDED')", name="ck_member_status"),
        db.Index("ix_member_group_status", "group_id", "status"),
    )

    @property
    def is_official(self):
        return self.status == "ACTIVE" and self.role in OFFICIAL_ROLES

    @property
    def role_label(self):
        return ROLE_LABELS.get(self.role, self.role)


class GroupInvitation(db.Model):
    __tablename__ = "group_invitations"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    token = db.Column(db.String(80), unique=True, nullable=False)
    label = db.Column(db.String(80))
    expires_at = db.Column(db.DateTime)
    revoked_at = db.Column(db.DateTime)
    max_uses = db.Column(db.Integer)
    use_count = db.Column(db.Integer, nullable=False, default=0)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    group = db.relationship("Group")

    @property
    def state(self):
        if self.revoked_at:
            return "REVOKED"
        if self.expires_at and self.expires_at < utcnow():
            return "EXPIRED"
        if self.max_uses and self.use_count >= self.max_uses:
            return "USED UP"
        return "ACTIVE"


class MemberApplication(db.Model):
    __tablename__ = "member_applications"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    invitation_id = db.Column(db.Integer, db.ForeignKey("group_invitations.id"))
    status = db.Column(db.String(20), nullable=False, default="PENDING")
    decided_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    decided_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    user = db.relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        CheckConstraint("status IN ('PENDING','APPROVED','REJECTED')", name="ck_app_status"),
    )


class InterestSettings(db.Model):
    """Single-row platform interest policy. Changes apply to NEW loans only;
    every loan stores its own agreed terms."""
    __tablename__ = "interest_settings"
    id = db.Column(db.Integer, primary_key=True)
    default_rate_bp = db.Column(db.Integer, nullable=False, default=1000)  # percent x 100 (10.00% = 1000)
    min_rate_bp = db.Column(db.Integer, nullable=False, default=500)
    max_rate_bp = db.Column(db.Integer, nullable=False, default=2000)
    flat_enabled = db.Column(db.Boolean, nullable=False, default=True)
    reducing_enabled = db.Column(db.Boolean, nullable=False, default=True)
    default_period = db.Column(db.String(10), nullable=False, default="MONTH")
    allow_custom_rates = db.Column(db.Boolean, nullable=False, default=True)
    lender_cap_bp = db.Column(db.Integer, default=280, server_default="280")  # % per month x100; NULL = no cap
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        CheckConstraint("min_rate_bp <= max_rate_bp", name="ck_interest_range"),
        CheckConstraint("default_period IN ('WEEK','MONTH','YEAR')", name="ck_interest_period"),
    )


class PlatformSetting(db.Model):
    __tablename__ = "platform_settings"
    key = db.Column(db.String(60), primary_key=True)
    value = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class Notification(db.Model):
    __tablename__ = "notifications"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), index=True)
    kind = db.Column(db.String(40), nullable=False)
    title = db.Column(db.String(160), nullable=False)
    body = db.Column(db.String(400))
    link = db.Column(db.String(300))
    is_read = db.Column(db.Boolean, nullable=False, default=False)
    # Prevents duplicate notifications for the same event (e.g. one like per user per post)
    dedupe_key = db.Column(db.String(120), index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    # Reserved for future SMS / WhatsApp delivery integrations.
    delivery_channel = db.Column(db.String(20), nullable=False, default="IN_APP")
    delivery_status = db.Column(db.String(20), nullable=False, default="DELIVERED")

    group = db.relationship("Group")


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    action = db.Column(db.String(80), nullable=False, index=True)
    entity_type = db.Column(db.String(40))
    entity_id = db.Column(db.Integer)
    details = db.Column(db.Text)
    ip_address = db.Column(db.String(60))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    user = db.relationship("User")
    group = db.relationship("Group")


class StoredFile(db.Model):
    """Metadata for every uploaded file. The bytes live at the external provider
    (Uploadcare for images, ImageKit for documents); nothing is kept on the
    application filesystem. Rows are soft-deleted so history stays auditable."""
    __tablename__ = "stored_files"
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id", use_alter=True, name="fk_file_group"), index=True)
    kind = db.Column(db.String(24), nullable=False)  # logo|avatar|post_image|post_document|announcement_image|...
    provider = db.Column(db.String(12), nullable=False)  # uploadcare | imagekit
    provider_file_id = db.Column(db.String(120), nullable=False)
    file_url = db.Column(db.String(500))  # storage reference (never rendered to users)
    filename = db.Column(db.String(200), nullable=False)  # original filename
    extension = db.Column(db.String(10))
    content_type = db.Column(db.String(100), nullable=False)
    size = db.Column(db.Integer, nullable=False)
    checksum = db.Column(db.String(64), index=True)
    related_type = db.Column(db.String(30))
    related_id = db.Column(db.Integer)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id", use_alter=True, name="fk_file_creator"))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    deleted_at = db.Column(db.DateTime)
    deleted_by = db.Column(db.Integer)

    __table_args__ = (
        CheckConstraint("provider IN ('uploadcare','imagekit')", name="ck_file_provider"),
        db.Index("ix_file_related", "related_type", "related_id"),
    )

    @property
    def is_image(self):
        return self.content_type.startswith("image/")


class WebAuthnCredential(db.Model):
    """A fingerprint / face / device-unlock credential (passkey) a user enrolled on one phone.
    Only the PUBLIC key is stored; the private key never leaves the phone."""
    __tablename__ = "webauthn_credentials"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    credential_id = db.Column(db.String(512), nullable=False, unique=True)  # base64url
    public_key = db.Column(db.Text, nullable=False)  # base64url COSE key
    sign_count = db.Column(db.BigInteger, nullable=False, default=0)
    label = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_used_at = db.Column(db.DateTime)

    user = db.relationship("User", backref=db.backref("webauthn_credentials", lazy="select"))
