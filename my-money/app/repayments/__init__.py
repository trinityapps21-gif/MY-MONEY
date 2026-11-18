from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func

from app.extensions import db
from app.models import Borrower, Loan, LoanRepayment, LoanRepaymentSchedule
from app.services import ledger, lender as lender_svc, loans as loan_svc
from app.services.audit import audit
from app.services.forms import form_amount, form_date, form_text, new_idem_key
from app.services.loans import ACTIVE_STATES, LoanError
from app.services.money import fmt
from app.services.savings import PAYMENT_METHODS, DuplicateError
from app.services.tenant import get_scoped_or_404, scoped, tenant_route
from app.services.timeutil import local_today

def money_text(v):
    return f"{g.group.currency} {v / (10 ** g.group.currency_decimals):,.{g.group.currency_decimals}f}"


bp = Blueprint("repayments", __name__, url_prefix="/g/<int:group_id>/repayments")


@bp.route("/")
@tenant_route(official=True)
def index(group_id=None):
    today = local_today(g.group.timezone)
    q = LoanRepayment.query.join(Borrower, Borrower.id == LoanRepayment.borrower_id) \
        .join(Loan, Loan.id == LoanRepayment.loan_id).filter(LoanRepayment.group_id == g.group.id)
    term = (request.args.get("q") or "").strip()
    if term:
        like = f"%{term}%"
        q = q.filter(Borrower.name.ilike(like) | Loan.reference.ilike(like)
                     | LoanRepayment.receipt_no.ilike(like) | LoanRepayment.payment_reference.ilike(like))
    page = q.order_by(LoanRepayment.payment_date.desc(), LoanRepayment.id.desc()).paginate(
        page=request.args.get("page", 1, type=int), per_page=15, error_out=False)
    month_start = today.replace(day=1)
    base = db.session.query(func.coalesce(func.sum(LoanRepayment.amount), 0)).filter(
        LoanRepayment.group_id == g.group.id, LoanRepayment.status == "POSTED")
    total = int(base.scalar() or 0)
    this_month = int(base.filter(LoanRepayment.payment_date >= month_start).scalar() or 0)
    due_now = db.session.query(func.coalesce(func.sum(
        LoanRepaymentSchedule.principal_due + LoanRepaymentSchedule.interest_due
        - LoanRepaymentSchedule.principal_paid - LoanRepaymentSchedule.interest_paid), 0)) \
        .join(Loan, Loan.id == LoanRepaymentSchedule.loan_id) \
        .filter(LoanRepaymentSchedule.group_id == g.group.id, Loan.status.in_(ACTIVE_STATES),
                Loan.disbursement_tx_id.isnot(None), LoanRepaymentSchedule.due_date <= today).scalar()
    return render_template("repayments/index.html", page=page, q=term, total=total, this_month=this_month,
                           due_now=int(due_now or 0))


@bp.route("/loans")
@tenant_route(official=True)
def loans_for_borrower(group_id=None):
    """Active loans of one borrower of THIS group (drives the repayment form)."""
    borrower = scoped(Borrower).filter_by(id=request.args.get("borrower", type=int)).first()
    if borrower is None:
        return jsonify(loans=[])
    c, d = g.group.currency, g.group.currency_decimals
    out = []
    for l in scoped(Loan, borrower_id=borrower.id).filter(Loan.status.in_(ACTIVE_STATES)).order_by(Loan.id):
        p, i = loan_svc.outstanding(l)
        lender_svc.accrue_penalties(g.group, local_today(g.group.timezone), l)
        fines = lender_svc.penalties_due(l) if g.group.is_lender else 0
        nd = loan_svc.next_due(l)
        out.append({"id": l.id, "reference": l.reference, "outstanding": p + i + fines,
                    "outstanding_text": fmt(p + i + fines, c, d) + (f" (includes late fines {fmt(fines, c, d)})" if fines else ""),
                    "next_due": nd.due_date.strftime("%d %b %Y") if nd else "",
                    "next_amount": fmt(nd.balance, c, d) if nd else ""})
    return jsonify(loans=out)


@bp.route("/new", methods=["GET", "POST"])
@tenant_route(official=True)
def new(group_id=None):
    today = local_today(g.group.timezone)
    if request.method == "POST":
        try:
            loan = get_scoped_or_404(Loan, request.form.get("loan_id", type=int) or 0)
            lender_svc.accrue_penalties(g.group, today, loan)
            amount = form_amount()
            rep, fine = lender_svc.record_payment(
                g.group, loan, current_user, amount, form_date("payment_date"),
                form_text("payment_method", 40), form_text("payment_reference", 80), form_text("notes"),
                request.form.get("idempotency_key") or None)
            audit("repayment.recorded", "loan_repayment", rep.id if rep else None,
                  {"amount": amount, "fine_part": fine, "loan": loan.reference}, group_id=g.group.id)
            db.session.commit()
            msg = f"Payment recorded. Receipt {rep.receipt_no}." if rep else "Payment recorded against the late fine."
            if fine and rep:
                msg += f" {fmt(fine, g.group.currency, g.group.currency_decimals)} cleared the late fine first."
            flash(msg, "success")
            late = getattr(rep, "assessed_fine", None) if rep else None
            if late is not None:
                flash(f"Paid {late.days_late} day(s) late: a fine of {money_text(late.amount)} was added. "
                      "Collect it from the Fines page.", "info")
            return redirect(url_for("lending.detail", loan_id=loan.id))
        except DuplicateError as e:
            db.session.rollback()
            flash(str(e), "error")
        except (LoanError, ValueError) as e:
            db.session.rollback()
            flash(str(e), "error")
    borrower_ids = [r[0] for r in db.session.query(Loan.borrower_id).filter(
        Loan.group_id == g.group.id, Loan.status.in_(ACTIVE_STATES), Loan.disbursement_tx_id.isnot(None)).distinct()]
    borrowers = scoped(Borrower).filter(Borrower.id.in_(borrower_ids or [0])).order_by(Borrower.name).all()
    return render_template("repayments/new.html", borrowers=borrowers, methods=PAYMENT_METHODS, today=today,
                           idem=new_idem_key(), pre_loan=request.args.get("loan", type=int),
                           pre_borrower=request.args.get("borrower", type=int))


@bp.route("/<int:repayment_id>/reverse", methods=["POST"])
@tenant_route(official=True)
def reverse(repayment_id, group_id=None):
    rep = get_scoped_or_404(LoanRepayment, repayment_id)
    try:
        loan_svc.reverse_repayment(g.group, rep, current_user, request.form.get("reason") or "",
                                   local_today(g.group.timezone))
        audit("repayment.reversed", "loan_repayment", rep.id, {"reason": request.form.get("reason")},
              group_id=g.group.id)
        db.session.commit()
        flash("Repayment reversed and the loan schedule restored.", "success")
    except (LoanError, ledger.LedgerError) as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(request.referrer or url_for("repayments.index"))
