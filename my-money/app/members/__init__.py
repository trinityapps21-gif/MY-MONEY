import secrets
from datetime import timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from flask_login import current_user, login_user

from app.auth import RESET_LINK_HOURS, make_reset_link, register_user, safe_next
from app.services.mail import send_email
from app.extensions import db, limiter
from app.models import (Group, GroupInvitation, GroupMember, MemberApplication, PasswordResetRequest, User, utcnow)
from app.services import feedlog, ledger, notify
from app.services.audit import audit
from app.services.tenant import get_scoped_or_404, scoped, tenant_route

bp = Blueprint("members", __name__)


def _join_url(inv):
    return url_for("members.join", token=inv.token, _external=True)


# ------------------------------------------------------------------ member lists
@bp.route("/g/<int:group_id>/members")
@tenant_route()
def index(group_id=None):
    q = (request.args.get("q") or "").strip()
    status = request.args.get("status") or "ACTIVE"
    query = GroupMember.query.join(User, User.id == GroupMember.user_id).filter(GroupMember.group_id == g.group.id)
    if status in ("ACTIVE", "PENDING", "SUSPENDED", "REJECTED"):
        query = query.filter(GroupMember.status == status)
    if q:
        like = f"%{q}%"
        query = query.filter((User.full_name.ilike(like)) | (User.phone.ilike(like)))
    page = request.args.get("page", 1, type=int)
    pagination = query.order_by(User.full_name).paginate(page=page, per_page=15, error_out=False)
    pending_count = scoped(MemberApplication, status="PENDING").count()
    shares = {m.id: ledger.member_savings(g.group.id, m.id) for m in pagination.items} if g.is_official \
        or True else {}
    return render_template("members/index.html", pagination=pagination, q=q, status=status,
                           pending_count=pending_count, shares=shares)


@bp.route("/g/<int:group_id>/members/pending")
@tenant_route(official=True)
def pending(group_id=None):
    apps = MemberApplication.query.filter_by(group_id=g.group.id, status="PENDING") \
        .order_by(MemberApplication.created_at).all()
    return render_template("members/pending.html", apps=apps)


# ----------------------------------------------------------- password reset requests
def _group_reset_requests():
    """Reset requests from ACTIVE members of *this* group only."""
    ids = db.session.query(GroupMember.user_id).filter(GroupMember.group_id == g.group.id,
                                                      GroupMember.status == "ACTIVE")
    return PasswordResetRequest.query.filter(PasswordResetRequest.user_id.in_(ids.scalar_subquery()))


def _resets_page(new_link=None, link_for=None):
    q = _group_reset_requests()
    open_reqs = q.filter(PasswordResetRequest.status.in_(("PENDING", "APPROVED"))) \
        .order_by(PasswordResetRequest.created_at.desc()).all()
    done = q.filter(PasswordResetRequest.status.in_(("REJECTED", "COMPLETED"))) \
        .order_by(PasswordResetRequest.created_at.desc()).limit(15).all()
    mems = {m.user_id: m for m in GroupMember.query.filter_by(group_id=g.group.id).all()}
    return render_template("members/password_resets.html", open_reqs=open_reqs, done=done, mems=mems,
                           new_link=new_link, link_for=link_for, hours=RESET_LINK_HOURS)


@bp.route("/g/<int:group_id>/members/password-resets")
@tenant_route(official=True)
def password_resets(group_id=None):
    return _resets_page()


@bp.route("/g/<int:group_id>/members/password-resets/<int:req_id>", methods=["POST"])
@tenant_route(official=True)
def password_reset_decide(req_id, group_id=None):
    req = _group_reset_requests().filter(PasswordResetRequest.id == req_id).first_or_404()
    if req.user_id == current_user.id:
        flash("Another official must handle your own reset request. You can use your recovery code instead.", "error")
        return redirect(url_for("members.password_resets"))
    action = request.form.get("action")
    if req.status not in ("PENDING", "APPROVED") or action not in ("send", "reject"):
        flash("This request has already been handled.", "error")
        return redirect(url_for("members.password_resets"))
    req.decided_by, req.decided_group_id, req.decided_at = current_user.id, g.group.id, utcnow()
    if action == "reject":
        req.status = "REJECTED"
        audit("auth.reset_rejected", "user", req.user_id, group_id=g.group.id)
        db.session.commit()
        flash("Request rejected. No reset link was sent.", "success")
        return redirect(url_for("members.password_resets"))
    req.status = "APPROVED"
    link = make_reset_link(req)
    audit("auth.reset_link_sent", "user", req.user_id, group_id=g.group.id)
    db.session.commit()
    if req.user.email:
        send_email(req.user.email, "Reset your MY MONEY password",
                   f"Hello {req.user.first_name},\n\nYour group administrator approved your request. Use this link "
                   f"within {RESET_LINK_HOURS} hours to choose a new password:\n{link}\n\nIf you did not ask for "
                   "this, ignore this message.\n\nMY MONEY - Developed and Powered by Emtrixz Technology Company.")
    resp = _resets_page(new_link=link, link_for=req.user)
    resp = current_app_response(resp)
    return resp


def current_app_response(html):
    from flask import make_response
    r = make_response(html)
    r.headers["Cache-Control"] = "no-store"
    return r


@bp.route("/g/<int:group_id>/members/<int:member_id>/decide", methods=["POST"])
@tenant_route(official=True)
def decide(member_id, group_id=None):
    m = get_scoped_or_404(GroupMember, member_id)
    action = request.form.get("action")
    app_row = scoped(MemberApplication, user_id=m.user_id, status="PENDING").first()
    if action in ("approve", "reject") and m.status != "PENDING":
        flash("This application has already been decided.", "error")
    elif action == "approve":
        m.status, m.approved_by, m.joined_at = "ACTIVE", current_user.id, utcnow()
        if app_row:
            app_row.status, app_row.decided_by, app_row.decided_at = "APPROVED", current_user.id, utcnow()
        feedlog.emit(g.group.id, "MEMBER_APPROVED", "New member approved",
                     f"{m.user.full_name} joined the group.", None, current_user.id, "member", m.id)
        notify.notify([m.user_id], g.group.id, "MEMBERSHIP", "Membership approved",
                      f"You are now a member of {g.group.name}.", f"/g/{g.group.id}/")
        audit("member.approved", "group_member", m.id, group_id=g.group.id)
        flash(f"{m.user.full_name} approved.", "success")
    elif action == "reject":
        m.status = "REJECTED"
        if app_row:
            app_row.status, app_row.decided_by, app_row.decided_at = "REJECTED", current_user.id, utcnow()
        notify.notify([m.user_id], g.group.id, "MEMBERSHIP", "Membership application declined",
                      f"Your application to {g.group.name} was not approved.", None)
        audit("member.rejected", "group_member", m.id, group_id=g.group.id)
        flash("Application rejected.", "success")
    elif action in ("suspend", "reactivate"):
        if m.user_id == g.group.owner_id:
            flash("The group owner cannot be suspended.", "error")
        elif action == "suspend" and m.status == "ACTIVE":
            m.status = "SUSPENDED"
            audit("member.suspended", "group_member", m.id, group_id=g.group.id)
            flash("Member suspended.", "success")
        elif action == "reactivate" and m.status == "SUSPENDED":
            m.status = "ACTIVE"
            audit("member.reactivated", "group_member", m.id, group_id=g.group.id)
            flash("Member reactivated.", "success")
    else:
        abort(400)
    db.session.commit()
    return redirect(request.referrer or url_for("members.index"))


@bp.route("/g/<int:group_id>/members/<int:member_id>/role", methods=["POST"])
@tenant_route(official=True)
def set_role(member_id, group_id=None):
    """Appoint Treasurer / Vice Chairperson from approved members. Only one holder
    of each office; the previous holder reverts to ordinary member."""
    m = get_scoped_or_404(GroupMember, member_id)
    role = request.form.get("role")
    if m.status != "ACTIVE":
        flash("Only approved members can hold an office.", "error")
    elif m.user_id == g.group.owner_id:
        flash("The group owner stays Group Administrator.", "error")
    elif role not in ("TREASURER", "VICE_CHAIR", "MEMBER"):
        abort(400)
    else:
        if role in ("TREASURER", "VICE_CHAIR"):
            for prev in scoped(GroupMember, role=role).filter(GroupMember.id != m.id):
                prev.role = "MEMBER"
        m.role = role
        audit("member.role_changed", "group_member", m.id, {"role": role}, group_id=g.group.id)
        db.session.commit()
        flash(f"{m.user.full_name} is now {m.role_label}.", "success")
    return redirect(request.referrer or url_for("members.index"))


# ------------------------------------------------------------------ invitations
@bp.route("/g/<int:group_id>/invitations", methods=["GET", "POST"])
@tenant_route(official=True)
def invitations(group_id=None):
    if request.method == "POST":
        days = request.form.get("expires_days", type=int)
        max_uses = request.form.get("max_uses", type=int)
        inv = GroupInvitation(group_id=g.group.id, token=secrets.token_urlsafe(32),
                              label=(request.form.get("label") or "").strip()[:80] or None,
                              expires_at=utcnow() + timedelta(days=days) if days and 0 < days <= 365 else None,
                              max_uses=max_uses if max_uses and max_uses > 0 else None, created_by=current_user.id)
        db.session.add(inv)
        audit("invitation.created", "group_invitation", None, group_id=g.group.id)
        db.session.commit()
        flash("Invitation link generated. Copy it below and share it.", "success")
        return redirect(url_for("members.invitations"))
    items = scoped(GroupInvitation).order_by(GroupInvitation.created_at.desc()).all()
    return render_template("members/invitations.html", items=items, join_url=_join_url)


@bp.route("/g/<int:group_id>/invitations/<int:inv_id>/revoke", methods=["POST"])
@tenant_route(official=True)
def revoke(inv_id, group_id=None):
    inv = get_scoped_or_404(GroupInvitation, inv_id)
    if not inv.revoked_at:
        inv.revoked_at = utcnow()
        audit("invitation.revoked", "group_invitation", inv.id, group_id=g.group.id)
        db.session.commit()
    flash("Invitation revoked.", "success")
    return redirect(url_for("members.invitations"))


# ---------------------------------------------------------- public join page
def _valid_invitation(token):
    inv = GroupInvitation.query.filter_by(token=token).first()
    if inv is None or inv.state != "ACTIVE" or inv.group.status != "ACTIVE":
        return None
    return inv


@bp.route("/join/<token>", methods=["GET", "POST"])
@limiter.limit("20 per hour", methods=["POST"])
def join(token):
    inv = _valid_invitation(token)
    if inv is None:
        return render_template("members/join_invalid.html"), 404
    group = inv.group
    existing = None
    if current_user.is_authenticated:
        existing = GroupMember.query.filter_by(group_id=group.id, user_id=current_user.id).first()
    if request.method == "POST":
        user, err = (current_user, None) if current_user.is_authenticated else register_user(
            request.form.get("full_name"), request.form.get("phone"), request.form.get("email"),
            request.form.get("password") or "", request.form.get("confirm_password") or "")
        if not err and GroupMember.query.filter_by(group_id=group.id, user_id=user.id).first():
            err = "You have already joined or applied to this group."
        if err:
            db.session.rollback()
            flash(err, "error")
        else:
            db.session.add(GroupMember(group_id=group.id, user_id=user.id, role="MEMBER", status="PENDING"))
            db.session.add(MemberApplication(group_id=group.id, user_id=user.id, invitation_id=inv.id))
            inv.use_count += 1
            notify.notify(notify.official_ids(group.id), group.id, "MEMBERSHIP", "New membership application",
                          f"{user.full_name} applied to join {group.name}.", f"/g/{group.id}/members/pending")
            audit("member.applied", "group", group.id, group_id=group.id, user_id=user.id)
            db.session.commit()
            if not current_user.is_authenticated:
                login_user(user)
            return render_template("members/join_done.html", group=group)
    return render_template("members/join.html", group=group, token=token, existing=existing)
