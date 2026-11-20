"""Authenticated file delivery. There are NO permanent public URLs for private
files: every request is checked against the caller's group membership, and
documents are delivered through short-lived signed provider URLs (or streamed
by Flask after the check). Group logos are the only publicly viewable files
(they appear on the public invitation page)."""
from flask import (Blueprint, Response, abort, flash, redirect, request, url_for)
from flask_login import current_user, login_required

from app.extensions import db
from app.models import Group, GroupMember, StoredFile
from app.services import storage
from app.services.audit import audit

bp = Blueprint("files", __name__)


def can_view(f: StoredFile) -> bool:
    if f.deleted_at:
        return False
    if f.kind == "logo":
        grp = db.session.get(Group, f.group_id) if f.group_id else None
        return bool(grp and grp.logo_file_id == f.id and grp.status == "ACTIVE")
    if not current_user.is_authenticated:
        return False
    if f.kind == "avatar":
        owner_id = f.related_id
        if owner_id == current_user.id:
            return True
        mine = db.session.query(GroupMember.group_id).filter_by(user_id=current_user.id, status="ACTIVE")
        return db.session.query(GroupMember.id).filter(
            GroupMember.user_id == owner_id, GroupMember.status == "ACTIVE", GroupMember.group_id.in_(mine)).first() \
            is not None
    if f.group_id is None:
        return False
    if f.kind in ("import", "kyc"):  # KYC scans and source records of imports hold private financial data: officials only
        m = GroupMember.query.filter_by(group_id=f.group_id, user_id=current_user.id, status="ACTIVE").first()
        return bool(m and m.is_official)
    return GroupMember.query.filter_by(group_id=f.group_id, user_id=current_user.id, status="ACTIVE").first() \
        is not None


@bp.route("/files/<int:file_id>")
def serve(file_id):
    f = db.session.get(StoredFile, file_id)
    if f is None or not can_view(f):
        abort(404)
    download = request.args.get("download") == "1"
    try:
        if f.is_image and f.provider == "imagekit" and not download:
            # Let the browser load the picture straight from ImageKit's fast CDN through a short-lived signed link
            # (the access check above already passed), instead of this server downloading and re-sending every image.
            link = storage.signed_url(f, seconds=900)
            if link:
                resp = redirect(link)
                resp.headers["Cache-Control"] = "private, max-age=300" if f.kind != "logo" else "public, max-age=300"
                return resp
        if f.is_image:
            data = storage.read_bytes(f)
            resp = Response(data, mimetype=f.content_type)
            resp.headers["Cache-Control"] = "private, max-age=3600" if f.kind != "logo" else "public, max-age=600"
            resp.headers["X-Content-Type-Options"] = "nosniff"
            if download:
                resp.headers["Content-Disposition"] = f'attachment; filename="{f.filename}"'
            return resp
        if download:
            data = storage.read_bytes(f)
            audit("file.downloaded", "stored_file", f.id, group_id=f.group_id)
            db.session.commit()
            resp = Response(data, mimetype=f.content_type)
            resp.headers["Content-Disposition"] = f'attachment; filename="{f.filename}"'
            resp.headers["Cache-Control"] = "private, no-store"
            return resp
        url = storage.signed_url(f)
        if url:
            resp = redirect(url)
        else:
            resp = Response(storage.read_bytes(f), mimetype=f.content_type)
            resp.headers["Content-Disposition"] = f'inline; filename="{f.filename}"'
        resp.headers["Cache-Control"] = "private, no-store"
        return resp
    except storage.UploadError:
        abort(404)


@bp.route("/profile/avatar", methods=["POST"])
@login_required
def upload_avatar():
    f = request.files.get("avatar")
    if not f or not f.filename:
        flash("Choose an image to upload.", "error")
        return redirect(url_for("groups.profile"))
    try:
        old = db.session.get(StoredFile, current_user.avatar_file_id) if current_user.avatar_file_id else None
        new = storage.store_upload(f, group_id=None, user_id=current_user.id, kind="avatar", images_only=True,
                                   related=("user", current_user.id), square=True, max_side=256, max_bytes=2 * 1024 * 1024)
        current_user.avatar_file_id = new.id
        db.session.flush()
        if old:
            storage.delete_file(old, current_user.id, reason="replaced")
        db.session.commit()
        flash("Profile picture updated.", "success")
    except storage.UploadError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("groups.profile"))


@bp.route("/profile/avatar/remove", methods=["POST"])
@login_required
def remove_avatar():
    if current_user.avatar_file_id:
        old = db.session.get(StoredFile, current_user.avatar_file_id)
        current_user.avatar_file_id = None
        db.session.flush()
        storage.delete_file(old, current_user.id, reason="avatar removed")
        db.session.commit()
    flash("Profile picture removed.", "success")
    return redirect(url_for("groups.profile"))
