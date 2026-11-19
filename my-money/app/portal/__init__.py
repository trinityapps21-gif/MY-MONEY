"""Borrower page: a private link per borrower to see their own loans, what is owed now, any late
fine and why it was added, and their payment history. No login, read-only. Staff record the
payments, and this page updates straight away."""
from flask import Blueprint, abort, render_template

from app.extensions import db, limiter
from app.models import Loan, LoanRepayment, User
from app.services import lender as lender_svc, loans as loan_svc
from app.services import portal as portal_svc
from app.services.money import fmt
from app.services.timeutil import local_today

bp = Blueprint("portal", __name__)


@bp.route("/pay/<token>")
@limiter.limit("60 per minute")
def home(token):
    group, b = portal_svc.resolve(token)
    if not group:
        abort(404)
    today = local_today(group.timezone)
    lender_svc.accrue_penalties(group, today)
    loans = Loan.query.filter(Loan.borrower_id == b.id, Loan.group_id == group.id,
                              Loan.disbursement_tx_id.isnot(None)).order_by(Loan.id.desc()).all()
    money = lambda v: fmt(v, group.currency, group.currency_decimals)
    rows = []
    for l in loans:
        if l.status not in loan_svc.ACTIVE_STATES:
            continue
        p, i = loan_svc.outstanding(l)
        fines = lender_svc.penalties_due(l)
        paid = sum(s.principal_paid + s.interest_paid for s in l.schedule)
        total = sum(s.principal_due + s.interest_due for s in l.schedule) or 1
        officer = db.session.get(User, l.officer_id or b.officer_id) if (l.officer_id or b.officer_id) else None
        rows.append({"loan": l, "out": p + i, "fines_owed": fines, "owe_total": p + i + fines,
                     "due": lender_svc.amount_due(l, today), "pct": min(100, int(paid * 100 / total)),
                     "late": loan_svc.is_overdue(l, today), "officer": officer,
                     "fines": [(f, lender_svc.fine_reason(l, f, money)) for f in l.penalties]})
    closed = [l for l in loans if l.status not in loan_svc.ACTIVE_STATES]
    history = LoanRepayment.query.filter(LoanRepayment.borrower_id == b.id, LoanRepayment.group_id == group.id,
                                         LoanRepayment.status == "POSTED").order_by(
        LoanRepayment.payment_date.desc(), LoanRepayment.id.desc()).limit(20).all()
    return render_template("portal/home.html", group=group, b=b, rows=rows, closed=closed, history=history,
                           today=today)
