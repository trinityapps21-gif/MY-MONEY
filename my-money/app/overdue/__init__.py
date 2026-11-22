"""Overdue repayments and late fees: admin tracking log, alerts and borrower notices (officials only)."""
import csv
import io

from flask import Blueprint, Response, abort, g, render_template, request
from flask_login import current_user

from app.extensions import db
from app.models import Loan, LoanRepaymentSchedule as S, utcnow
from app.services import documents, late_fees
from app.services.audit import audit
from app.services.money import from_minor
from app.services.tenant import get_scoped_or_404, tenant_route
from app.services.timeutil import local_today, to_local

bp = Blueprint("overdue", __name__, url_prefix="/g/<int:group_id>/overdue")


def _safe(v):
    """Stop spreadsheet formula injection from a borrower's name."""
    v = str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@") else v


@bp.route("/")
@tenant_route(official=True)
def index(group_id=None):
    group, today = g.group, local_today(g.group.timezone)
    late_fees.run_daily(group, today)               # alert administrators for any new penalty block
    db.session.commit()
    rows = late_fees.log_rows(group, today)
    if request.args.get("export") == "csv":
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(late_fees.LOG_COLUMNS)
        d = group.currency_decimals
        money = (lambda v: str(from_minor(v, d))) if d else str
        for r in rows:
            ln = r["last_notified"]
            w.writerow([r["loan"].reference, _safe(r["borrower"].name), f"{r['due_date']:%Y-%m-%d}", r["days_overdue"],
                        money(r["amount"]), f"{r['tier_pct']:g}%", money(r["fine"]), money(r["total"]),
                        f"{to_local(ln, group.timezone):%Y-%m-%d %H:%M}" if ln else "Never"])
        audit("late_fee.log_exported", "group", group.id, group_id=group.id)
        db.session.commit()
        resp = Response(out.getvalue(), mimetype="text/csv")
        resp.headers["Content-Disposition"] = f'attachment; filename="overdue-late-fee-log-{today:%Y-%m-%d}.csv"'
        resp.headers["Cache-Control"] = "private, no-store"
        return resp
    totals = {"count": len(rows), "amount": sum(r["amount"] for r in rows), "fine": sum(r["fine"] for r in rows),
              "total": sum(r["total"] for r in rows), "fined": sum(1 for r in rows if r["fine"] > 0),
              "capped": sum(1 for r in rows if r["capped"])}
    return render_template("overdue/index.html", rows=rows, totals=totals, today=today, columns=late_fees.LOG_COLUMNS,
                           mode=late_fees.block_mode(), older=sum(1 for r in rows if r["older_terms"]),
                           group_terms=late_fees.fine_terms.describe(late_fees.get_settings(g.group.id), g.group.currency,
                                                                     g.group.currency_decimals))


@bp.route("/<int:sched_id>/notice.pdf")
@tenant_route(official=True)
def notice(sched_id, group_id=None):
    sched = get_scoped_or_404(S, sched_id)
    loan, today = sched.loan, local_today(g.group.timezone)
    a = late_fees.assess(sched.balance, sched.due_date, today, terms=loan)
    if loan.group_id != g.group.id or sched.balance <= 0 or a["days_overdue"] <= 0:
        abort(404)                                   # nothing overdue on this instalment
    data = documents.late_fee_notice_pdf(g.group, loan, a, today)
    audit("late_fee.notice_generated", "loan", loan.id, {"instalment": sched.number, "fine": a["fine"]}, group_id=g.group.id)
    db.session.commit()
    resp = Response(data, mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f'attachment; filename="late-fee-notice-{loan.reference}-{sched.number}.pdf"'
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@bp.route("/loan/<int:loan_id>/statement.pdf")
@tenant_route(official=True)
def statement(loan_id, group_id=None):
    """Overdue Loan Fine Statement for the whole loan: principal before the fine, the fine, the new principal after it,
    the policy behind it and the working for each overdue instalment."""
    loan = get_scoped_or_404(Loan, loan_id)
    stmt = late_fees.statement(g.group, loan, local_today(g.group.timezone))
    if stmt is None:
        abort(404)                                   # nothing overdue on this loan
    data = documents.late_fee_statement_pdf(g.group, stmt)
    audit("late_fee.statement_generated", "loan", loan.id,
          {"instalments": len(stmt["lines"]), "principal_before": stmt["principal_before"], "fine": stmt["fine"],
           "principal_after": stmt["principal_after"]}, group_id=g.group.id)
    db.session.commit()
    resp = Response(data, mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f'attachment; filename="fine-statement-{loan.reference}.pdf"'
    resp.headers["Cache-Control"] = "private, no-store"
    return resp
