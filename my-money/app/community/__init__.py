"""Community Social Feed - a private, per-group social space.

Every object (post, like, comment, reply, share, report, attachment) carries the
group_id, and every lookup goes through ``scoped`` which filters by the group the
user was verified to belong to by ``tenant_route`` - the group id in the URL is
never trusted on its own. Nothing here can be reached by someone who is not an
ACTIVE member of that group (404), and officials get moderation powers only.

Threading: comments have one level of replies (a reply to a reply stays on the
same comment and records who it answers) to keep discussions readable.
"""
from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (POST_CATEGORIES, REPORT_REASONS, FeedAttachment, FeedCommentReply, FeedModerationLog,
                        FeedPost, FeedPostComment, FeedPostLike, FeedPostShare, FeedReport, GroupMember, StoredFile,
                        User, utcnow)
from app.services import notify, storage
from app.services.audit import audit
from app.services.tenant import scoped, tenant_route

bp = Blueprint("community", __name__, url_prefix="/g/<int:group_id>/community")

POSTS_PER_PAGE = 8
COMMENTS_PER_PAGE = 5
MAX_ATTACHMENTS = 6
MAX_POST_CHARS = 5000
MAX_COMMENT_CHARS = 2000


# ------------------------------------------------------------------- helpers
def wants_json():
    return request.headers.get("X-Requested-With") == "fetch" or request.accept_mimetypes.best == "application/json"


def fail(message, code=400):
    if wants_json():
        return jsonify(error=message), code
    flash(message, "error")
    return redirect(request.referrer or url_for("community.index"))


def live_posts():
    return scoped(FeedPost, status="PUBLISHED").filter(FeedPost.deleted_at.is_(None))


def get_post(post_id):
    p = live_posts().filter_by(id=post_id).first()
    if p is None:
        abort(404)
    return p


def get_comment(comment_id):
    c = scoped(FeedPostComment).filter_by(id=comment_id).filter(FeedPostComment.deleted_at.is_(None)).first()
    if c is None or get_post_or_none(c.post_id) is None:
        abort(404)
    return c


def get_reply(reply_id):
    r = scoped(FeedCommentReply).filter_by(id=reply_id).filter(FeedCommentReply.deleted_at.is_(None)).first()
    if r is None:
        abort(404)
    parent = scoped(FeedPostComment).filter_by(id=r.comment_id).filter(FeedPostComment.deleted_at.is_(None)).first()
    if parent is None or get_post_or_none(parent.post_id) is None:
        abort(404)
    return r


def get_post_or_none(post_id):
    return live_posts().filter_by(id=post_id).first()


def is_author(obj):
    return obj.author_user_id == current_user.id


def mod_log(target_type, target_id, action, reason=None):
    db.session.add(FeedModerationLog(group_id=g.group.id, moderator_user_id=current_user.id,
                                     target_type=target_type, target_id=target_id, action=action,
                                     reason=(reason or "")[:300] or None))


def post_link(post_id, comment_id=None):
    base = f"/g/{g.group.id}/community/posts/{post_id}"
    return base + (f"#comment-{comment_id}" if comment_id else "")


def clean_text(name, limit):
    text = (request.form.get(name) or "").strip()
    if len(text) > limit:
        raise ValueError(f"Text is too long (max {limit} characters).")
    return text


def decorate(posts):
    """Attach like/comment/share counts and the viewer's like state (no N+1)."""
    ids = [p.id for p in posts]
    for p in posts:
        p.like_count = p.comment_count = p.share_count = 0
        p.liked_by_me = False
    if not ids:
        return posts
    gid = g.group.id
    likes = dict(db.session.query(FeedPostLike.post_id, func.count(FeedPostLike.id)).filter(
        FeedPostLike.group_id == gid, FeedPostLike.post_id.in_(ids)).group_by(FeedPostLike.post_id))
    mine = {r[0] for r in db.session.query(FeedPostLike.post_id).filter(
        FeedPostLike.group_id == gid, FeedPostLike.user_id == current_user.id, FeedPostLike.post_id.in_(ids))}
    comments = dict(db.session.query(FeedPostComment.post_id, func.count(FeedPostComment.id)).filter(
        FeedPostComment.group_id == gid, FeedPostComment.post_id.in_(ids), FeedPostComment.deleted_at.is_(None))
        .group_by(FeedPostComment.post_id))
    replies = dict(db.session.query(FeedPostComment.post_id, func.count(FeedCommentReply.id)).join(
        FeedCommentReply, FeedCommentReply.comment_id == FeedPostComment.id).filter(
        FeedPostComment.group_id == gid, FeedPostComment.post_id.in_(ids), FeedPostComment.deleted_at.is_(None),
        FeedCommentReply.deleted_at.is_(None)).group_by(FeedPostComment.post_id))
    shares = dict(db.session.query(FeedPostShare.post_id, func.count(FeedPostShare.id)).filter(
        FeedPostShare.group_id == gid, FeedPostShare.post_id.in_(ids)).group_by(FeedPostShare.post_id))
    for p in posts:
        p.like_count = int(likes.get(p.id, 0))
        p.comment_count = int(comments.get(p.id, 0)) + int(replies.get(p.id, 0))
        p.share_count = int(shares.get(p.id, 0))
        p.liked_by_me = p.id in mine
        orig = p.shared_post
        p.original = orig if (orig and orig.group_id == gid and orig.status == "PUBLISHED"
                              and orig.deleted_at is None) else None
    return posts


def posts_page(page_no):
    q = live_posts()
    term = (request.args.get("q") or "").strip()
    if term:
        like = f"%{term}%"
        q = q.join(User, User.id == FeedPost.author_user_id).filter(
            FeedPost.content.ilike(like) | User.full_name.ilike(like))
    cat = request.args.get("category")
    if cat in POST_CATEGORIES:
        q = q.filter(FeedPost.category == cat)
    q = q.order_by(FeedPost.is_pinned.desc(), FeedPost.created_at.desc(), FeedPost.id.desc())
    pagination = q.paginate(page=page_no, per_page=POSTS_PER_PAGE, error_out=False)
    decorate(pagination.items)
    return pagination


def comment_pagination(post, page_no):
    q = scoped(FeedPostComment, post_id=post.id).order_by(FeedPostComment.created_at, FeedPostComment.id)
    # keep deleted comments only if they still have live replies (shown as a placeholder)
    live = q.filter(FeedPostComment.deleted_at.is_(None))
    return live.paginate(page=page_no, per_page=COMMENTS_PER_PAGE, error_out=False)


def attach_files(post, files):
    """Validate and store attachments (images -> Uploadcare, documents -> ImageKit).
    Rolls back remote uploads if any file in the batch fails."""
    files = [f for f in files if f and f.filename]
    existing = FeedAttachment.query.filter_by(post_id=post.id).count()
    if existing + len(files) > MAX_ATTACHMENTS:
        raise ValueError(f"You can attach at most {MAX_ATTACHMENTS} files to a post.")
    stored = []
    try:
        for f in files:
            sf = storage.store_upload(f, group_id=g.group.id, user_id=current_user.id, kind="post_media",
                                      related=("post", post.id), max_bytes=current_app.config["ATTACHMENT_MAX_BYTES"])
            stored.append(sf)
            db.session.add(FeedAttachment(group_id=g.group.id, post_id=post.id, uploaded_by=current_user.id,
                                          file_id=sf.id, file_name=sf.filename, file_type=sf.content_type,
                                          file_size=sf.size))
    except storage.UploadError as e:
        for sf in stored:
            storage.delete_file(sf, current_user.id, reason="upload batch failed")
        raise ValueError(str(e))


def drop_attachments(post, only_ids=None):
    q = FeedAttachment.query.filter_by(post_id=post.id, group_id=g.group.id)
    if only_ids is not None:
        q = q.filter(FeedAttachment.id.in_(only_ids or [0]))
    for att in q.all():
        storage.delete_file(db.session.get(StoredFile, att.file_id), current_user.id, reason="post content removed")
        db.session.delete(att)


def retire_post(post, status):
    post.status, post.deleted_at, post.is_pinned = status, utcnow(), False
    drop_attachments(post)  # no lingering access to media of a deleted/removed post


# --------------------------------------------------------------------- pages
@bp.route("/")
@tenant_route()
def index(group_id=None):
    page = posts_page(1)
    return render_template("community/index.html", page=page, categories=POST_CATEGORIES,
                           q=request.args.get("q", ""), category=request.args.get("category", ""),
                           open_reports=scoped(FeedReport, status="OPEN").count() if g.is_official else 0)


@bp.route("/posts")
@tenant_route()
def posts_fragment(group_id=None):
    """Lazy loading: next page of post cards as an HTML fragment."""
    page = posts_page(request.args.get("page", 1, type=int))
    resp = render_template("community/_posts.html", page=page)
    return resp, 200, {"X-Has-Next": "1" if page.has_next else "0", "X-Next-Page": str(page.next_num or "")}


@bp.route("/posts/<int:post_id>")
@tenant_route()
def detail(post_id, group_id=None):
    post = get_post(post_id)
    decorate([post])
    comments = comment_pagination(post, 1)
    return render_template("community/detail.html", post=post, comments=comments, categories=POST_CATEGORIES)


# --------------------------------------------------------------------- posts
@bp.route("/posts", methods=["POST"])
@tenant_route()
def create_post(group_id=None):
    try:
        content = clean_text("content", MAX_POST_CHARS)
        category = request.form.get("category") or "General Updates"
        if category not in POST_CATEGORIES:
            raise ValueError("Select a valid category.")
        files = [f for f in request.files.getlist("files") if f and f.filename]
        if not content and not files:
            raise ValueError("Write something or attach a photo or document.")
        post = FeedPost(group_id=g.group.id, author_user_id=current_user.id, content=content, category=category)
        db.session.add(post)
        db.session.flush()
        attach_files(post, files)
        audit("community.post_created", "feed_post", post.id, group_id=g.group.id)
        db.session.commit()
    except ValueError as e:
        db.session.rollback()
        return fail(str(e))
    if wants_json():
        decorate([post])
        return jsonify(html=render_template("community/_post.html", post=post), id=post.id)
    flash("Post published.", "success")
    return redirect(url_for("community.index"))


@bp.route("/posts/<int:post_id>/edit", methods=["POST"])
@tenant_route()
def edit_post(post_id, group_id=None):
    post = get_post(post_id)
    if not is_author(post):
        abort(403)  # officials moderate; they do not rewrite other people's posts
    try:
        content = clean_text("content", MAX_POST_CHARS)
        category = request.form.get("category") or post.category
        if category not in POST_CATEGORIES:
            raise ValueError("Select a valid category.")
        remove_ids = [int(x) for x in request.form.getlist("remove_attachment") if x.isdigit()]
        if remove_ids:
            drop_attachments(post, remove_ids)
        files = [f for f in request.files.getlist("files") if f and f.filename]
        db.session.flush()
        remaining = FeedAttachment.query.filter_by(post_id=post.id).count()
        if not content and not files and not remaining and not post.shared_post_id:
            raise ValueError("A post cannot be empty.")
        attach_files(post, files)
        post.content, post.category = content, category
        post.updated_at = utcnow()
        audit("community.post_edited", "feed_post", post.id, group_id=g.group.id)
        db.session.commit()
    except ValueError as e:
        db.session.rollback()
        return fail(str(e))
    if wants_json():
        decorate([post])
        return jsonify(html=render_template("community/_post.html", post=post))
    flash("Post updated.", "success")
    return redirect(request.referrer or url_for("community.detail", post_id=post.id))


@bp.route("/posts/<int:post_id>/delete", methods=["POST"])
@tenant_route()
def delete_post(post_id, group_id=None):
    post = get_post(post_id)
    own = is_author(post)
    if not own and not g.is_official:
        abort(403)
    reason = (request.form.get("reason") or "").strip()
    if own:
        retire_post(post, "DELETED")
        audit("community.post_deleted", "feed_post", post.id, group_id=g.group.id)
    else:
        retire_post(post, "REMOVED")
        mod_log("post", post.id, "REMOVE", reason)
        audit("community.post_removed", "feed_post", post.id, {"reason": reason}, group_id=g.group.id)
        notify.notify([post.author_user_id], g.group.id, "COMMUNITY", "Your post was removed",
                      reason or "A group official removed your post for breaking the group rules.", None)
        resolve_reports("post", post.id)
    db.session.commit()
    if wants_json():
        return jsonify(ok=True)
    flash("Post removed.", "success")
    return redirect(url_for("community.index"))


@bp.route("/posts/<int:post_id>/pin", methods=["POST"])
@tenant_route(official=True)
def pin_post(post_id, group_id=None):
    post = get_post(post_id)
    pin = request.form.get("pin") == "1"
    post.is_pinned = pin
    mod_log("post", post.id, "PIN" if pin else "UNPIN")
    audit("community.post_pinned" if pin else "community.post_unpinned", "feed_post", post.id, group_id=g.group.id)
    db.session.commit()
    if wants_json():
        return jsonify(ok=True, pinned=pin)
    flash("Post pinned." if pin else "Post unpinned.", "success")
    return redirect(request.referrer or url_for("community.index"))


# --------------------------------------------------------------------- likes
def _like_count(post_id):
    return FeedPostLike.query.filter_by(group_id=g.group.id, post_id=post_id).count()


@bp.route("/posts/<int:post_id>/like", methods=["POST"])
@tenant_route()
def like(post_id, group_id=None):
    post = get_post(post_id)
    if not FeedPostLike.query.filter_by(post_id=post.id, user_id=current_user.id).first():
        try:
            with db.session.begin_nested():  # unique(post_id, user_id) stops double-likes under races
                db.session.add(FeedPostLike(group_id=g.group.id, post_id=post.id, user_id=current_user.id))
                db.session.flush()
            if post.author_user_id != current_user.id:
                notify.notify([post.author_user_id], g.group.id, "COMMUNITY", "New like on your post",
                              f"{current_user.full_name} liked your post.", post_link(post.id),
                              dedupe_key=f"like:{post.id}:{current_user.id}")
        except IntegrityError:
            pass
        db.session.commit()
    return jsonify(liked=True, count=_like_count(post.id))


@bp.route("/posts/<int:post_id>/unlike", methods=["POST"])
@tenant_route()
def unlike(post_id, group_id=None):
    post = get_post(post_id)
    row = FeedPostLike.query.filter_by(post_id=post.id, user_id=current_user.id, group_id=g.group.id).first()
    if row:
        db.session.delete(row)
        db.session.commit()
    return jsonify(liked=False, count=_like_count(post.id))


@bp.route("/posts/<int:post_id>/likes")
@tenant_route()
def likes(post_id, group_id=None):
    post = get_post(post_id)
    rows = FeedPostLike.query.filter_by(post_id=post.id, group_id=g.group.id).order_by(FeedPostLike.created_at.desc()) \
        .limit(100).all()
    return jsonify(count=_like_count(post.id), users=[{"name": r.user.full_name} for r in rows])


# ------------------------------------------------------------------ comments
@bp.route("/posts/<int:post_id>/comments")
@tenant_route()
def comments_fragment(post_id, group_id=None):
    post = get_post(post_id)
    page = comment_pagination(post, request.args.get("page", 1, type=int))
    html = render_template("community/_comments.html", post=post, comments=page)
    return html, 200, {"X-Has-Next": "1" if page.has_next else "0", "X-Next-Page": str(page.next_num or "")}


@bp.route("/posts/<int:post_id>/comments", methods=["POST"])
@tenant_route()
def add_comment(post_id, group_id=None):
    post = get_post(post_id)
    try:
        text = clean_text("text", MAX_COMMENT_CHARS)
        if not text:
            raise ValueError("Write a comment first.")
    except ValueError as e:
        return fail(str(e))
    c = FeedPostComment(group_id=g.group.id, post_id=post.id, author_user_id=current_user.id, comment_text=text)
    db.session.add(c)
    db.session.flush()
    if post.author_user_id != current_user.id:
        notify.notify([post.author_user_id], g.group.id, "COMMUNITY", "New comment on your post",
                      f"{current_user.full_name}: {text[:120]}", post_link(post.id, c.id),
                      dedupe_key=f"comment:{c.id}")
    audit("community.comment_added", "feed_comment", c.id, group_id=g.group.id)
    db.session.commit()
    decorate([post])
    if wants_json():
        return jsonify(html=render_template("community/_comment.html", c=c, post=post), count=post.comment_count)
    return redirect(post_link(post.id, c.id))


@bp.route("/comments/<int:comment_id>/replies", methods=["POST"])
@tenant_route()
def add_reply(comment_id, group_id=None):
    comment = get_comment(comment_id)
    post = get_post(comment.post_id)
    try:
        text = clean_text("text", MAX_COMMENT_CHARS)
        if not text:
            raise ValueError("Write a reply first.")
    except ValueError as e:
        return fail(str(e))
    reply_to = request.form.get("reply_to_user_id", type=int)
    if reply_to and not GroupMember.query.filter_by(group_id=g.group.id, user_id=reply_to, status="ACTIVE").first():
        reply_to = None  # never reference someone outside this group
    r = FeedCommentReply(group_id=g.group.id, comment_id=comment.id, author_user_id=current_user.id,
                         reply_to_user_id=reply_to, reply_text=text)
    db.session.add(r)
    db.session.flush()
    targets = {comment.author_user_id} | ({reply_to} if reply_to else set())
    targets.discard(current_user.id)
    notify.notify(targets, g.group.id, "COMMUNITY", "New reply to your comment",
                  f"{current_user.full_name}: {text[:120]}", post_link(post.id, comment.id),
                  dedupe_key=f"reply:{r.id}")
    audit("community.reply_added", "feed_reply", r.id, group_id=g.group.id)
    db.session.commit()
    decorate([post])
    if wants_json():
        return jsonify(html=render_template("community/_reply.html", r=r, c=comment, post=post), count=post.comment_count)
    return redirect(post_link(post.id, comment.id))


def _edit(obj, field, label):
    if not is_author(obj):
        abort(403)
    try:
        text = clean_text("text", MAX_COMMENT_CHARS)
        if not text:
            raise ValueError("The text cannot be empty.")
    except ValueError as e:
        return fail(str(e))
    setattr(obj, field, text)
    obj.updated_at = utcnow()
    audit(f"community.{label}_edited", f"feed_{label}", obj.id, group_id=g.group.id)
    db.session.commit()
    return jsonify(ok=True, text=text) if wants_json() else redirect(request.referrer or url_for("community.index"))


def _remove(obj, target_type, label, post_id):
    own = is_author(obj)
    if not own and not g.is_official:
        abort(403)
    obj.deleted_at = utcnow()
    reason = (request.form.get("reason") or "").strip()
    if own:
        audit(f"community.{label}_deleted", f"feed_{label}", obj.id, group_id=g.group.id)
    else:
        mod_log(target_type, obj.id, "REMOVE", reason)
        audit(f"community.{label}_removed", f"feed_{label}", obj.id, {"reason": reason}, group_id=g.group.id)
        notify.notify([obj.author_user_id], g.group.id, "COMMUNITY", f"Your {label} was removed",
                      reason or f"A group official removed your {label}.", post_link(post_id))
        resolve_reports(target_type, obj.id)
    db.session.commit()
    post = get_post_or_none(post_id)
    if post:
        decorate([post])
    return jsonify(ok=True, count=post.comment_count if post else 0) if wants_json() else \
        redirect(request.referrer or url_for("community.index"))


@bp.route("/comments/<int:comment_id>/edit", methods=["POST"])
@tenant_route()
def edit_comment(comment_id, group_id=None):
    return _edit(get_comment(comment_id), "comment_text", "comment")


@bp.route("/comments/<int:comment_id>/delete", methods=["POST"])
@tenant_route()
def delete_comment(comment_id, group_id=None):
    c = get_comment(comment_id)
    return _remove(c, "comment", "comment", c.post_id)


@bp.route("/replies/<int:reply_id>/edit", methods=["POST"])
@tenant_route()
def edit_reply(reply_id, group_id=None):
    return _edit(get_reply(reply_id), "reply_text", "reply")


@bp.route("/replies/<int:reply_id>/delete", methods=["POST"])
@tenant_route()
def delete_reply(reply_id, group_id=None):
    r = get_reply(reply_id)
    return _remove(r, "reply", "reply", r.comment.post_id)


# -------------------------------------------------------------------- shares
@bp.route("/posts/<int:post_id>/share", methods=["POST"])
@tenant_route()
def share(post_id, group_id=None):
    """Share within the SAME group only. The share keeps attribution to the
    original author and links back to the original post."""
    post = get_post(post_id)
    original = post
    if post.shared_post_id:  # sharing a share points at the original
        original = get_post_or_none(post.shared_post_id)
        if original is None:
            return fail("The original post is no longer available.", 404)
    try:
        note = clean_text("content", MAX_POST_CHARS)
    except ValueError as e:
        return fail(str(e))
    new = FeedPost(group_id=g.group.id, author_user_id=current_user.id, content=note, category=original.category,
                   shared_post_id=original.id)
    db.session.add(new)
    db.session.flush()
    db.session.add(FeedPostShare(group_id=g.group.id, post_id=original.id, shared_by=current_user.id,
                                 new_post_id=new.id))
    if original.author_user_id != current_user.id:
        notify.notify([original.author_user_id], g.group.id, "COMMUNITY", "Your post was shared",
                      f"{current_user.full_name} shared your post.", post_link(new.id),
                      dedupe_key=f"share:{original.id}:{current_user.id}")
    audit("community.post_shared", "feed_post", new.id, {"original": original.id}, group_id=g.group.id)
    db.session.commit()
    if wants_json():
        decorate([new])
        return jsonify(html=render_template("community/_post.html", post=new), id=new.id)
    flash("Post shared with your group.", "success")
    return redirect(url_for("community.index"))


# ------------------------------------------------------------------- reports
def resolve_reports(target_type, target_id, status="ACTIONED"):
    key = f"{target_type}:{target_id}"
    for r in scoped(FeedReport, target_key=key, status="OPEN"):
        r.status, r.reviewed_by, r.reviewed_at = status, current_user.id, utcnow()


def _report(kind, obj, post_id):
    if is_author(obj):
        return fail("You cannot report your own content.")
    reason = request.form.get("reason")
    if reason not in REPORT_REASONS:
        return fail("Select a reason for your report.")
    key = f"{kind}:{obj.id}"
    if FeedReport.query.filter_by(reported_by=current_user.id, target_key=key).first():
        return fail("You have already reported this. Our officials will review it.")
    try:
        with db.session.begin_nested():
            db.session.add(FeedReport(
                group_id=g.group.id, reported_by=current_user.id, target_key=key, reason=reason,
                description=(request.form.get("description") or "").strip()[:500] or None,
                post_id=post_id if kind == "post" else None, comment_id=obj.id if kind == "comment" else None,
                reply_id=obj.id if kind == "reply" else None))
            db.session.flush()
    except IntegrityError:
        db.session.rollback()
        return fail("You have already reported this. Our officials will review it.")
    notify.notify(notify.official_ids(g.group.id, exclude=current_user.id), g.group.id, "COMMUNITY",
                  "Content reported", f"A {kind} was reported ({reason}).", f"/g/{g.group.id}/community/reports")
    audit("community.reported", f"feed_{kind}", obj.id, {"reason": reason}, group_id=g.group.id)
    db.session.commit()
    if wants_json():
        return jsonify(ok=True, message="Thank you. Group officials will review this report.")
    flash("Thank you. Group officials will review this report.", "success")
    return redirect(request.referrer or url_for("community.index"))


@bp.route("/posts/<int:post_id>/report", methods=["POST"])
@tenant_route()
def report_post(post_id, group_id=None):
    p = get_post(post_id)
    return _report("post", p, p.id)


@bp.route("/comments/<int:comment_id>/report", methods=["POST"])
@tenant_route()
def report_comment(comment_id, group_id=None):
    c = get_comment(comment_id)
    return _report("comment", c, c.post_id)


@bp.route("/replies/<int:reply_id>/report", methods=["POST"])
@tenant_route()
def report_reply(reply_id, group_id=None):
    r = get_reply(reply_id)
    return _report("reply", r, r.comment.post_id)


def _report_target(rep):
    """(kind, object, post) for display, or (kind, None, None) if already removed."""
    if rep.post_id and rep.comment_id is None and rep.reply_id is None:
        p = scoped(FeedPost).filter_by(id=rep.post_id).first()
        live = p is not None and p.status == "PUBLISHED" and p.deleted_at is None
        return "post", p if live else None, p
    if rep.comment_id:
        c = scoped(FeedPostComment).filter_by(id=rep.comment_id).first()
        return "comment", (c if c and not c.deleted_at else None), (c.post if c and hasattr(c, "post") else None)
    r = scoped(FeedCommentReply).filter_by(id=rep.reply_id).first()
    return "reply", (r if r and not r.deleted_at else None), None


@bp.route("/reports")
@tenant_route(official=True)
def reports(group_id=None):
    status = request.args.get("status", "OPEN")
    q = scoped(FeedReport)
    if status in ("OPEN", "DISMISSED", "ACTIONED"):
        q = q.filter_by(status=status)
    page = q.order_by(FeedReport.created_at.desc()).paginate(
        page=request.args.get("page", 1, type=int), per_page=15, error_out=False)
    rows = []
    for rep in page.items:
        kind, obj, _ = _report_target(rep)
        text = None
        if obj is not None:
            text = getattr(obj, "content", None) or getattr(obj, "comment_text", None) or getattr(obj, "reply_text", None)
        rows.append({"report": rep, "kind": kind, "obj": obj, "text": text,
                     "post_id": rep.post_id or (obj.post_id if kind == "comment" and obj else None)})
    return render_template("community/reports.html", rows=rows, page=page, status=status)


@bp.route("/reports/<int:report_id>/review", methods=["POST"])
@tenant_route(official=True)
def review_report(report_id, group_id=None):
    rep = scoped(FeedReport).filter_by(id=report_id).first_or_404()
    action = request.form.get("action")
    if rep.status != "OPEN":
        flash("This report was already reviewed.", "info")
        return redirect(url_for("community.reports"))
    reason = (request.form.get("reason") or "").strip() or rep.reason
    if action == "dismiss":
        rep.status, rep.reviewed_by, rep.reviewed_at = "DISMISSED", current_user.id, utcnow()
        mod_log(_report_target(rep)[0], rep.reply_id or rep.comment_id or rep.post_id, "DISMISS_REPORT", reason)
        audit("community.report_dismissed", "feed_report", rep.id, group_id=g.group.id)
        flash("Report dismissed.", "success")
    elif action == "remove":
        kind, obj, _ = _report_target(rep)
        if obj is not None:
            if kind == "post":
                retire_post(obj, "REMOVED")
            else:
                obj.deleted_at = utcnow()
            mod_log(kind, obj.id, "REMOVE", reason)
            notify.notify([obj.author_user_id], g.group.id, "COMMUNITY", f"Your {kind} was removed",
                          f"Reason: {reason}", None)
            audit(f"community.{kind}_removed", f"feed_{kind}", obj.id, {"report": rep.id}, group_id=g.group.id)
        resolve_reports(kind, obj.id if obj else (rep.reply_id or rep.comment_id or rep.post_id))
        rep.status, rep.reviewed_by, rep.reviewed_at = "ACTIONED", current_user.id, utcnow()
        flash("Content removed and the report closed.", "success")
    else:
        abort(400)
    db.session.commit()
    return redirect(url_for("community.reports"))


@bp.route("/moderation-log")
@tenant_route(official=True)
def moderation_log(group_id=None):
    page = scoped(FeedModerationLog).order_by(FeedModerationLog.created_at.desc()).paginate(
        page=request.args.get("page", 1, type=int), per_page=25, error_out=False)
    return render_template("community/moderation.html", page=page)
