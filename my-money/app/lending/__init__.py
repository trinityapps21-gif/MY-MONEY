from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db
from app.models import Borrower, Loan, LoanApproval
from app.services import analytics, ledger, loans as loan_svc
from app.services.audit import audit
from app.services.forms import form_amount, form_choice, form_date, form_text
from app.services.loans import STATUS_LABELS, LoanError
from app.services.money import fmt, parse_rate_bp
from app.services.tenant import get_scoped_or_404, scoped, tenant_route
from app.services.timeutil import local_today

bp = Blueprint("lending", __name__, url_prefix="/g/<int:group_id>/loans")

PERIODS = {"WEEK": "per week", "MONTH": "per month", "YEAR": "per year"}
METHODS = {"FLAT": "Flat interest", "REDUCING": "Reducing balance"}
UNITS = {"MONTH": "months", "WEEK": "weeks"}
FREQS = {"MONTHLY": "Monthly", "WEEKLY": "Weekly"}


def _loan_data():
    s = loan_svc.get_interest_settings()
    rate_bp = parse_rate_bp(request.form.get("rate")) if s.allow_custom_rates else s.default_rate_bp
    period = form_choice("rate_period", PERIODS, "interest period") if s.allow_custom_rates else s.default_period
    duration = request.form.get("duration", type=int)
    if not duration or duration <= 0 or duration > 600:
        raise ValueError("Enter a valid loan duration.")
    return {
        "borrower_id": request.form.get("borrower_id", type=int),
        "principal": form_amount("principal"),
        "rate_bp": rate_bp, "rate_period": period,
        "method": form_choice("method", METHODS, "interest method"),
        "duration": duration, "duration_unit": form_choice("duration_unit", UNITS, "duration unit"),
        "frequency": form_choice("frequency", FREQS, "repayment frequency"),
        "disbursement_date": form_date("disbursement_date"),
        "first_repayment_date": form_date("first_repayment_date"),
        "purpose": form_text("purpose", 300), "notes": form_text("notes", 2000),
    }


@bp.route("/")
@tenant_route(official=True)
def index(group_id=None):
    today = local_today(g.group.timezone)
    status = request.args.get("status")
    q = Loan.query.join(Borrower, Borrower.id == Loan.borrower_id).filter(Loan.group_id == g.group.id)
    term = (request.args.get("q") or "").strip()
    if term:
        q = q.filter(Borrower.name.ilike(f"%{term}%") | Loan.reference.ilike(f"%{term}%"))
    if status in STATUS_LABELS and status != "OVERDUE":
        q = q.filter(Loan.status == status)
    page = q.order_by(Loan.created_at.desc(), Loan.id.desc()).paginate(
        page=request.args.get("page", 1, type=int), per_page=15, error_out=False)
    items = page.items
    if status == "OVERDUE":
        all_loans = q.all()
        items = [l for l in all_loans if loan_svc.is_overdue(l, today)]
    pf = loan_svc.portfolio(g.group, today)
    total_loans = Loan.query.filter_by(group_id=g.group.id).count()
    return render_template("lending/index.html", page=page, items=items, pf=pf, status=status, q=term,
                           total_loans=total_loans, eff=lambda l: loan_svc.effective_status(l, today),
                           statuses=STATUS_LABELS)


def _form_context(loan=None):
    s = loan_svc.get_interest_settings()
    today = local_today(g.group.timezone)
    borrowers = scoped(Borrower).order_by(Borrower.name).all()
    return dict(s=s, borrowers=borrowers, periods=PERIODS, methods=METHODS, units=UNITS, freqs=FREQS, today=today,
                preselect=request.args.get("borrower", type=int), cash=ledger.cash_balance(g.group.id))


@bp.route("/new", methods=["GET", "POST"])
@tenant_route(official=True)
def new(group_id=None):
    if request.method == "POST":
        try:
            data = _loan_data()
            loan = loan_svc.create_loan(g.group, current_user, data, save_draft=request.form.get("draft") == "1")
            audit("loan.created", "loan", loan.id, {"principal": loan.principal, "rate_bp": loan.rate_bp,
                                                     "method": loan.method}, group_id=g.group.id)
            db.session.commit()
            flash(f"Loan {loan.reference} created.", "success")
            return redirect(url_for("lending.detail", loan_id=loan.id))
        except (ValueError, LoanError) as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("lending/new.html", **_form_context())


@bp.route("/preview", methods=["POST"])
@tenant_route(official=True)
def preview(group_id=None):
    """Complete repayment schedule shown before the loan is confirmed."""
    try:
        data = _loan_data()
        result = loan_svc.preview(data)
    except (ValueError, LoanError) as e:
        return jsonify(error=str(e)), 400
    c, d = g.group.currency, g.group.currency_decimals
    return jsonify(
        rows=[{"n": i.number, "due": i.due_date.strftime("%d %b %Y"), "principal": fmt(i.principal, c, d, False),
               "interest": fmt(i.interest, c, d, False), "total": fmt(i.total, c, d, False)}
              for i in result.instalments],
        totals={"principal": fmt(result.total_principal, c, d), "interest": fmt(result.total_interest, c, d),
                "total": fmt(result.total_repayable, c, d), "count": len(result.instalments)})


@bp.route("/<int:loan_id>")
@tenant_route(official=True)
def detail(loan_id, group_id=None):
    loan = get_scoped_or_404(Loan, loan_id)
    today = local_today(g.group.timezone)
    p_out, i_out = loan_svc.outstanding(loan)
    my_decision = LoanApproval.query.filter_by(loan_id=loan.id, user_id=current_user.id).first()
    return render_template("lending/detail.html", loan=loan, today=today, p_out=p_out, i_out=i_out,
                           eff=loan_svc.effective_status(loan, today), next_due=loan_svc.next_due(loan),
                           my_decision=my_decision, periods=PERIODS, methods=METHODS, units=UNITS, freqs=FREQS,
                           can_self_approve=not (loan.approvals_required > 1 and loan.created_by == current_user.id))


@bp.route("/<int:loan_id>/action", methods=["POST"])
@tenant_route(official=True)
def action(loan_id, group_id=None):
    loan = get_scoped_or_404(Loan, loan_id)
    act, reason = request.form.get("action"), (request.form.get("reason") or "").strip()
    today = local_today(g.group.timezone)
    try:
        if act == "submit":
            loan_svc.submit_for_approval(g.group, loan, current_user)
            msg = "Loan submitted for approval."
        elif act == "approve":
            done = loan_svc.approve(g.group, loan, current_user, reason)
            msg = "Loan approved." if done else "Your approval was recorded. More approvals are required."
        elif act == "reject":
            loan_svc.reject(g.group, loan, current_user, reason)
            msg = "Loan rejected."
        elif act == "disburse":
            loan_svc.disburse(g.group, loan, current_user, form_date("disbursed_on", required=False, default=today))
            msg = "Loan disbursed. A disbursement was posted to the ledger."
        elif act == "reverse_disbursement":
            loan_svc.reverse_disbursement(g.group, loan, current_user, reason, today)
            msg = "Disbursement reversed."
        elif act == "write_off":
            loan_svc.write_off(g.group, loan, current_user, reason)
            msg = "Loan written off."
        else:
            abort(400)
        audit(f"loan.{act}", "loan", loan.id, {"reason": reason} if reason else None, group_id=g.group.id)
        db.session.commit()
        flash(msg, "success")
    except (LoanError, ledger.LedgerError, ValueError) as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("lending.detail", loan_id=loan.id))
