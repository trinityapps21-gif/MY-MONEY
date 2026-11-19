import time

from flask import current_app

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
    clear_settings_cache()


SETTINGS_TTL = 30  # seconds; platform settings rarely change, and every page asks for them


def _settings_cache():
    """Per-app, per-process cache of ALL platform settings (one query instead of one per lookup per page)."""
    store = current_app.extensions.setdefault("mm_settings_cache", {"at": 0.0, "data": None})
    if store["data"] is None or time.monotonic() - store["at"] > SETTINGS_TTL:
        store["data"] = {r.key: r.value for r in db.session.query(PlatformSetting).all()}
        store["at"] = time.monotonic()
    return store


def get_setting(key, default=""):
    value = _settings_cache()["data"].get(key)
    return value if value is not None else PLATFORM_DEFAULTS.get(key, default)


def clear_settings_cache():
    current_app.extensions.pop("mm_settings_cache", None)


def set_setting(key, value):
    row = db.session.get(PlatformSetting, key)
    if row is None:
        db.session.add(PlatformSetting(key=key, value=str(value)))
    else:
        row.value = str(value)
    clear_settings_cache()
