from flask import current_app
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.extensions import db
from app.models import GroupMember, Notification, OFFICIAL_ROLES, PushSubscription


def notify(user_ids, group_id, kind, title, body=None, link=None, dedupe_key=None):
    """Create in-app notifications. With a dedupe_key, a user is never notified
    twice for the same event. Channel fields are reserved for SMS/WhatsApp."""
    from app.services import importctx
    if importctx.active():
        return 0
    created = 0
    fresh = []
    for uid in set(user_ids):
        if dedupe_key and Notification.query.filter_by(user_id=uid, dedupe_key=dedupe_key).first():
            continue
        n = Notification(user_id=uid, group_id=group_id, kind=kind, title=title[:160],
                         body=(body or "")[:400], link=link, dedupe_key=dedupe_key)
        db.session.add(n)
        fresh.append(n)
        created += 1
    _queue_push(fresh)
    return created


def _queue_push(notifications):
    """Remember who should get a pop-up; it is sent only once the notification is safely committed."""
    if not notifications:
        return
    try:
        db.session.flush()                                        # gives every notification its id
        subs = PushSubscription.query.filter(PushSubscription.user_id.in_([n.user_id for n in notifications])).all()
        if not subs:
            return
        by_user = {}
        for sb in subs:
            by_user.setdefault(sb.user_id, []).append((sb.id, sb.endpoint, sb.p256dh, sb.auth))
        queue = db.session.info.setdefault("push_queue", [])
        from app.services import webpush
        for n in notifications:
            if n.user_id in by_user:
                queue.append((by_user[n.user_id], webpush.payload_for(n)))
    except Exception:                                             # noqa: BLE001 - pop-ups are a bonus; never block the real action
        db.session.info.pop("push_queue", None)


@event.listens_for(Session, "after_commit")
def _send_queued_pushes(session):
    queue = session.info.pop("push_queue", None)
    if not queue:
        return
    try:
        from app.services import webpush
        app = current_app._get_current_object()
        for targets, message in queue:
            webpush.deliver_async(app, targets, message)
    except Exception:                                             # noqa: BLE001
        pass


@event.listens_for(Session, "after_rollback")
def _drop_queued_pushes(session):
    session.info.pop("push_queue", None)


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
