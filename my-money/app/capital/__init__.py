"""Owner and investor capital for lending businesses. Capital in and withdrawals are real ledger
entries (so cash and every report stay correct) and are tracked per investor here."""
from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db
from app.models import CapitalEntry, Investor, Transaction
from app.services import ledger
from app.services.audit import audit
from app.services.forms import form_amount, form_choice, form_date, form_text
from app.services.tenant import get_scoped_or_404, scoped, tenant_route
from app.services.timeutil import local_today

bp = Blueprint("capital", __name__, url_prefix="/g/<int:group_id>/capital")


def _position(entries):
    """Net capital per investor id (None = owner), ignoring reversed entries."""
    out = {}
    for e in entries:
        if e.reversed:
            continue
        out[e.investor_id] = out.get(e.investor_id, 0) + (e.amount if e.kind == "CAPITAL_IN" else -e.amount)
    return out


@bp.route("/")
@tenant_route(official=True, kind="LENDING")
def index(group_id=None):
    entries = scoped(CapitalEntry).order_by(CapitalEntry.entry_date.desc(), CapitalEntry.id.desc()).all()
    investors = scoped(Investor).order_by(Investor.name).all()
    pos = _position(entries)
    total_in = sum(e.amount for e in entries if e.kind == "CAPITAL_IN" and not e.reversed)
    total_out = sum(e.amount for e in entries if e.kind == "WITHDRAWAL" and not e.reversed)
    return render_template("capital/index.html", entries=entries, investors=investors, pos=pos, total_in=total_in,
                           total_out=total_out, today=local_today(g.group.timezone),
                           cash=ledger.cash_balance(g.group.id))


@bp.route("/investors", methods=["POST"])
@tenant_route(official=True, kind="LENDING")
def add_investor(group_id=None):
    name = (request.form.get("name") or "").strip()
    if len(name) < 2:
        flash("Enter the investor's name.", "error")
    else:
        db.session.add(Investor(group_id=g.group.id, name=name[:150], phone=form_text("phone", 30),
                                notes=form_text("notes", 300), created_by=current_user.id))
        audit("investor.created", "investor", None, {"name": name}, group_id=g.group.id)
        db.session.commit()
        flash("Investor added.", "success")
    return redirect(url_for("capital.index"))


@bp.route("/entries", methods=["POST"])
@tenant_route(official=True, kind="LENDING")
def add_entry(group_id=None):
    try:
        kind = form_choice("kind", ("CAPITAL_IN", "WITHDRAWAL"), "entry type")
        amount, when = form_amount(), form_date("entry_date")
        if amount <= 0:
            raise ValueError("Enter an amount greater than zero.")
        inv = None
        if request.form.get("investor_id"):
            inv = scoped(Investor, id=request.form.get("investor_id", type=int)).first()
            if inv is None:
                raise ValueError("Select an investor from this business.")
        who = inv.name if inv else "Owner"
        direction = "IN" if kind == "CAPITAL_IN" else "OUT"
        if direction == "OUT" and amount > ledger.cash_balance(g.group.id):
            raise ValueError("Insufficient available cash for this withdrawal.")
        pos = _position(scoped(CapitalEntry).all()).get(inv.id if inv else None, 0)
        if direction == "OUT" and amount > pos:
            raise ValueError(f"{who} has only {pos:,} of capital in the business to withdraw.")
        tx = ledger.post(g.group.id, "ADJUSTMENT", amount, direction, when, current_user.id,
                         description=f"{'Capital from' if direction == 'IN' else 'Capital withdrawn by'} {who}",
                         prefix="CAP")
        e = CapitalEntry(group_id=g.group.id, investor_id=inv.id if inv else None, kind=kind, amount=amount,
                         entry_date=when, note=form_text("note", 300), tx_id=tx.id, created_by=current_user.id)
        db.session.add(e)
        db.session.flush()
        audit(f"capital.{kind.lower()}", "capital_entry", e.id, {"amount": amount, "who": who}, group_id=g.group.id)
        db.session.commit()
        flash("Recorded.", "success")
    except (ValueError, ledger.LedgerError) as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("capital.index"))


@bp.route("/entries/<int:entry_id>/reverse", methods=["POST"])
@tenant_route(official=True, kind="LENDING")
def reverse_entry(entry_id, group_id=None):
    e = get_scoped_or_404(CapitalEntry, entry_id)
    reason = (request.form.get("reason") or "").strip()
    try:
        if e.reversed:
            raise ValueError("Already reversed.")
        if e.kind == "CAPITAL_IN" and e.amount > ledger.cash_balance(g.group.id):
            raise ValueError("Not enough cash to reverse this capital entry.")
        ledger.reverse(db.session.get(Transaction, e.tx_id), current_user.id, reason, local_today(g.group.timezone))
        e.reversed = True
        audit("capital.reversed", "capital_entry", e.id, {"reason": reason}, group_id=g.group.id)
        db.session.commit()
        flash("Entry reversed.", "success")
    except (ValueError, ledger.LedgerError) as err:
        db.session.rollback()
        flash(str(err), "error")
    return redirect(url_for("capital.index"))
