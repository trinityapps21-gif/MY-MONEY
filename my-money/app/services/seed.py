from app.extensions import db
from app.models import GroupRole, InterestSettings, PlatformSetting

ROLES = [
    ("ADMIN", "Group Administrator", True, "Full group management; group owner is the creator."),
    ("TREASURER", "Treasurer", True, "Equal day-to-day group management permissions."),
    ("VICE_CHAIR", "Vice Chairperson", True, "Equal day-to-day group management permissions."),
    ("MEMBER", "Member", False, "Read-only access to group transparency pages; community participation."),
]
PLATFORM_DEFAULTS = {
    "platform_name": "MY MONEY",
    "company_identity": "Developed and Powered by Emtrixz Technology Company.",
    "default_currency": "UGX",
    "maintenance_mode": "0",
    "allow_group_registration": "1",
    "contact_email": "",
    "session_hours": "12",
}


def seed_defaults():
    """Idempotent: roles, the single interest-settings row and platform settings."""
    for code, name, official, desc in ROLES:
        if db.session.get(GroupRole, code) is None:
            db.session.add(GroupRole(code=code, name=name, is_official=official, description=desc))
    if db.session.get(InterestSettings, 1) is None:
        db.session.add(InterestSettings(id=1))
    for k, v in PLATFORM_DEFAULTS.items():
        if db.session.get(PlatformSetting, k) is None:
            db.session.add(PlatformSetting(key=k, value=v))
    db.session.commit()


def get_setting(key, default=""):
    row = db.session.get(PlatformSetting, key)
    return row.value if row and row.value is not None else PLATFORM_DEFAULTS.get(key, default)


def set_setting(key, value):
    row = db.session.get(PlatformSetting, key)
    if row is None:
        db.session.add(PlatformSetting(key=key, value=str(value)))
    else:
        row.value = str(value)
