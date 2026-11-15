from flask import Blueprint, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models import GroupMember, Notification

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
