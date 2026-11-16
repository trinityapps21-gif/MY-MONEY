from app.extensions import db
from app.models import GroupMember, Notification, OFFICIAL_ROLES


def notify(user_ids, group_id, kind, title, body=None, link=None, dedupe_key=None):
    """Create in-app notifications. With a dedupe_key, a user is never notified
    twice for the same event. Channel fields are reserved for SMS/WhatsApp."""
    from app.services import importctx
    if importctx.active():
        return 0
    created = 0
    for uid in set(user_ids):
        if dedupe_key and Notification.query.filter_by(user_id=uid, dedupe_key=dedupe_key).first():
            continue
        db.session.add(
            Notification(user_id=uid, group_id=group_id, kind=kind, title=title[:160],
                         body=(body or "")[:400], link=link, dedupe_key=dedupe_key)
        )
        created += 1
    return created


def official_ids(group_id, exclude=None):
    rows = GroupMember.query.filter(
        GroupMember.group_id == group_id, GroupMember.status == "ACTIVE", GroupMember.role.in_(OFFICIAL_ROLES)
    ).all()
    return [m.user_id for m in rows if m.user_id != exclude]


def member_ids(group_id, exclude=None):
    rows = GroupMember.query.filter_by(group_id=group_id, status="ACTIVE").all()
    return [m.user_id for m in rows if m.user_id != exclude]


def notify_officials(group_id, kind, title, body=None, link=None, exclude=None):
    return notify(official_ids(group_id, exclude), group_id, kind, title, body, link)


def notify_members(group_id, kind, title, body=None, link=None, exclude=None):
    return notify(member_ids(group_id, exclude), group_id, kind, title, body, link)
