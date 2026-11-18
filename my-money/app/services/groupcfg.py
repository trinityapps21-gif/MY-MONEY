"""Per-group settings (savings minimum, share price, fines, loan interest policy)."""
from app.extensions import db
from app.models import GroupSettings, InterestSettings


def get_settings(group_id) -> GroupSettings:
    """The group's settings row, created on first use. New rows copy the old
    platform-wide interest policy so existing groups keep working unchanged."""
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
    return s
