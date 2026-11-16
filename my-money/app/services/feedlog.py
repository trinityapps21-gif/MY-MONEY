"""Automatic Financial Activity Feed events. Descriptions deliberately never
include borrower names, phone numbers or identification details."""
from app.extensions import db
from app.models import GroupFeed

MILESTONES_MAJOR_UNITS = [1_000_000, 5_000_000, 10_000_000, 25_000_000, 50_000_000, 100_000_000,
                          250_000_000, 500_000_000, 1_000_000_000]


def emit(group_id, event_type, title, description=None, amount=None, actor_id=None, ref_type=None, ref_id=None):
    from app.services import importctx
    if importctx.active():  # imported history must not flood the feed
        return None
    row = GroupFeed(group_id=group_id, event_type=event_type, title=title[:160],
                    description=(description or "")[:300] or None, amount=amount, actor_id=actor_id,
                    ref_type=ref_type, ref_id=ref_id)
    db.session.add(row)
    return row


def check_savings_milestone(group, before_minor, after_minor, actor_id=None):
    scale = 10 ** group.currency_decimals
    for m in MILESTONES_MAJOR_UNITS:
        if before_minor < m * scale <= after_minor:
            emit(group.id, "MILESTONE", "Savings milestone reached",
                 f"Total group savings passed {group.currency} {m:,}.", m * scale, actor_id)
