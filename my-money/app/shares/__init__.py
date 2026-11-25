from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func

from app.extensions import db
from app.models import GroupMember, ShareEntry, ShareRank, ShareTransfer, User
from app.services import receipts as receipts_svc, shares as share_svc
from app.services.audit import audit
from app.services.forms import form_amount, form_date, form_text
from app.services.groupcfg import get_settings
from app.services.money import fmt, to_minor
from app.services.shares import RANK_ICONS, ShareError
from app.services.tenant import get_scoped_or_404, tenant_route
from app.services.timeutil import local_today

bp = Blueprint("shares", __name__, url_prefix="/g/<int:group_id>/shares")


def _members():
    return GroupMember.query.join(User, User.id == GroupMember.user_id).filter(
        GroupMember.group_id == g.group.id, GroupMember.status == "ACTIVE").order_by(User.full_name).all()


def _units(request_key):
    raw = (request.form.get(request_key) or "").strip().replace(",", "")
    if not raw.isdigit() or int(raw) <= 0:
        raise ShareError("Enter a whole number of shares above zero.")
    return int(raw)


@bp.route("/")
@tenant_route(official=True)
def index(group_id=None):
    group, today = g.group, local_today(g.group.timezone)
    s = get_settings(group.id)
    held = share_svc.holdings(group.id)
    ranks = share_svc.ensure_ranks(group.id)
    rows = []
    for m in _members():
        units = held.get(m.id, 0)
        cur, nxt, val = share_svc.rank_for(group.id, units, ranks)
        rows.append({"m": m, "units": units, "value": val, "rank": cur})
    rows.sort(key=lambda r: (-r["units"], r["m"].user.full_name.lower()))
    total_units = sum(held.values())
    month_key = func.strftime("%Y-%m", ShareEntry.entry_date)
    series = dict(db.session.query(month_key, func.coalesce(func.sum(ShareEntry.units), 0)).filter(
        ShareEntry.group_id == group.id, ShareEntry.kind == "PURCHASE", ShareEntry.reversed_at.is_(None))
        .group_by(month_key).all())
    labels, y, m = [], today.year, today.month
    for _ in range(6):
        labels.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    labels.reverse()
    recent = ShareEntry.query.filter_by(group_id=group.id, kind="PURCHASE").order_by(
        ShareEntry.created_at.desc(), ShareEntry.id.desc()).limit(8).all()
    pending = ShareTransfer.query.filter_by(group_id=group.id, status="PENDING").count()
    return render_template("shares/index.html", rows=rows, s=s, total_units=total_units,
                           total_value=total_units * s.share_price, holders=sum(1 for v in held.values() if v > 0),
                           labels=labels, bought=[int(series.get(k, 0)) for k in labels],
                           dist=share_svc.rank_distribution(group.id), recent=recent, pending=pending)


@bp.route("/new", methods=["GET", "POST"])
@tenant_route(official=True)
def new(group_id=None):
    s = get_settings(g.group.id)
    if request.method == "POST":
        try:
            member = GroupMember.query.filter_by(id=request.form.get("member_id", type=int), group_id=g.group.id,
                                                 status="ACTIVE").first()
            if member is None:
                raise ShareError("Select an active member.")
            price_raw = (request.form.get("unit_price") or "").strip()
            price = to_minor(price_raw, g.group.currency_decimals) if price_raw else None
            left = 0
            if request.form.get("mode", "amount") == "amount":       # enter the money, the app works out the shares
                paid = form_amount("amount")
                e, left = share_svc.record_purchase_by_amount(g.group, current_user, member, paid,
                                                              form_date("entry_date"), price, form_text("note"),
                                                              form_text("payment_method", 40))
            else:
                e = share_svc.record_purchase(g.group, current_user, member, _units("units"), form_date("entry_date"),
                                              price, form_text("note"), form_text("payment_method", 40))
            audit("shares.purchased", "share_entry", e.id, {"units": e.units, "amount": e.amount, "left_over": left},
                  group_id=g.group.id)
            db.session.commit()
            msg = f"{e.units} share(s) recorded for {fmt(e.amount, g.group.currency, g.group.currency_decimals)}. Receipt {e.receipt_no}."
            if left:
                msg += (f" {fmt(left, g.group.currency, g.group.currency_decimals)} was left over (less than one share) "
                        "and was not recorded as shares.")
            flash(msg, "warning" if left else "success")
            receipts_svc.offer(g.group, "share", e.id)
            return redirect(url_for("shares.index"))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("shares/new.html", members=_members(), s=s, today=local_today(g.group.timezone),
                           preselect=request.args.get("member", type=int))


@bp.route("/<int:entry_id>/reverse", methods=["POST"])
@tenant_route(official=True)
def reverse(entry_id, group_id=None):
    e = get_scoped_or_404(ShareEntry, entry_id)
    try:
        share_svc.reverse_purchase(g.group, e, current_user, request.form.get("reason") or "",
                                   local_today(g.group.timezone))
        audit("shares.reversed", "share_entry", e.id, {"reason": request.form.get("reason")}, group_id=g.group.id)
        db.session.commit()
        flash("Share purchase reversed.", "success")
    except ValueError as err:
        db.session.rollback()
        flash(str(err), "error")
    return redirect(request.referrer or url_for("shares.index"))


@bp.route("/me")
@tenant_route()
def mine(group_id=None):
    group = g.group
    units = share_svc.member_units(group.id, g.membership.id)
    cur, nxt, val = share_svc.rank_for(group.id, units)
    entries = ShareEntry.query.filter_by(group_id=group.id, member_id=g.membership.id).order_by(
        ShareEntry.entry_date.desc(), ShareEntry.id.desc()).limit(40).all()
    transfers = ShareTransfer.query.filter(
        ShareTransfer.group_id == group.id,
        (ShareTransfer.seller_id == g.membership.id) | (ShareTransfer.buyer_id == g.membership.id)).order_by(
        ShareTransfer.created_at.desc()).limit(20).all()
    pct = 100
    if nxt:
        base = cur.min_value if cur else 0
        pct = max(0, min(100, int((val - base) * 100 / max(1, nxt.min_value - base))))
    return render_template("shares/mine.html", units=units, value=val, cur=cur, nxt=nxt, pct=pct, entries=entries,
                           transfers=transfers, s=get_settings(group.id),
                           ladder=share_svc.ensure_ranks(group.id))


@bp.route("/leaderboard")
@tenant_route()
def leaderboard(group_id=None):
    s = get_settings(g.group.id)
    if not s.leaderboard_on:
        return render_template("shares/leaderboard.html", rows=[], off=True, s=s,
                               ladder=share_svc.ensure_ranks(g.group.id))
    return render_template("shares/leaderboard.html", rows=share_svc.leaderboard(g.group.id), off=False, s=s,
                           ladder=share_svc.ensure_ranks(g.group.id), me=g.membership.id)


@bp.route("/transfers")
@tenant_route(official=True)
def transfers(group_id=None):
    q = ShareTransfer.query.filter_by(group_id=g.group.id).order_by(ShareTransfer.created_at.desc())
    page = q.paginate(page=request.args.get("page", 1, type=int), per_page=20, error_out=False)
    return render_template("shares/transfers.html", page=page)


@bp.route("/transfer/new", methods=["GET", "POST"])
@tenant_route()
def transfer_new(group_id=None):
    members = _members()
    held = share_svc.holdings(g.group.id)
    if request.method == "POST":
        try:
            seller_id = request.form.get("seller_id", type=int) if g.is_official else g.membership.id
            seller = GroupMember.query.filter_by(id=seller_id, group_id=g.group.id, status="ACTIVE").first()
            buyer = GroupMember.query.filter_by(id=request.form.get("buyer_id", type=int), group_id=g.group.id,
                                                status="ACTIVE").first()
            price_raw = (request.form.get("unit_price") or "").strip()
            price = to_minor(price_raw, g.group.currency_decimals) if price_raw else get_settings(g.group.id).share_price
            t = share_svc.request_transfer(g.group, current_user, seller, buyer, _units("units"), price,
                                           form_text("note"))
            audit("shares.transfer_requested", "share_transfer", t.id, {"units": t.units}, group_id=g.group.id)
            db.session.commit()
            flash("Transfer request sent. An official will review it.", "success")
            return redirect(url_for("shares.mine"))
        except ValueError as err:
            db.session.rollback()
            flash(str(err), "error")
    return render_template("shares/transfer_new.html", members=members, held=held, s=get_settings(g.group.id),
                           mine=g.membership)


@bp.route("/transfers/<int:transfer_id>/decide", methods=["POST"])
@tenant_route(official=True)
def transfer_decide(transfer_id, group_id=None):
    t = get_scoped_or_404(ShareTransfer, transfer_id)
    approve = request.form.get("decision") == "approve"
    try:
        share_svc.decide_transfer(g.group, t, current_user, approve, request.form.get("note"),
                                  local_today(g.group.timezone))
        audit("shares.transfer_" + ("approved" if approve else "rejected"), "share_transfer", t.id,
              group_id=g.group.id)
        db.session.commit()
        flash("Transfer approved." if approve else "Transfer declined.", "success")
        if approve:
            receipts_svc.offer(g.group, "share_transfer", t.id)
    except ValueError as err:
        db.session.rollback()
        flash(str(err), "error")
    return redirect(url_for("shares.transfers"))


@bp.route("/transfers/<int:transfer_id>/cancel", methods=["POST"])
@tenant_route()
def transfer_cancel(transfer_id, group_id=None):
    t = get_scoped_or_404(ShareTransfer, transfer_id)
    if t.status != "PENDING" or (t.requested_by != current_user.id and not g.is_official):
        abort(403)
    t.status = "CANCELLED"
    db.session.commit()
    flash("Transfer request cancelled.", "success")
    return redirect(request.referrer or url_for("shares.mine"))


@bp.route("/ranks", methods=["GET", "POST"])
@tenant_route(official=True)
def ranks(group_id=None):
    ladder = share_svc.ensure_ranks(g.group.id)
    if request.method == "POST":
        try:
            names = request.form.getlist("name")
            mins = request.form.getlist("min_value")
            icons = request.form.getlist("icon")
            clean = []
            for n, mv, ic in zip(names, mins, icons):
                n = (n or "").strip()[:40]
                if not n:
                    continue
                clean.append((n, to_minor(mv or "0", g.group.currency_decimals), ic if ic in RANK_ICONS else "star"))
            if not clean:
                raise ValueError("Keep at least one rank.")
            if len({c[1] for c in clean}) != len(clean):
                raise ValueError("Two ranks cannot need the same amount.")
            ShareRank.query.filter_by(group_id=g.group.id).delete()
            for n, mv, ic in clean:
                db.session.add(ShareRank(group_id=g.group.id, name=n, min_value=mv, icon=ic))
            audit("shares.ranks_changed", "group", g.group.id, group_id=g.group.id)
            db.session.commit()
            flash("Rank ladder saved.", "success")
            return redirect(url_for("shares.ranks"))
        except ValueError as err:
            db.session.rollback()
            flash(str(err), "error")
    return render_template("shares/ranks.html", ladder=ladder, icons=RANK_ICONS)
