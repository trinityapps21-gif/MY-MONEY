"""Platform administrator area. Sees aggregates and manages groups/users and the
global interest policy; has no access to group-internal records such as
members' contributions or borrower details."""
import json

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func

from app.extensions import db
from app.models import AuditLog, Group, GroupMember, Loan, User, utcnow
from app.services import analytics, ledger, loans as loan_svc, storage
from app.services.audit import audit
from app.services.money import parse_rate_bp
from app.services.seed import get_setting, seed_defaults, set_setting
from app.services.tenant import platform_required

bp = Blueprint("platform", __name__, url_prefix="/platform")


@bp.route("/")
@platform_required
def dashboard():
    seed_defaults()
    s = analytics.platform_summary()
    s["outstanding_principal"] = analytics.platform_outstanding()
    recent_groups = Group.query.order_by(Group.created_at.desc()).limit(6).all()
    activity = AuditLog.query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(10).all()
    return render_template("platform/dashboard.html", s=s, growth=analytics.platform_growth(),
                           recent_groups=recent_groups, activity=activity)


@bp.route("/groups")
@platform_required
def groups():
    q = Group.query
    term = (request.args.get("q") or "").strip()
    if term:
        q = q.filter(Group.name.ilike(f"%{term}%") | Group.location.ilike(f"%{term}%"))
    status = request.args.get("status")
    if status in ("ACTIVE", "SUSPENDED"):
        q = q.filter(Group.status == status)
    page = q.order_by(Group.created_at.desc()).paginate(page=request.args.get("page", 1, type=int), per_page=15,
                                                        error_out=False)
    counts = dict(db.session.query(GroupMember.group_id, func.count(GroupMember.id)).filter(
        GroupMember.status == "ACTIVE", GroupMember.group_id.in_([x.id for x in page.items] or [0]))
        .group_by(GroupMember.group_id))
    return render_template("platform/groups.html", page=page, q=term, status=status, counts=counts)


@bp.route("/groups/<int:group_id>")
@platform_required
def group_detail(group_id):
    grp = db.session.get(Group, group_id) or abort(404)
    officials = GroupMember.query.filter(GroupMember.group_id == grp.id, GroupMember.status == "ACTIVE",
                                         GroupMember.role.in_(["ADMIN", "TREASURER", "VICE_CHAIR"])).all()
    members = GroupMember.query.filter_by(group_id=grp.id, status="ACTIVE").count()
    pending = GroupMember.query.filter_by(group_id=grp.id, status="PENDING").count()
    t = ledger.totals(grp.id)
    loans = Loan.query.filter(Loan.group_id == grp.id, Loan.disbursement_tx_id.isnot(None)).count()
    activity = AuditLog.query.filter_by(group_id=grp.id).order_by(AuditLog.created_at.desc()).limit(15).all()
    return render_template("platform/group_detail.html", grp=grp, officials=officials, members=members,
                           pending=pending, t=t, loans=loans, activity=activity)


@bp.route("/groups/<int:group_id>/status", methods=["POST"])
@platform_required
def group_status(group_id):
    grp = db.session.get(Group, group_id) or abort(404)
    action = request.form.get("action")
    if action == "suspend" and grp.status == "ACTIVE":
        grp.status = "SUSPENDED"
    elif action in ("activate", "reactivate") and grp.status == "SUSPENDED":
        grp.status = "ACTIVE"
    else:
        flash("Nothing to change.", "info")
        return redirect(url_for("platform.group_detail", group_id=grp.id))
    audit(f"platform.group_{grp.status.lower()}", "group", grp.id, {"name": grp.name}, group_id=grp.id)
    db.session.commit()
    flash(f"{grp.name} is now {grp.status.lower()}.", "success")
    return redirect(request.referrer or url_for("platform.group_detail", group_id=grp.id))


@bp.route("/users")
@platform_required
def users():
    q = User.query.filter(User.is_platform_admin.is_(False))
    term = (request.args.get("q") or "").strip()
    if term:
        like = f"%{term}%"
        q = q.filter(User.full_name.ilike(like) | User.phone.ilike(like) | User.email.ilike(like))
    page = q.order_by(User.created_at.desc()).paginate(page=request.args.get("page", 1, type=int), per_page=20,
                                                       error_out=False)
    counts = dict(db.session.query(GroupMember.user_id, func.count(GroupMember.id)).filter(
        GroupMember.status == "ACTIVE", GroupMember.user_id.in_([u.id for u in page.items] or [0]))
        .group_by(GroupMember.user_id))
    return render_template("platform/users.html", page=page, q=term, counts=counts)


@bp.route("/users/<int:user_id>/status", methods=["POST"])
@platform_required
def user_status(user_id):
    u = db.session.get(User, user_id) or abort(404)
    if u.is_platform_admin or u.id == current_user.id:
        abort(403)
    u.status = "SUSPENDED" if request.form.get("action") == "suspend" else "ACTIVE"
    audit(f"platform.user_{u.status.lower()}", "user", u.id)
    db.session.commit()
    flash(f"Account {u.status.lower()}.", "success")
    return redirect(request.referrer or url_for("platform.users"))


@bp.route("/interest", methods=["GET", "POST"])
@platform_required
def interest():
    s = loan_svc.get_interest_settings()
    if request.method == "POST":
        try:
            default, mn, mx = (parse_rate_bp(request.form.get(k)) for k in ("default_rate", "min_rate", "max_rate"))
            if not (mn <= default <= mx):
                raise ValueError("The default rate must sit between the minimum and maximum rates.")
            if mx > 100000:
                raise ValueError("Maximum rate is too high.")
            flat, reducing = request.form.get("flat_enabled") == "1", request.form.get("reducing_enabled") == "1"
            if not (flat or reducing):
                raise ValueError("Enable at least one interest method.")
            period = request.form.get("default_period")
            if period not in ("WEEK", "MONTH", "YEAR"):
                raise ValueError("Select a default interest period.")
        except ValueError as e:
            flash(str(e), "error")
        else:
            before = {"default_bp": s.default_rate_bp, "min_bp": s.min_rate_bp, "max_bp": s.max_rate_bp,
                      "flat": s.flat_enabled, "reducing": s.reducing_enabled, "period": s.default_period,
                      "custom": s.allow_custom_rates}
            s.default_rate_bp, s.min_rate_bp, s.max_rate_bp = default, mn, mx
            s.flat_enabled, s.reducing_enabled, s.default_period = flat, reducing, period
            s.allow_custom_rates = request.form.get("allow_custom_rates") == "1"
            s.updated_by, s.updated_at = current_user.id, utcnow()
            after = {"default_bp": default, "min_bp": mn, "max_bp": mx, "flat": flat, "reducing": reducing,
                     "period": period, "custom": s.allow_custom_rates}
            audit("platform.interest_policy_changed", "interest_settings", 1, {"before": before, "after": after})
            db.session.commit()
            flash("Interest policy updated. It applies to NEW loans only; existing loans keep their agreed terms.",
                  "success")
            return redirect(url_for("platform.interest"))
    history = AuditLog.query.filter_by(action="platform.interest_policy_changed") \
        .order_by(AuditLog.created_at.desc()).limit(20).all()
    parsed = []
    for h in history:
        try:
            parsed.append((h, json.loads(h.details or "{}")))
        except ValueError:
            parsed.append((h, {}))
    return render_template("platform/interest.html", s=s, history=parsed)


@bp.route("/analytics")
@platform_required
def platform_analytics():
    s = analytics.platform_summary()
    s["outstanding_principal"] = analytics.platform_outstanding()
    top = db.session.query(Group.name, func.count(GroupMember.id)).join(GroupMember, GroupMember.group_id == Group.id) \
        .filter(GroupMember.status == "ACTIVE").group_by(Group.id, Group.name) \
        .order_by(func.count(GroupMember.id).desc()).limit(8).all()
    return render_template("platform/analytics.html", s=s, growth=analytics.platform_growth(12), top=top)


@bp.route("/audit")
@platform_required
def audit_logs():
    q = AuditLog.query
    action = (request.args.get("action") or "").strip()
    if action:
        q = q.filter(AuditLog.action.ilike(f"%{action}%"))
    gid = request.args.get("group", type=int)
    if gid:
        q = q.filter(AuditLog.group_id == gid)
    page = q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).paginate(
        page=request.args.get("page", 1, type=int), per_page=30, error_out=False)
    return render_template("platform/audit.html", page=page, action=action, group_id=gid)


@bp.route("/settings", methods=["GET", "POST"])
@platform_required
def settings():
    if request.method == "POST":
        name = (request.form.get("platform_name") or "").strip()
        if len(name) < 2:
            flash("Enter a platform name.", "error")
        else:
            changes = {"platform_name": name[:60], "default_currency": (request.form.get("default_currency") or "UGX")[:3],
                       "contact_email": (request.form.get("contact_email") or "").strip()[:255],
                       "maintenance_mode": "1" if request.form.get("maintenance_mode") == "1" else "0",
                       "allow_group_registration": "1" if request.form.get("allow_group_registration") == "1" else "0"}
            for k, v in changes.items():
                set_setting(k, v)
            audit("platform.settings_changed", "platform_settings", None, changes)
            db.session.commit()
            flash("Platform settings saved.", "success")
            return redirect(url_for("platform.settings"))
    from app.services.money import CURRENCIES
    return render_template("platform/settings.html", get=get_setting, currencies=CURRENCIES,
                           storage=storage.storage_status())
