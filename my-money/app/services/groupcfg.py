"""Per-group settings (savings minimum, share price, fines, loan interest policy)."""
from flask import g, has_app_context

from app.extensions import db
from app.models import GroupSettings, InterestSettings


def get_settings(group_id) -> GroupSettings:
    """The group's settings row, created on first use. New rows copy the old
    platform-wide interest policy so existing groups keep working unchanged."""
    # One lookup per request (a dashboard used to fetch this same row 5 times, each a network trip to Turso).
    cache = g.setdefault("_group_settings", {}) if has_app_context() else {}
    s = cache.get(group_id)
    if s is not None and s in db.session:
        return s
    s = GroupSettings.query.filter_by(group_id=group_id).first()
    if s is None:
        s = GroupSettings(group_id=group_id)
        old = db.session.get(InterestSettings, 1)
        if old is not None:
            for f in ("default_rate_bp", "min_rate_bp", "max_rate_bp", "flat_enabled", "reducing_enabled",
                      "default_period", "allow_custom_rates"):
                setattr(s, f, getattr(old, f))
        db.session.add(s)
        db.session.flush()
    cache[group_id] = s
    return s
