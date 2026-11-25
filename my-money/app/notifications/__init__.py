from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models import GroupMember, Notification, PushSubscription
from app.services import webpush

bp = Blueprint("notifications", __name__, url_prefix="/notifications")


def _accessible(n: Notification) -> bool:
    """A notification link is only followed while the user still belongs to that group."""
    if not n.group_id:
        return True
    return GroupMember.query.filter_by(group_id=n.group_id, user_id=current_user.id, status="ACTIVE").first() is not None


@bp.route("/")
@login_required
def index():
    q = Notification.query.filter_by(user_id=current_user.id)
    if request.args.get("unread") == "1":
        q = q.filter_by(is_read=False)
    page = q.order_by(Notification.created_at.desc(), Notification.id.desc()).paginate(
        page=request.args.get("page", 1, type=int), per_page=20, error_out=False)
    return render_template("notifications/index.html", page=page, unread_only=request.args.get("unread") == "1")


@bp.route("/<int:nid>/open")
@login_required
def open_(nid):
    n = Notification.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    n.is_read = True
    db.session.commit()
    if n.link and _accessible(n) and n.link.startswith("/") and not n.link.startswith("//"):
        return redirect(n.link)
    return redirect(url_for("notifications.index"))


@bp.route("/<int:nid>/read", methods=["POST"])
@login_required
def mark_read(nid):
    n = Notification.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    n.is_read = True
    db.session.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True, unread=Notification.query.filter_by(user_id=current_user.id, is_read=False).count())
    return redirect(request.referrer or url_for("notifications.index"))


@bp.route("/read-all", methods=["POST"])
@login_required
def read_all():
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update({"is_read": True})
    db.session.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True, unread=0)
    return redirect(request.referrer or url_for("notifications.index"))


@bp.route("/recent")
@login_required
def recent():
    """Data for the bell dropdown."""
    rows = Notification.query.filter_by(user_id=current_user.id).order_by(
        Notification.created_at.desc(), Notification.id.desc()).limit(8).all()
    return jsonify(
        unread=Notification.query.filter_by(user_id=current_user.id, is_read=False).count(),
        items=[{"id": n.id, "title": n.title, "body": n.body, "read": n.is_read,
                "url": url_for("notifications.open_", nid=n.id)} for n in rows])


# ----------------------------------------------------------------- pop-up (push) notifications
def _json():
    return request.get_json(silent=True) or request.form.to_dict() or {}


@bp.route("/push/config")
@login_required
def push_config():
    return jsonify(enabled=True, key=webpush.public_key(current_app.config.get("SECRET_KEY", "")),
                   devices=PushSubscription.query.filter_by(user_id=current_user.id).count())


@bp.route("/push/subscribe", methods=["POST"])
@login_required
def push_subscribe():
    d = _json()
    keys = d.get("keys") or {}
    endpoint, p256dh, auth = d.get("endpoint", ""), keys.get("p256dh", ""), keys.get("auth", "")
    try:
        ok = webpush.allowed_endpoint(endpoint) and len(webpush.b64d(p256dh)) == 65 and len(webpush.b64d(auth)) == 16
    except Exception:                                          # noqa: BLE001
        ok = False
    if not ok:
        return jsonify(ok=False, error="This device sent an invalid notification address."), 400
    sub = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if sub:                                                    # same phone/laptop, possibly a different person logging in
        sub.user_id, sub.p256dh, sub.auth = current_user.id, p256dh, auth
    else:
        db.session.add(PushSubscription(user_id=current_user.id, endpoint=endpoint, p256dh=p256dh, auth=auth,
                                        user_agent=(request.headers.get("User-Agent") or "")[:200]))
        db.session.flush()
        extra = PushSubscription.query.filter_by(user_id=current_user.id).order_by(PushSubscription.id.desc()).offset(
            webpush.MAX_SUBSCRIPTIONS_PER_USER).all()
        for old in extra:
            db.session.delete(old)
    db.session.commit()
    return jsonify(ok=True)


@bp.route("/push/unsubscribe", methods=["POST"])
@login_required
def push_unsubscribe():
    endpoint = _json().get("endpoint", "")
    if endpoint:
        PushSubscription.query.filter_by(endpoint=endpoint, user_id=current_user.id).delete()
        db.session.commit()
    return jsonify(ok=True)


@bp.route("/push/test", methods=["POST"])
@login_required
def push_test():
    """Sends a real pop-up to every device this user enabled, so they can see it works."""
    subs = PushSubscription.query.filter_by(user_id=current_user.id).all()
    msg = {"title": "Notifications are on", "body": "You will now get pop-up alerts from MY MONEY on this device.",
           "url": url_for("notifications.index"), "tag": "test"}
    sent, dead = 0, []
    for sb in subs:
        status = webpush.send(sb.endpoint, sb.p256dh, sb.auth, msg, subject=current_app.config.get("VAPID_SUBJECT")
                              or "mailto:admin@example.com", secret_key=current_app.config.get("SECRET_KEY", ""))
        if 200 <= status < 300:
            sent += 1
        elif status in (404, 410):
            dead.append(sb)
    for sb in dead:
        db.session.delete(sb)
    db.session.commit()
    return jsonify(ok=True, sent=sent, devices=len(subs) - len(dead))
