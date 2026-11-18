"""Collections: who must pay today, who is late, and the follow-up log."""
from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db
from app.models import FOLLOWUP_KINDS, FollowUp, Loan, User
from app.services import collections as col, loans as loan_svc, officers
from app.services.audit import audit
from app.services.forms import form_amount, form_choice, form_date, form_text
from app.services.tenant import get_scoped_or_404, scoped, tenant_route
from app.services.timeutil import local_today

bp = Blueprint("collections", __name__, url_prefix="/g/<int:group_id>/collections")


@bp.route("/")
@tenant_route(official=True, kind="LENDING")
def index(group_id=None):
    today = local_today(g.group.timezone)
    from app.services import lender as lender_svc
    lender_svc.accrue_penalties(g.group, today)
    officer = request.args.get("officer", type=int)
    if request.args.get("mine"):
        officer = current_user.id
    work = col.worklist(g.group, today, officer)
    names = dict(officers.officer_options(g.group.id))
    return render_template("collections/index.html", work=work, today=today, officers=names, officer=officer,
                           kinds=FOLLOWUP_KINDS, total_overdue=sum(r["overdue_amount"] for r in work["overdue"]))


@bp.route("/loans/<int:loan_id>/followup", methods=["POST"])
@tenant_route(official=True, kind="LENDING")
def add_followup(loan_id, group_id=None):
    loan = get_scoped_or_404(Loan, loan_id)
    try:
        note = (request.form.get("note") or "").strip()
        if len(note) < 3:
            raise ValueError("Write what happened, even briefly.")
        pdate = form_date("promise_date", required=False, default=None)
        pamt = form_amount("promise_amount") if (request.form.get("promise_amount") or "").strip() else None
        if pdate and pdate < local_today(g.group.timezone):
            raise ValueError("A promise date cannot be in the past.")
        db.session.add(FollowUp(group_id=g.group.id, loan_id=loan.id, borrower_id=loan.borrower_id,
                                kind=form_choice("kind", FOLLOWUP_KINDS, "contact type"), note=note[:500],
                                promise_date=pdate, promise_amount=pamt, created_by=current_user.id))
        audit("loan.followup", "loan", loan.id, {"promise": str(pdate) if pdate else None}, group_id=g.group.id)
        db.session.commit()
        flash("Follow-up saved.", "success")
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(request.form.get("next") or url_for("lending.detail", loan_id=loan.id))


@bp.route("/loans/<int:loan_id>/officer", methods=["POST"])
@tenant_route(official=True, kind="LENDING")
def assign_officer(loan_id, group_id=None):
    loan = get_scoped_or_404(Loan, loan_id)
    uid = request.form.get("officer_id", type=int)
    if uid and not officers.valid_officer(g.group.id, uid):
        flash("Choose one of your staff.", "error")
    else:
        loan.officer_id = uid or None
        audit("loan.officer", "loan", loan.id, {"officer": uid}, group_id=g.group.id)
        db.session.commit()
        flash("Officer updated.", "success")
    return redirect(url_for("lending.detail", loan_id=loan.id))
