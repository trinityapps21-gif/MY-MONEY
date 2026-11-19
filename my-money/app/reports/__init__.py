"""Reports, receipts and the transaction ledger pages."""
from datetime import date

from flask import (Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for)
from flask_login import current_user

from app.extensions import db
from app.models import (Borrower, Contribution, Expense, GroupMember, Loan, LoanRepayment, Transaction, User, utcnow)
from app.services import documents, ledger, reports as report_svc, storage
from app.services.audit import audit
from app.services.forms import form_amount, form_choice, form_date, form_text
from app.services.report_helpers import member_options
from app.services.tenant import get_scoped_or_404, scoped, tenant_route
from app.services.timeutil import local_today, parse_date

bp = Blueprint("reports", __name__, url_prefix="/g/<int:group_id>")

FORMATS = {"html", "pdf", "xlsx", "csv"}
MIME = {"pdf": "application/pdf", "csv": "text/csv; charset=utf-8",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


@bp.route("/reports/")
@tenant_route(official=True)
def index(group_id=None):
    today = local_today(g.group.timezone)
    return render_template("reports/index.html", titles=report_svc.titles_for(g.group), helps=report_svc.REPORT_HELP,
                           members=member_options(g.group.id), today=today, year_start=today.replace(month=1, day=1))


@bp.route("/reports/<key>")
@tenant_route()
def run(key, group_id=None):
    today = local_today(g.group.timezone)
    if key not in report_svc.titles_for(g.group):
        abort(404)
    member_id = request.args.get("member", type=int)
    if key == "member_statement" and not g.is_official:
        member_id = g.membership.id  # members may only ever see their own statement
    elif not g.is_official:
        abort(403)
    fmt_ = request.args.get("format", "html")
    if fmt_ not in FORMATS:
        abort(400)
    start = parse_date(request.args.get("start"))
    end = parse_date(request.args.get("end"))
    try:
        report = report_svc.build(key, g.group, today, start, end, member_id=member_id,
                                  month=request.args.get("month"),
                                  year=request.args.get("year", type=int))
    except KeyError:
        abort(404)
    if fmt_ == "html":
        return render_template("reports/view.html", report=report, query=request.args)
    now = utcnow()
    if fmt_ == "csv":
        data = documents.to_csv(report, g.group)
    elif fmt_ == "xlsx":
        data = documents.to_xlsx(report, g.group)
    else:
        data = documents.to_pdf(report, g.group, now, current_user.full_name)
        storage.archive_generated(data, documents._filename(report, "pdf"), "application/pdf",
                                  group_id=g.group.id, user_id=current_user.id, kind="statement",
                                  related=("report", None))
    audit("report.exported", "report", None, {"report": key, "format": fmt_}, group_id=g.group.id)
    db.session.commit()
    resp = Response(data, mimetype=MIME[fmt_])
    resp.headers["Content-Disposition"] = f'attachment; filename="{documents._filename(report, fmt_)}"'
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


# ---------------------------------------------------------------------- receipts
@bp.route("/receipts/<kind>/<int:obj_id>")
@tenant_route()
def receipt(kind, obj_id, group_id=None):
    grp, now = g.group, utcnow()
    if kind == "contribution":
        c = get_scoped_or_404(Contribution, obj_id)
        if not g.is_official and c.member_id != g.membership.id:
            abort(404)
        fields = [("Received from", c.member.user.full_name), ("Category", report_svc.TX_LABELS.get(c.tx.tx_type)),
                  ("Contribution type", c.category.title()), ("Date", f"{c.contribution_date:%d %b %Y}"),
                  ("Payment method", c.payment_method), ("Payment reference", c.payment_reference),
                  ("Transaction reference", c.tx.reference), ("Recorded by", c.recorder.full_name)]
        pdf = documents.receipt_pdf(grp, "Contribution Receipt", c.receipt_no, fields, c.amount, now,
                                    "Reversed" if ledger.is_reversed(c.tx_id) else None)
        number, related = c.receipt_no, ("contribution", c.id)
    elif kind in ("repayment", "disbursement", "transaction"):
        if not g.is_official:
            abort(404)  # borrower information is never shown to ordinary members
        if kind == "repayment":
            r = get_scoped_or_404(LoanRepayment, obj_id)
            fields = [("Received from", r.borrower.name), ("Loan", r.loan.reference), ("Category", "Loan repayment"),
                      ("Principal portion", f"{r.principal_part:,}" if not grp.currency_decimals else
                       documents.fmt(r.principal_part, grp.currency, grp.currency_decimals, False)),
                      ("Interest portion", f"{r.interest_part:,}" if not grp.currency_decimals else
                       documents.fmt(r.interest_part, grp.currency, grp.currency_decimals, False)),
                      ("Date", f"{r.payment_date:%d %b %Y}"), ("Payment method", r.payment_method),
                      ("Payment reference", r.payment_reference), ("Recorded by", r.recorder.full_name)]
            pdf = documents.receipt_pdf(grp, "Loan Repayment Receipt", r.receipt_no, fields, r.amount, now,
                                        "This repayment has been REVERSED." if r.status == "REVERSED" else None)
            number, related = r.receipt_no, ("repayment", r.id)
        elif kind == "disbursement":
            loan = get_scoped_or_404(Loan, obj_id)
            if not loan.disbursement_tx_id:
                abort(404)
            tx = db.session.get(Transaction, loan.disbursement_tx_id)
            fields = [("Paid to", loan.borrower.name), ("Loan", loan.reference), ("Category", "Loan disbursement"),
                      ("Interest rate", f"{loan.rate_bp / 100:g}% per {loan.rate_period.lower()} "
                                        f"({'flat' if loan.method == 'FLAT' else 'reducing balance'})"),
                      ("Term", f"{loan.duration} {loan.duration_unit.lower()}(s), {loan.frequency.lower()} repayments"),
                      ("Total repayable", documents.fmt(loan.total_repayable, grp.currency, grp.currency_decimals)),
                      ("Date", f"{tx.tx_date:%d %b %Y}"), ("Transaction reference", tx.reference),
                      ("Recorded by", tx.recorder.full_name)]
            pdf = documents.receipt_pdf(grp, "Loan Disbursement Receipt", tx.reference, fields, loan.principal, now)
            number, related = tx.reference, ("loan", loan.id)
        else:
            tx = get_scoped_or_404(Transaction, obj_id)
            who = tx.member.user.full_name if tx.member else (tx.borrower.name if tx.borrower else "")
            fields = [("Party", who), ("Category", report_svc.TX_LABELS.get(tx.tx_type, tx.tx_type)),
                      ("Direction", "Money in" if tx.direction == "IN" else "Money out"),
                      ("Description", tx.description), ("Date", f"{tx.tx_date:%d %b %Y}"),
                      ("Payment method", tx.payment_method), ("Payment reference", tx.payment_reference),
                      ("Recorded by", tx.recorder.full_name)]
            pdf = documents.receipt_pdf(grp, "Transaction Receipt", tx.reference, fields, tx.amount, now)
            number, related = tx.reference, ("transaction", tx.id)
    else:
        abort(404)
    storage.archive_generated(pdf, f"receipt_{number}.pdf", "application/pdf", group_id=grp.id,
                              user_id=current_user.id, kind="receipt", related=related)
    db.session.commit()
    resp = Response(pdf, mimetype="application/pdf")
    disp = "attachment" if request.args.get("download") == "1" else "inline"
    resp.headers["Content-Disposition"] = f'{disp}; filename="receipt_{number}.pdf"'
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


# ------------------------------------------------------------------ transactions
@bp.route("/transactions/")
@tenant_route(official=True)
def transactions(group_id=None):
    q = Transaction.query.filter_by(group_id=g.group.id)
    t = request.args.get("type")
    if t in report_svc.TX_LABELS:
        q = q.filter(Transaction.tx_type == t)
    start, end = parse_date(request.args.get("start")), parse_date(request.args.get("end"))
    if start:
        q = q.filter(Transaction.tx_date >= start)
    if end:
        q = q.filter(Transaction.tx_date <= end)
    term = (request.args.get("q") or "").strip()
    if term:
        like = f"%{term}%"
        q = q.filter(Transaction.reference.ilike(like) | Transaction.description.ilike(like))
    page = q.order_by(Transaction.tx_date.desc(), Transaction.id.desc()).paginate(
        page=request.args.get("page", 1, type=int), per_page=20, error_out=False)
    reversed_ids = {r[0] for r in db.session.query(Transaction.reverses_id).filter(
        Transaction.group_id == g.group.id, Transaction.reverses_id.isnot(None))}
    t_all = ledger.totals(g.group.id)
    return render_template("reports/transactions.html", page=page, labels=report_svc.TX_LABELS, type_=t, q=term,
                           start=start, end=end, reversed_ids=reversed_ids, totals=t_all,
                           reversible=("EXPENSE", "PENALTY", "ADJUSTMENT"))


@bp.route("/transactions/new", methods=["GET", "POST"])
@tenant_route(official=True)
def transaction_new(group_id=None):
    today = local_today(g.group.timezone)
    if request.method == "POST":
        try:
            kind = form_choice("tx_type", ("EXPENSE", "PENALTY", "ADJUSTMENT"), "transaction type")
            amount, when = form_amount(), form_date("tx_date")
            desc = form_text("description")
            if not desc:
                raise ValueError("A description is required.")
            if kind == "EXPENSE":
                direction = "OUT"
            elif kind == "PENALTY":
                direction = "IN"
            else:
                direction = form_choice("direction", ("IN", "OUT"), "direction")
            member = None
            if kind == "PENALTY" and request.form.get("member_id"):
                member = GroupMember.query.filter_by(id=request.form.get("member_id", type=int), group_id=g.group.id,
                                                     status="ACTIVE").first()
                if member is None:
                    raise ValueError("Select an active member.")
            if direction == "OUT" and amount > ledger.cash_balance(g.group.id):
                raise ValueError("Insufficient available cash for this payment.")
            tx = ledger.post(g.group.id, kind, amount, direction, when, current_user.id, desc,
                             form_text("payment_method", 40), form_text("payment_reference", 80),
                             member_id=member.id if member else None, prefix={"EXPENSE": "EXP", "PENALTY": "PEN"}.get(kind, "ADJ"))
            if kind == "EXPENSE":
                db.session.add(Expense(group_id=g.group.id, category=(form_text("category", 60) or "General"),
                                       description=desc, amount=amount, expense_date=when, tx_id=tx.id,
                                       recorded_by=current_user.id))
            audit(f"transaction.{kind.lower()}", "transaction", tx.id, {"amount": amount}, group_id=g.group.id)
            db.session.commit()
            flash("Transaction recorded.", "success")
            return redirect(url_for("reports.transactions"))
        except (ValueError, ledger.LedgerError) as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("reports/transaction_new.html", today=today, members=member_options(g.group.id, models=True),
                           preselect=request.args.get("type"))


@bp.route("/transactions/<int:tx_id>/reverse", methods=["POST"])
@tenant_route(official=True)
def transaction_reverse(tx_id, group_id=None):
    tx = get_scoped_or_404(Transaction, tx_id)
    if tx.kind:
        flash("Reverse share purchases, income, fines and withdrawals from their own pages so balances stay consistent.",
              "error")
        return redirect(url_for("reports.transactions"))
    if tx.tx_type not in ("EXPENSE", "PENALTY", "ADJUSTMENT"):
        flash("Reverse contributions and loan payments from their own pages so balances stay consistent.", "error")
        return redirect(url_for("reports.transactions"))
    try:
        ledger.reverse(tx, current_user.id, request.form.get("reason") or "", local_today(g.group.timezone))
        audit("transaction.reversed", "transaction", tx.id, {"reason": request.form.get("reason")},
              group_id=g.group.id)
        db.session.commit()
        flash("Transaction reversed. A reversal entry was added; the original is kept.", "success")
    except ledger.LedgerError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("reports.transactions"))
