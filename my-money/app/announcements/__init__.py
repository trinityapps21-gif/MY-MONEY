import json
from datetime import datetime, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db
from app.models import Announcement, AnnouncementHistory, StoredFile, utcnow
from app.services import feedlog, notify, storage
from app.services.audit import audit
from app.services.forms import form_text
from app.services.tenant import get_scoped_or_404, scoped, tenant_route
from app.services.timeutil import local_today

bp = Blueprint("announcements", __name__, url_prefix="/g/<int:group_id>/announcements")


def _snapshot(a):
    return json.dumps({"title": a.title, "content": a.content, "pinned": a.is_pinned,
                       "published": a.is_published, "status": a.status}, default=str)


def _history(a, action):
    db.session.add(AnnouncementHistory(group_id=g.group.id, announcement_id=a.id, action=action,
                                       snapshot=_snapshot(a), user_id=current_user.id))


def visible_query():
    return scoped(Announcement, status="ACTIVE", is_published=True).filter(Announcement.publish_date <= utcnow())


@bp.route("/")
@tenant_route()
def index(group_id=None):
    q = visible_query()
    if g.is_official and request.args.get("drafts") == "1":
        q = scoped(Announcement, status="ACTIVE").filter(
            (Announcement.is_published.is_(False)) | (Announcement.publish_date > utcnow()))
    page = q.order_by(Announcement.is_pinned.desc(), Announcement.publish_date.desc(), Announcement.id.desc()) \
        .paginate(page=request.args.get("page", 1, type=int), per_page=10, error_out=False)
    return render_template("announcements/index.html", page=page, drafts=request.args.get("drafts") == "1")


@bp.route("/history")
@tenant_route(official=True)
def history(group_id=None):
    """Previous announcements, including removed ones (audit record retained)."""
    items = scoped(Announcement).order_by(Announcement.created_at.desc()).limit(200).all()
    return render_template("announcements/history.html", items=items)


def _apply(a):
    title = (request.form.get("title") or "").strip()
    content = (request.form.get("content") or "").strip()
    if len(title) < 3:
        raise ValueError("Enter a title (at least 3 characters).")
    if not content:
        raise ValueError("Write the announcement content.")
    a.title, a.content = title[:160], content[:10000]
    a.is_published = request.form.get("is_published", "1") == "1"
    raw = (request.form.get("publish_date") or "").strip()
    if raw:
        try:
            day = datetime.fromisoformat(raw)
        except ValueError:
            raise ValueError("Enter a valid publication date.")
        a.publish_date = utcnow() if day.date() <= local_today(g.group.timezone) else day
    elif a.publish_date is None:
        a.publish_date = utcnow()
    a.is_pinned = request.form.get("is_pinned") == "1"
    f = request.files.get("attachment")
    if f and f.filename:
        try:
            sf = storage.store_upload(f, group_id=g.group.id, user_id=current_user.id, kind="announcement_file",
                                      related=("announcement", a.id))
        except storage.UploadError as e:
            raise ValueError(str(e))
        if a.attachment_file_id:
            storage.delete_file(db.session.get(StoredFile, a.attachment_file_id), current_user.id, "replaced")
        a.attachment_file_id = sf.id
    elif request.form.get("remove_attachment") == "1" and a.attachment_file_id:
        storage.delete_file(db.session.get(StoredFile, a.attachment_file_id), current_user.id, "removed")
        a.attachment_file_id = None


def _announce(a):
    if a.is_published and a.publish_date <= utcnow():
        feedlog.emit(g.group.id, "ANNOUNCEMENT", a.title, (a.content[:140] + ("..." if len(a.content) > 140 else "")),
                     None, current_user.id, "announcement", a.id)
        notify.notify_members(g.group.id, "ANNOUNCEMENT", "New announcement", a.title,
                              f"/g/{g.group.id}/announcements/", exclude=current_user.id)


@bp.route("/new", methods=["GET", "POST"])
@tenant_route(official=True)
def new(group_id=None):
    if request.method == "POST":
        a = Announcement(group_id=g.group.id, title="x", content="x", created_by=current_user.id,
                         publish_date=utcnow())
        try:
            _apply(a)
            db.session.add(a)
            db.session.flush()
            _history(a, "CREATED")
            _announce(a)
            audit("announcement.created", "announcement", a.id, group_id=g.group.id)
            db.session.commit()
            flash("Announcement saved.", "success")
            return redirect(url_for("announcements.index"))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("announcements/form.html", a=None)


@bp.route("/<int:ann_id>/edit", methods=["GET", "POST"])
@tenant_route(official=True)
def edit(ann_id, group_id=None):
    a = get_scoped_or_404(Announcement, ann_id)
    if a.status != "ACTIVE":
        abort(404)
    if request.method == "POST":
        was_visible = a.is_published and a.publish_date <= utcnow()
        try:
            _apply(a)
            _history(a, "EDITED")
            if not was_visible:
                _announce(a)
            audit("announcement.edited", "announcement", a.id, group_id=g.group.id)
            db.session.commit()
            flash("Announcement updated.", "success")
            return redirect(url_for("announcements.index"))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("announcements/form.html", a=a)


@bp.route("/<int:ann_id>/pin", methods=["POST"])
@tenant_route(official=True)
def pin(ann_id, group_id=None):
    a = get_scoped_or_404(Announcement, ann_id)
    a.is_pinned = request.form.get("pin") == "1"
    _history(a, "PINNED" if a.is_pinned else "UNPINNED")
    audit("announcement.pinned" if a.is_pinned else "announcement.unpinned", "announcement", a.id,
          group_id=g.group.id)
    db.session.commit()
    flash("Announcement pinned." if a.is_pinned else "Announcement unpinned.", "success")
    return redirect(request.referrer or url_for("announcements.index"))


@bp.route("/<int:ann_id>/remove", methods=["POST"])
@tenant_route(official=True)
def remove(ann_id, group_id=None):
    a = get_scoped_or_404(Announcement, ann_id)
    if a.status == "ACTIVE":
        a.status, a.removed_by, a.removed_at, a.is_pinned = "REMOVED", current_user.id, utcnow(), False
        _history(a, "REMOVED")
        audit("announcement.removed", "announcement", a.id, {"title": a.title}, group_id=g.group.id)
        db.session.commit()
    flash("Announcement removed. An audit record was kept.", "success")
    return redirect(url_for("announcements.index"))
