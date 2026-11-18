from datetime import timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func

from app.extensions import db
from app.models import Borrower, Loan, LoanFine, LoanPromise
from app.services import fines as fine_svc
from app.services.audit import audit
from app.services.forms import form_amount, form_date, form_text
from app.services.groupcfg import get_settings
from app.services.loans import ACTIVE_STATES
from app.services.money import to_minor
from app.services.savings import PAYMENT_METHODS
from app.services.tenant import get_scoped_or_404, tenant_route
from app.services.timeutil import local_today

bp = Blueprint("fines", __name__, url_prefix="/g/<int:group_id>/fines")


def _months(today, n=6):
    keys, y, m = [], today.year, today.month
    for _ in range(n):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(keys))


@bp.route("/")
@tenant_route(official=True)
def index(group_id=None):
    group, today = g.group, local_today(g.group.timezone)
    status = request.args.get("status")
    q = LoanFine.query.filter_by(group_id=group.id)
    if status in ("UNPAID", "PAID", "WAIVED", "VOID"):
        q = q.filter(LoanFine.status == status)
    page = q.order_by(LoanFine.created_at.desc(), LoanFine.id.desc()).paginate(
        page=request.args.get("page", 1, type=int), per_page=15, error_out=False)
    month = func.strftime("%Y-%m", LoanFine.paid_on)
    series = dict(db.session.query(month, func.coalesce(func.sum(LoanFine.amount), 0)).filter(
        LoanFine.group_id == group.id, LoanFine.status.in_(["UNPAID", "PAID"])).group_by(month).all())
    labels = _months(today)
    by_b = db.session.query(Borrower.name, func.sum(LoanFine.amount)).join(
        LoanFine, LoanFine.borrower_id == Borrower.id).filter(
        LoanFine.group_id == group.id, LoanFine.status.in_(["UNPAID", "PAID"])).group_by(Borrower.id, Borrower.name) \
        .order_by(func.sum(LoanFine.amount).desc()).limit(6).all()
    loans = Loan.query.filter(Loan.group_id == group.id, Loan.status.in_(list(ACTIVE_STATES)),
                              Loan.disbursement_tx_id.isnot(None)).all()
    return render_template("fines/index.html", page=page, status=status, t=fine_svc.totals(group.id),
                           overdue=fine_svc.overdue_now(group, today), labels=labels,
                           monthly=[int(series.get(k, 0)) for k in labels],
                           borrowers=[b[0] for b in by_b], b_amounts=[int(b[1]) for b in by_b],
                           methods=PAYMENT_METHODS, s=get_settings(group.id), loans=loans, today=today,
                           promises=LoanPromise.query.filter_by(group_id=group.id, status="OPEN")
                           .order_by(LoanPromise.promise_date).all())


@bp.route("/<int:fine_id>/settle", methods=["POST"])
@tenant_route(official=True)
def settle(fine_id, group_id=None):
    f = get_scoped_or_404(LoanFine, fine_id)
    try:
        fine_svc.settle(g.group, f, current_user, local_today(g.group.timezone), form_text("payment_method", 40))
        audit("fine.collected", "loan_fine", f.id, {"amount": f.amount}, group_id=g.group.id)
        db.session.commit()
        flash("Fine collected and added to group income.", "success")
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(request.referrer or url_for("fines.index"))


@bp.route("/<int:fine_id>/waive", methods=["POST"])
@tenant_route(official=True)
def waive(fine_id, group_id=None):
    f = get_scoped_or_404(LoanFine, fine_id)
    try:
        raw = (request.form.get("new_amount") or "").strip()
        new = to_minor(raw, g.group.currency_decimals) if raw else None
        fine_svc.waive(g.group, f, current_user, request.form.get("reason") or "", new)
        audit("fine.waived", "loan_fine", f.id, {"reason": request.form.get("reason")}, group_id=g.group.id)
        db.session.commit()
        flash("Fine updated.", "success")
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(request.referrer or url_for("fines.index"))


@bp.route("/promise", methods=["POST"])
@tenant_route(official=True)
def promise():
    try:
        loan = get_scoped_or_404(Loan, request.form.get("loan_id", type=int) or 0)
        if loan.status not in ACTIVE_STATES:
            raise ValueError("Promises can only be recorded on active loans.")
        p = LoanPromise(group_id=g.group.id, loan_id=loan.id, promise_date=form_date("promise_date"),
                        amount=form_amount(), note=form_text("note"), created_by=current_user.id)
        db.session.add(p)
        db.session.flush()
        audit("loan.promise_added", "loan_promise", p.id, {"date": str(p.promise_date)}, group_id=g.group.id)
        db.session.commit()
        flash("Promise recorded. The fine deadline is now that date.", "success")
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(request.referrer or url_for("fines.index"))


@bp.route("/promise/<int:promise_id>/cancel", methods=["POST"])
@tenant_route(official=True)
def promise_cancel(promise_id, group_id=None):
    p = get_scoped_or_404(LoanPromise, promise_id)
    if p.status == "OPEN":
        p.status = "CANCELLED"
        db.session.commit()
        flash("Promise cancelled.", "success")
    return redirect(request.referrer or url_for("fines.index"))
