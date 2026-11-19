from datetime import timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func

from app.extensions import db
from app.models import Contribution, GroupMember, SavingsWithdrawal, Transaction, User
from app.services import analytics, group_savings, ledger, savings as savings_svc
from app.services.audit import audit
from app.services.forms import form_amount, form_choice, form_date, form_text, new_idem_key
from app.services.savings import CATEGORY_LABELS, PAYMENT_METHODS, DuplicateError
from app.services.groupcfg import get_settings
from app.services.tenant import get_scoped_or_404, scoped, tenant_route
from app.services.timeutil import local_today, resolve_range

bp = Blueprint("savings", __name__, url_prefix="/g/<int:group_id>/savings")


def _reversed_ids(tx_ids):
    if not tx_ids:
        return set()
    rows = db.session.query(Transaction.reverses_id).filter(
        Transaction.group_id == g.group.id, Transaction.reverses_id.in_(tx_ids)).all()
    return {r[0] for r in rows}


@bp.route("/")
@tenant_route(official=True, kind="SAVINGS_GROUP")
def index(group_id=None):
    group, today = g.group, local_today(g.group.timezone)
    start, end, key = resolve_range(request.args, group.timezone)
    q = Contribution.query.join(GroupMember, GroupMember.id == Contribution.member_id) \
        .join(User, User.id == GroupMember.user_id).filter(Contribution.group_id == group.id)
    if start:
        q = q.filter(Contribution.contribution_date >= start)
    if end:
        q = q.filter(Contribution.contribution_date <= end)
    member_id = request.args.get("member", type=int)
    if member_id:
        q = q.filter(Contribution.member_id == member_id)
    cat = request.args.get("category")
    if cat in CATEGORY_LABELS:
        q = q.filter(Contribution.category == cat)
    term = (request.args.get("q") or "").strip()
    if term:
        like = f"%{term}%"
        q = q.filter(User.full_name.ilike(like) | Contribution.payment_reference.ilike(like)
                     | Contribution.receipt_no.ilike(like))
    page = q.order_by(Contribution.contribution_date.desc(), Contribution.id.desc()) \
        .paginate(page=request.args.get("page", 1, type=int), per_page=15, error_out=False)
    month_start = today.replace(day=1)
    month = ledger.totals(group.id, month_start, today)["total_savings"]
    year = ledger.totals(group.id, today.replace(month=1, day=1), today)["total_savings"]
    contributed, total_members = analytics.members_contributed(group, month_start, today)
    charts = analytics.dashboard_charts(group, today)
    members = GroupMember.query.join(User, User.id == GroupMember.user_id).filter(GroupMember.group_id == group.id, GroupMember.status == "ACTIVE") \
        .order_by(User.full_name).all()
    recent = Contribution.query.filter_by(group_id=group.id) \
        .order_by(Contribution.created_at.desc(), Contribution.id.desc()).limit(5).all()
    expected = sum(savings_svc.expected_for_member(group, m, today) for m in members)
    actual_all = ledger.totals(group.id)["total_savings"]
    return render_template(
        "savings/index.html", page=page, month=month, year=year, contributed=contributed,
        total_members=total_members, charts=charts, members=members, recent=recent, range_key=key,
        start=start, end=end, q=term, member_id=member_id, category=cat, reversed_ids=_reversed_ids([c.tx_id for c in page.items]),
        categories=CATEGORY_LABELS, methods=PAYMENT_METHODS, idem=new_idem_key(), today=today,
        expected=expected, actual_all=actual_all, missed=max(0, expected - actual_all))


@bp.route("/new", methods=["GET", "POST"])
@tenant_route(official=True, kind="SAVINGS_GROUP")
def new(group_id=None):
    members = GroupMember.query.join(User, User.id == GroupMember.user_id).filter(GroupMember.group_id == g.group.id, GroupMember.status == "ACTIVE") \
        .order_by(User.full_name).all()
    if request.method == "POST":
        try:
            member = GroupMember.query.filter_by(id=request.form.get("member_id", type=int), group_id=g.group.id,
                                                 status="ACTIVE").first()
            if member is None:
                raise ValueError("Select an active member.")
            c = savings_svc.record_contribution(
                g.group, current_user, member, form_amount(), form_choice("category", CATEGORY_LABELS, "category"),
                form_date("contribution_date"), form_text("payment_method", 40), form_text("payment_reference", 80),
                form_text("notes"), request.form.get("idempotency_key") or None)
            audit("contribution.recorded", "contribution", c.id, {"amount": c.amount}, group_id=g.group.id)
            db.session.commit()
            flash(f"Contribution recorded. Receipt {c.receipt_no}.", "success")
            return redirect(url_for("savings.index"))
        except DuplicateError as e:
            db.session.rollback()
            flash(str(e), "info")
            return redirect(url_for("savings.index"))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("savings/new.html", members=members, categories=CATEGORY_LABELS, methods=PAYMENT_METHODS,
                           idem=new_idem_key(), today=local_today(g.group.timezone),
                           preselect=request.args.get("member", type=int))


@bp.route("/<int:contribution_id>/reverse", methods=["POST"])
@tenant_route(official=True, kind="SAVINGS_GROUP")
def reverse(contribution_id, group_id=None):
    c = get_scoped_or_404(Contribution, contribution_id)
    try:
        savings_svc.reverse_contribution(g.group, c, current_user, request.form.get("reason") or "",
                                         local_today(g.group.timezone))
        audit("contribution.reversed", "contribution", c.id, {"reason": request.form.get("reason")},
              group_id=g.group.id)
        db.session.commit()
        flash("Contribution reversed. A reversal entry was added to the ledger.", "success")
    except ledger.LedgerError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(request.referrer or url_for("savings.index"))


@bp.route("/group")
@tenant_route(kind="SAVINGS_GROUP")
def overview(group_id=None):
    """The Savings button: the group's total savings and everyone's contributions. Open to every active member
    of the group, administrators and ordinary members alike."""
    group, today = g.group, local_today(g.group.timezone)
    view = request.args.get("view", "members")
    if view not in ("members", "all"):
        view = "members"
    data = group_savings.breakdown(group)
    month_start = today.replace(day=1)
    month = ledger.totals(group.id, month_start, today)["total_savings"]
    contributed, active_members = analytics.members_contributed(group, month_start, today)
    mine = next((r for r in data["rows"] if r["member"].id == g.membership.id), None)
    ctx = dict(view=view, data=data, month=month, contributed=contributed, active_members=active_members,
               mine=mine, today=today, categories=CATEGORY_LABELS, page=None, selected=None, q="", reversed_ids=set())
    if view == "all":
        q = Contribution.query.join(GroupMember, GroupMember.id == Contribution.member_id) \
            .join(User, User.id == GroupMember.user_id).filter(Contribution.group_id == group.id)
        member_id = request.args.get("member", type=int)
        selected = next((r for r in data["rows"] if r["member"].id == member_id), None) if member_id else None
        if selected:
            q = q.filter(Contribution.member_id == member_id)
        term = (request.args.get("q") or "").strip()
        if term:
            q = q.filter(User.full_name.ilike(f"%{term}%"))
        page = q.order_by(Contribution.contribution_date.desc(), Contribution.id.desc()) \
            .paginate(page=request.args.get("page", 1, type=int), per_page=20, error_out=False)
        ctx.update(page=page, selected=selected, q=term, reversed_ids=_reversed_ids([c.tx_id for c in page.items]))
    return render_template("savings/overview.html", **ctx)


@bp.route("/me")
@tenant_route(kind="SAVINGS_GROUP")
def mine(group_id=None):
    q = Contribution.query.filter_by(group_id=g.group.id, member_id=g.membership.id)
    page = q.order_by(Contribution.contribution_date.desc(), Contribution.id.desc()) \
        .paginate(page=request.args.get("page", 1, type=int), per_page=15, error_out=False)
    today = local_today(g.group.timezone)
    wds = SavingsWithdrawal.query.filter_by(group_id=g.group.id, member_id=g.membership.id).order_by(
        SavingsWithdrawal.created_at.desc()).limit(10).all()
    return render_template("savings/mine.html", page=page, total=ledger.member_savings(g.group.id, g.membership.id),
                           reversed_ids=_reversed_ids([c.tx_id for c in page.items]),
                           expected=savings_svc.expected_for_member(g.group, g.membership, today),
                           available=savings_svc.available_to_withdraw(g.group, g.membership),
                           minimum=get_settings(g.group.id).min_savings_balance, withdrawals=wds)


@bp.route("/member/<int:member_id>")
@tenant_route(official=True, kind="SAVINGS_GROUP")
def member_statement(member_id, group_id=None):
    m = get_scoped_or_404(GroupMember, member_id)
    q = Contribution.query.filter_by(group_id=g.group.id, member_id=m.id) \
        .order_by(Contribution.contribution_date.desc(), Contribution.id.desc())
    page = q.paginate(page=request.args.get("page", 1, type=int), per_page=20, error_out=False)
    return render_template("savings/member.html", m=m, page=page, total=ledger.member_savings(g.group.id, m.id),
                           reversed_ids=_reversed_ids([c.tx_id for c in page.items]),
                           expected=savings_svc.expected_for_member(g.group, m, local_today(g.group.timezone)))


# ------------------------------------------------------------------ withdrawals
@bp.route("/withdraw", methods=["GET", "POST"])
@tenant_route()
def withdraw(group_id=None):
    members = GroupMember.query.join(User, User.id == GroupMember.user_id).filter(
        GroupMember.group_id == g.group.id, GroupMember.status == "ACTIVE").order_by(User.full_name).all() \
        if g.is_official else [g.membership]
    if request.method == "POST":
        try:
            member = next((m for m in members if m.id == (request.form.get("member_id", type=int) or g.membership.id)),
                          None)
            if member is None:
                raise ValueError("Select a member.")
            w = savings_svc.request_withdrawal(g.group, current_user, member, form_amount(), form_text("note"))
            audit("savings.withdrawal_requested", "savings_withdrawal", w.id, {"amount": w.amount}, group_id=g.group.id)
            db.session.commit()
            flash("Withdrawal request sent. An official will review it." if not g.is_official or
                  member.id == g.membership.id else "Withdrawal request created. Approve it from Withdrawals.",
                  "success")
            return redirect(url_for("savings.withdrawals") if g.is_official else url_for("savings.mine"))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")
    avail = {m.id: savings_svc.available_to_withdraw(g.group, m) for m in members}
    return render_template("savings/withdraw.html", members=members, avail=avail, mine=g.membership,
                           minimum=get_settings(g.group.id).min_savings_balance)


@bp.route("/withdrawals")
@tenant_route(official=True)
def withdrawals(group_id=None):
    q = SavingsWithdrawal.query.filter_by(group_id=g.group.id).order_by(
        (SavingsWithdrawal.status == "PENDING").desc(), SavingsWithdrawal.created_at.desc())
    page = q.paginate(page=request.args.get("page", 1, type=int), per_page=20, error_out=False)
    t = ledger.totals(g.group.id)
    return render_template("savings/withdrawals.html", page=page, methods=PAYMENT_METHODS, withdrawn=t["withdrawals"],
                           minimum=get_settings(g.group.id).min_savings_balance)


@bp.route("/withdrawals/<int:wid>/decide", methods=["POST"])
@tenant_route(official=True)
def withdrawal_decide(wid, group_id=None):
    w = get_scoped_or_404(SavingsWithdrawal, wid)
    try:
        today = local_today(g.group.timezone)
        action = request.form.get("decision")
        if action == "approve":
            savings_svc.approve_withdrawal(g.group, w, current_user, today, form_text("payment_method", 40))
        elif action == "reject":
            savings_svc.reject_withdrawal(g.group, w, current_user, form_text("note"))
        elif action == "reverse":
            savings_svc.reverse_withdrawal(g.group, w, current_user, request.form.get("reason") or "", today)
        else:
            raise ValueError("Choose an action.")
        audit(f"savings.withdrawal_{action}", "savings_withdrawal", w.id, group_id=g.group.id)
        db.session.commit()
        flash("Done.", "success")
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("savings.withdrawals"))


@bp.route("/withdrawals/<int:wid>/cancel", methods=["POST"])
@tenant_route()
def withdrawal_cancel(wid, group_id=None):
    w = get_scoped_or_404(SavingsWithdrawal, wid)
    if w.requested_by != current_user.id and not g.is_official:
        abort(403)
    try:
        savings_svc.cancel_withdrawal(g.group, w, current_user)
        db.session.commit()
        flash("Request cancelled.", "success")
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(request.referrer or url_for("savings.mine"))
