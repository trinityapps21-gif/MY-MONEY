from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models import (FEED_EVENT_TYPES, Group, GroupFeed, GroupMember, MemberApplication, StoredFile, Transaction, utcnow)
from app.services import analytics, ledger, loans as loan_svc, storage
from app.services.audit import audit
from app.services.money import CURRENCIES, decimals_for, parse_rate_bp, to_minor
from app.services.seed import seed_defaults
from app.services.tenant import tenant_route
from app.services.timeutil import local_today, resolve_range

bp = Blueprint("groups", __name__)

TIMEZONES = ["Africa/Kampala", "Africa/Nairobi", "Africa/Dar_es_Salaam", "Africa/Kigali", "Africa/Lagos",
             "Africa/Accra", "Africa/Johannesburg", "Africa/Cairo", "UTC", "Europe/London", "America/New_York"]
FEED_TYPE_LABELS = {
    "CONTRIBUTION": "New contributions", "LOAN_APPROVED": "Loan approvals", "LOAN_DISBURSED": "Loan disbursements",
    "REPAYMENT": "Repayments received", "LOAN_REPAID": "Loans fully repaid", "MEMBER_APPROVED": "New members",
    "ANNOUNCEMENT": "Announcements", "MILESTONE": "Financial milestones",
}


def group_form_context(group=None):
    return {"currencies": CURRENCIES, "timezones": TIMEZONES, "group": group}


def _apply_group_fields(group, form, creating=False):
    name = (form.get("group_name") or form.get("name") or "").strip()
    if len(name) < 3:
        return "Enter a group name (at least 3 characters)."
    currency = (form.get("currency") or group.currency or "UGX").upper()
    if currency not in CURRENCIES:
        return "Select a valid currency."
    btype = (form.get("business_type") or "SAVINGS_GROUP") if creating else group.business_type
    if btype not in ("SAVINGS_GROUP", "LENDING"):
        return "Choose whether this is a savings group or a lending business."
    freq = form.get("contribution_frequency") or "MONTHLY"
    if btype == "LENDING":
        freq = "MONTHLY"
    if freq not in ("WEEKLY", "MONTHLY", "CUSTOM"):
        return "Select a valid contribution frequency."
    tz = form.get("timezone") or "Africa/Kampala"
    if tz not in TIMEZONES:
        return "Select a valid timezone."
    if not creating and currency != group.currency and Transaction.query.filter_by(group_id=group.id).first():
        return "The currency cannot be changed after transactions exist."
    try:
        dec = decimals_for(currency)
        default_contrib = to_minor(form.get("default_contribution") or "0", dec)
    except ValueError as e:
        return str(e)
    if creating:
        group.business_type = btype
    group.name, group.description = name, (form.get("description") or "").strip()[:1000] or None
    group.location = (form.get("location") or "").strip()[:120] or None
    group.phone = (form.get("group_phone") or form.get("contact_phone") or "").strip()[:30] or None
    group.email = (form.get("group_email") or form.get("contact_email") or "").strip()[:255] or None
    group.registration_number = (form.get("registration_number") or "").strip()[:60] or None
    group.currency, group.currency_decimals = currency, decimals_for(currency)
    group.contribution_frequency, group.default_contribution, group.timezone = freq, default_contrib, tz
    return None


def _apply_lender_settings(group, form):
    """Own rate cap and default late-payment penalty (lending businesses only)."""
    try:
        cap = (form.get("rate_cap") or "").strip()
        group.rate_cap_bp = parse_rate_bp(cap) if cap else None
        if group.rate_cap_bp is not None and group.rate_cap_bp <= 0:
            raise ValueError("Enter a rate cap above zero, or leave it empty.")
        ptype = form.get("penalty_type") or "NONE"
        if ptype not in ("NONE", "FLAT", "PERCENT"):
            raise ValueError("Select a valid penalty type.")
        raw = (form.get("penalty_value") or "0").strip() or "0"
        value = 0 if ptype == "NONE" else (to_minor(raw, group.currency_decimals) if ptype == "FLAT"
                                           else parse_rate_bp(raw))
        grace = int(form.get("penalty_grace_days") or 0)
        if not 0 <= grace <= 365:
            raise ValueError("Grace days must be between 0 and 365.")
    except ValueError as e:
        return str(e) or "Check the penalty and rate cap values."
    group.penalty_type, group.penalty_value, group.penalty_grace_days = ptype, value, grace
    group.pay_instructions = (form.get("pay_instructions") or "").strip()[:400] or None
    return None


def _save_logo(group, file_storage, user_id, square=True):
    """Group logos are images -> Uploadcare (metadata in Turso). The new logo is
    stored first; the previous one is retired only after that succeeds."""
    from flask import current_app
    old = db.session.get(StoredFile, group.logo_file_id) if group.logo_file_id else None
    sf = storage.store_upload(file_storage, group_id=group.id, user_id=user_id, kind="logo", images_only=True,
                              related=("group", group.id), square=square, max_side=512,
                              max_bytes=current_app.config["LOGO_MAX_BYTES"])
    group.logo_file_id = sf.id
    db.session.flush()
    if old:
        storage.delete_file(old, user_id, reason="replaced")


def create_group_from_form(user, form, logo_file=None):
    seed_defaults()
    group = Group(owner_id=user.id, name="x")
    err = _apply_group_fields(group, form, creating=True)
    if err:
        return None, err
    db.session.add(group)
    db.session.flush()
    db.session.add(GroupMember(group_id=group.id, user_id=user.id, role="ADMIN", status="ACTIVE",
                               approved_by=user.id, joined_at=utcnow()))
    if logo_file and logo_file.filename:
        try:
            _save_logo(group, logo_file, user.id)
        except storage.UploadError as e:
            return None, str(e)
    audit("group.created", "group", group.id, {"name": group.name}, group_id=group.id, user_id=user.id)
    return group, None


# ------------------------------------------------------------- selector / create
@bp.route("/groups")
@login_required
def select():
    if current_user.is_platform_admin:
        return redirect(url_for("platform.dashboard"))
    active = GroupMember.query.filter_by(user_id=current_user.id, status="ACTIVE").all()
    active = [m for m in active if m.group.status == "ACTIVE"]
    pending = GroupMember.query.filter_by(user_id=current_user.id, status="PENDING").all()
    if len(active) == 1 and not pending and not request.args.get("all"):
        return redirect(url_for("groups.dashboard", group_id=active[0].group_id))
    return render_template("groups/select.html", memberships=active, pending=pending)


@bp.route("/groups/new", methods=["GET", "POST"])
@login_required
def new():
    if current_user.is_platform_admin:
        abort(404)
    if request.method == "POST":
        group, err = create_group_from_form(current_user, request.form, request.files.get("logo"))
        if err:
            db.session.rollback()
            flash(err, "error")
        else:
            db.session.commit()
            flash("Group created. You are the Group Administrator.", "success")
            return redirect(url_for("groups.dashboard", group_id=group.id))
    return render_template("groups/new.html", **group_form_context())


# -------------------------------------------------------------------- dashboards
@bp.route("/g/<int:group_id>/")
@tenant_route()
def dashboard(group_id=None):
    group = g.group
    today = local_today(group.timezone)
    if group.is_lender:
        return lender_dashboard(group, today)
    start, end, key = resolve_range(request.args, group.timezone)
    summary = analytics.group_summary(group, today, start, end)
    charts = analytics.dashboard_charts(group, today)
    recent = GroupFeed.query.filter_by(group_id=group.id).order_by(GroupFeed.created_at.desc(), GroupFeed.id.desc()) \
        .limit(8).all()
    if not g.is_official:
        recent = [e for e in recent if e.event_type in group.visible_types][:5]
        return member_dashboard(summary, charts, recent, today)
    pending = MemberApplication.query.filter_by(group_id=group.id, status="PENDING").count()
    from app.services import fines as fine_svc, schedules as sched_svc
    unpaid = sched_svc.run_daily(group, today)
    db.session.commit()
    return render_template("groups/dashboard.html", s=summary, charts=charts, recent=recent, range_key=key,
                           start=start, end=end, pending=pending, unpaid_members=unpaid,
                           unpaid_fines=fine_svc.totals(group.id)["unpaid"])


def lender_dashboard(group, today):
    from app.models import Borrower, Loan
    from app.services import lender as lender_svc
    if not g.is_official:  # staff without finance rights go to the community instead of a savings dashboard
        return redirect(url_for("community.index"))
    lender_svc.accrue_penalties(group, today)
    start, end, key = resolve_range(request.args, group.timezone)
    summary = analytics.group_summary(group, today, start, end)
    charts = analytics.dashboard_charts(group, today)
    pf = loan_svc.portfolio(group, today)
    from datetime import timedelta
    due_soon = [l for l in Loan.query.filter(Loan.group_id == group.id, Loan.disbursement_tx_id.isnot(None)).all()
                if l.status in loan_svc.ACTIVE_STATES and (n := loan_svc.next_due(l)) is not None
                and today <= n.due_date <= today + timedelta(days=7)]
    recent = GroupFeed.query.filter_by(group_id=group.id).order_by(GroupFeed.created_at.desc(), GroupFeed.id.desc()) \
        .limit(8).all()
    pending = MemberApplication.query.filter_by(group_id=group.id, status="PENDING").count()
    awaiting = Loan.query.filter_by(group_id=group.id, status="PENDING_APPROVAL").count()
    return render_template("groups/dashboard_lender.html", s=summary, pf=pf, charts=charts, recent=recent,
                           range_key=key, pending=pending, awaiting=awaiting, due_soon=len(due_soon),
                           borrowers=Borrower.query.filter_by(group_id=group.id).count())


def member_dashboard(summary, charts, recent, today):
    from app.models import Announcement, Contribution
    mine = Contribution.query.filter_by(group_id=g.group.id, member_id=g.membership.id) \
        .order_by(Contribution.contribution_date.desc(), Contribution.id.desc())
    total_mine = ledger.member_savings(g.group.id, g.membership.id)
    board = analytics.leaderboard(g.group, today)
    my_rank = next((r["rank"] for r in board if r["member_id"] == g.membership.id), None)
    from app.services import schedules as sched_svc, shares as share_svc
    from app.services.groupcfg import get_settings
    row = sched_svc.get_schedule(g.group.id, g.membership.id)
    countdown = sched_svc.status_for(g.group, g.membership, row, today)
    sched_svc.run_daily(g.group, today)
    units = share_svc.member_units(g.group.id, g.membership.id)
    my_share_rank, _nx, _val = share_svc.rank_for(g.group.id, units)
    db.session.commit()
    announcements = Announcement.query.filter_by(group_id=g.group.id, status="ACTIVE", is_published=True) \
        .order_by(Announcement.is_pinned.desc(), Announcement.publish_date.desc()).limit(3).all()
    return render_template("groups/member_dashboard.html", s=summary, charts=charts, recent=recent,
                           contributions=mine.limit(6).all(), total_mine=int(total_mine or 0), my_rank=my_rank,
                           members_count=len(board), announcements=announcements, countdown=countdown,
                           share_units=units, my_share_rank=my_share_rank)


# ------------------------------------------------------------------ group settings
@bp.route("/g/<int:group_id>/settings", methods=["GET", "POST"])
@tenant_route(official=True)
def settings(group_id=None):
    group = g.group
    if request.method == "POST":
        err = _apply_group_fields(group, request.form)
        if not err:
            try:
                approvals = int(request.form.get("loan_approvals_required") or 1)
                if approvals not in (1, 2, 3):
                    raise ValueError
                group.loan_approvals_required = approvals
            except ValueError:
                err = "Loan approvals required must be 1, 2 or 3."
        if not err and group.is_lender:
            err = _apply_lender_settings(group, request.form)
        if err:
            db.session.rollback()
            flash(err, "error")
        else:
            chosen = [t for t in request.form.getlist("feed_types") if t in FEED_EVENT_TYPES]
            group.feed_visible_types = ",".join(chosen)
            audit("group.settings_updated", "group", group.id, group_id=group.id)
            db.session.commit()
            flash("Group settings saved.", "success")
            return redirect(url_for("groups.settings"))
    officials = GroupMember.query.filter(GroupMember.group_id == group.id, GroupMember.status == "ACTIVE",
                                         GroupMember.role.in_(["ADMIN", "TREASURER", "VICE_CHAIR"])).all()
    return render_template("groups/settings.html", **group_form_context(group), feed_labels=FEED_TYPE_LABELS,
                           officials=officials, platform_cap=loan_svc.platform_lender_cap())


@bp.route("/g/<int:group_id>/settings/logo", methods=["POST"])
@tenant_route(official=True)
def upload_logo(group_id=None):
    f = request.files.get("logo")
    if not f or not f.filename:
        flash("Choose an image to upload.", "error")
        return redirect(url_for("groups.settings"))
    try:
        _save_logo(g.group, f, current_user.id, square=bool(request.form.get("square", "1") == "1"))
    except storage.UploadError as e:
        flash(str(e), "error")
        return redirect(url_for("groups.settings"))
    audit("group.logo_changed", "group", g.group.id, group_id=g.group.id)
    db.session.commit()
    flash("Group logo updated.", "success")
    return redirect(url_for("groups.settings"))


@bp.route("/g/<int:group_id>/settings/logo/remove", methods=["POST"])
@tenant_route(official=True)
def remove_logo(group_id=None):
    if g.group.logo_file_id:
        old = db.session.get(StoredFile, g.group.logo_file_id)
        g.group.logo_file_id = None
        db.session.flush()
        storage.delete_file(old, current_user.id, reason="logo removed")
        audit("group.logo_removed", "group", g.group.id, group_id=g.group.id)
        db.session.commit()
    flash("Logo removed. The default group icon is now shown.", "success")
    return redirect(url_for("groups.settings"))


@bp.route("/g/<int:group_id>/settings/transfer-owner", methods=["POST"])
@tenant_route(official=True)
def transfer_owner(group_id=None):
    if g.group.owner_id != current_user.id:
        abort(403)
    target = GroupMember.query.filter_by(id=request.form.get("member_id", type=int), group_id=g.group.id,
                                         status="ACTIVE").first()
    if not target or target.user_id == current_user.id:
        flash("Select another active member.", "error")
        return redirect(url_for("groups.settings"))
    target.role = "ADMIN"
    g.group.owner_id = target.user_id
    audit("group.ownership_transferred", "group", g.group.id, {"to_user": target.user_id}, group_id=g.group.id)
    db.session.commit()
    flash("Group ownership transferred.", "success")
    return redirect(url_for("groups.settings"))


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        from app.auth import normalize_phone, valid_phone
        from app.models import User
        name = (request.form.get("full_name") or "").strip()
        phone = normalize_phone(request.form.get("phone"))
        email = (request.form.get("email") or "").strip().lower() or None
        if len(name) < 2 or not valid_phone(phone):
            flash("Enter your name and a valid phone number.", "error")
        elif User.query.filter(User.phone == phone, User.id != current_user.id).first() or \
                (email and User.query.filter(User.email == email, User.id != current_user.id).first()):
            flash("That phone number or email is already in use.", "error")
        else:
            current_user.full_name, current_user.phone, current_user.email = name, phone, email
            audit("user.profile_updated", "user", current_user.id)
            db.session.commit()
            flash("Profile updated.", "success")
        return redirect(url_for("groups.profile"))
    memberships = GroupMember.query.filter_by(user_id=current_user.id).all()
    return render_template("groups/profile.html", memberships=memberships)
