from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db
from app.models import (Borrower, Collateral, COLLATERAL_STATUS, Guarantor, Loan, LoanApproval, LoanProduct,
                        StoredFile, utcnow)
from app.models import FOLLOWUP_KINDS, LoanApplication, Group
from app.services import (agreement, analytics, late_fees, ledger, lender as lender_svc, loan_summary, loans as loan_svc, officers,
                          storage)
from app.services.audit import audit
from app.services.forms import form_amount, form_choice, form_date, form_text, new_idem_key
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
        "product_id": request.form.get("product_id", type=int),
        "officer_id": request.form.get("officer_id", type=int) or None,
        "refinance_of_id": request.form.get("refinance_of_id", type=int) or None,
    }


@bp.route("/")
@tenant_route(official=True)
def index(group_id=None):
    today = local_today(g.group.timezone)
    lender_svc.accrue_penalties(g.group, today)
    status = request.args.get("status")
    q = Loan.query.join(Borrower, Borrower.id == Loan.borrower_id).filter(Loan.group_id == g.group.id)
    term = (request.args.get("q") or "").strip()
    if term:
        q = q.filter(Borrower.name.ilike(f"%{term}%") | Loan.reference.ilike(f"%{term}%"))
    officer = request.args.get("officer", type=int)
    if officer:
        q = q.filter(Loan.officer_id == officer)
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
                           statuses=STATUS_LABELS, officers=dict(officers.officer_options(g.group.id)), officer=officer)


def _form_context(loan=None):
    s = loan_svc.get_interest_settings()
    today = local_today(g.group.timezone)
    borrowers = scoped(Borrower).order_by(Borrower.name).all()
    products = scoped(LoanProduct, active=True).order_by(LoanProduct.name).all() if g.group.is_lender else []
    prefill = {}
    pre_borrower = request.args.get("borrower", type=int)
    app_id = request.values.get("application") or request.values.get("application_id")
    if app_id and g.group.is_lender:
        a = scoped(LoanApplication, id=int(app_id), status="APPROVED").first() if str(app_id).isdigit() else None
        if a:
            pre_borrower = a.borrower_id
            prefill = {"application_id": a.id, "principal": loan_svc_plain(a.recommended_amount or a.amount),
                       "purpose": a.purpose, "product_id": a.product_id}
    rid = request.values.get("refinance") or request.values.get("refinance_of_id")
    if rid and str(rid).isdigit() and g.group.is_lender:
        old = scoped(Loan, id=int(rid)).first()
        if old and old.status in loan_svc.ACTIVE_STATES:
            q = loan_svc.settlement_quote(old, today)
            pre_borrower = old.borrower_id
            prefill.update({"refinance_of": old, "payoff": q["payoff"], "principal": loan_svc_plain(q["payoff"]),
                            "product_id": old.product_id})
    return dict(s=s, lender_cap=loan_svc.platform_lender_cap(), borrowers=borrowers, products=products, periods=PERIODS, methods=METHODS, units=UNITS, freqs=FREQS, today=today,
                preselect=pre_borrower, cash=ledger.cash_balance(g.group.id), prefill=prefill,
                officers=officers.officer_options(g.group.id))


def loan_svc_plain(minor):
    d = g.group.currency_decimals
    return f"{minor / (10 ** d):.{d}f}"


@bp.route("/new", methods=["GET", "POST"])
@tenant_route(official=True)
def new(group_id=None):
    if request.method == "POST":
        try:
            data = _loan_data()
            loan = loan_svc.create_loan(g.group, current_user, data, save_draft=request.form.get("draft") == "1")
            audit("loan.created", "loan", loan.id, {"principal": loan.principal, "rate_bp": loan.rate_bp,
                                                     "method": loan.method}, group_id=g.group.id)
            app_id = request.form.get("application_id", type=int)
            if app_id:
                a = scoped(LoanApplication, id=app_id, status="APPROVED", borrower_id=loan.borrower_id).first()
                if a:
                    a.status, a.loan_id = "CONVERTED", loan.id
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
        result = loan_svc.preview(data, g.group)
        extra = loan_svc.charges(g.group, data)
    except (ValueError, LoanError) as e:
        return jsonify(error=str(e)), 400
    c, d = g.group.currency, g.group.currency_decimals
    return jsonify(
        rows=[{"n": i.number, "due": i.due_date.strftime("%d %b %Y"), "principal": fmt(i.principal, c, d, False),
               "interest": fmt(i.interest, c, d, False), "total": fmt(i.total, c, d, False)}
              for i in result.instalments],
        totals={"principal": fmt(result.total_principal, c, d), "interest": fmt(result.total_interest, c, d),
                "total": fmt(result.total_repayable, c, d), "count": len(result.instalments),
                "fees_raw": extra["fee_processing"] + extra["fee_insurance"],
                "fees": fmt(extra["fee_processing"] + extra["fee_insurance"], c, d),
                "net": fmt(data["principal"] - extra["fee_processing"] - extra["fee_insurance"], c, d)})


@bp.route("/<int:loan_id>")
@tenant_route(official=True)
def detail(loan_id, group_id=None):
    loan = get_scoped_or_404(Loan, loan_id)
    today = local_today(g.group.timezone)
    lender_svc.accrue_penalties(g.group, today, loan)
    p_out, i_out = loan_svc.outstanding(loan)
    fine_stmt = late_fees.statement(g.group, loan, today)
    my_decision = LoanApproval.query.filter_by(loan_id=loan.id, user_id=current_user.id).first()
    wa_link = None
    phone = "".join(ch for ch in (loan.borrower.phone or "") if ch.isdigit())
    if phone:
        if phone.startswith("0"):
            phone = "256" + phone[1:]
        from urllib.parse import quote
        nd = loan_svc.next_due(loan)
        msg = (f"Hello {loan.borrower.name}, this is {g.group.name}. Your loan {loan.reference} payment schedule is attached."
               + (f" Your next payment of {fmt(nd.balance, g.group.currency, g.group.currency_decimals)} is due on {nd.due_date:%d %b %Y}." if nd else ""))
        wa_link = f"https://wa.me/{phone}?text={quote(msg)}"
    return render_template("lending/detail.html", wa_link=wa_link, loan=loan, today=today, p_out=p_out, i_out=i_out,
                           fine_stmt=fine_stmt,
                           summary=loan_summary.summary(loan, today, accruing=(fine_stmt or {}).get("fine", 0)),
                           fine_terms_info=late_fees.loan_terms(g.group, loan),
                           eff=loan_svc.effective_status(loan, today), next_due=loan_svc.next_due(loan),
                           my_decision=my_decision, periods=PERIODS, methods=METHODS, units=UNITS, freqs=FREQS,
                           penalty_due=lender_svc.penalties_due(loan), due=lender_svc.amount_due(loan, today), fine_reason=lambda p: lender_svc.fine_reason(loan, p, lambda v: fmt(v, g.group.currency, g.group.currency_decimals)), idem=new_idem_key(), officer_opts=officers.officer_options(g.group.id),
                           followup_kinds=FOLLOWUP_KINDS, quote=(loan_svc.settlement_quote(loan, today)
                                                                if loan.status in loan_svc.ACTIVE_STATES else None), collateral_status=COLLATERAL_STATUS,
                           can_self_approve=not (loan.approvals_required > 1 and loan.created_by == current_user.id))


@bp.route("/<int:loan_id>/schedule.pdf")
@tenant_route(official=True)
def schedule_pdf(loan_id, group_id=None):
    from flask import Response
    from app.services import documents
    from app.services import fine_terms
    loan = get_scoped_or_404(Loan, loan_id)
    if not loan.disbursement_tx_id:
        abort(404)
    # the loan's OWN late-fine terms (frozen when it was created), never the group's current rules
    rule = "Late payments: " + fine_terms.describe(loan, g.group.currency, g.group.currency_decimals)
    data = documents.loan_schedule_pdf(g.group, loan, utcnow(), rule, loan.fines)
    audit("loan.schedule_downloaded", "loan", loan.id, group_id=g.group.id)
    db.session.commit()
    resp = Response(data, mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f'attachment; filename="payment-schedule-{loan.reference}.pdf"'
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@bp.route("/<int:loan_id>/statement.pdf")
@tenant_route(official=True)
def statement_pdf(loan_id, group_id=None):
    """Complete loan statement (whole payment journey) - available for any disbursed loan, repaid or not."""
    from flask import Response
    from app.services import documents, fine_terms
    loan = get_scoped_or_404(Loan, loan_id)
    today = local_today(g.group.timezone)
    fs = late_fees.statement(g.group, loan, today)
    summ = loan_summary.summary(loan, today, accruing=(fs or {}).get("fine", 0))
    if not summ:
        abort(404)
    rule = "Late payments: " + fine_terms.describe(loan, g.group.currency, g.group.currency_decimals)
    data = documents.loan_statement_pdf(g.group, loan, summ, utcnow(), rule)
    audit("loan.statement_downloaded", "loan", loan.id, group_id=g.group.id)
    db.session.commit()
    resp = Response(data, mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f'attachment; filename="loan-statement-{loan.reference}.pdf"'
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


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


# ------------------------------------------------ guarantors, collateral, penalties
def _attach(loan):
    f = request.files.get("file")
    if f and f.filename:
        return storage.store_upload(f, group_id=g.group.id, user_id=current_user.id, kind="kyc",
                                    related=("loan", loan.id)).id
    return None


@bp.route("/<int:loan_id>/guarantors", methods=["POST"])
@tenant_route(official=True)
def add_guarantor(loan_id, group_id=None):
    loan = get_scoped_or_404(Loan, loan_id)
    try:
        name = (request.form.get("name") or "").strip()
        if len(name) < 2:
            raise ValueError("Enter the guarantor's name.")
        db.session.add(Guarantor(group_id=g.group.id, loan_id=loan.id, name=name[:150], phone=form_text("phone", 30),
                                 id_number=form_text("id_number", 60), relationship=form_text("relationship", 60),
                                 address=form_text("address", 250), occupation=form_text("occupation", 120),
                                 file_id=_attach(loan), created_by=current_user.id))
        audit("loan.guarantor_added", "loan", loan.id, {"name": name}, group_id=g.group.id)
        db.session.commit()
        flash("Guarantor added.", "success")
    except (ValueError, storage.UploadError) as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("lending.detail", loan_id=loan.id))


@bp.route("/<int:loan_id>/guarantors/<int:gid>/delete", methods=["POST"])
@tenant_route(official=True)
def delete_guarantor(loan_id, gid, group_id=None):
    gr = scoped(Guarantor, id=gid, loan_id=loan_id).first_or_404()
    if gr.file_id:
        storage.delete_file(db.session.get(StoredFile, gr.file_id), current_user.id, reason="guarantor removed")
    db.session.delete(gr)
    audit("loan.guarantor_removed", "loan", loan_id, {"name": gr.name}, group_id=g.group.id)
    db.session.commit()
    flash("Guarantor removed.", "success")
    return redirect(url_for("lending.detail", loan_id=loan_id))


@bp.route("/<int:loan_id>/collateral", methods=["POST"])
@tenant_route(official=True)
def add_collateral(loan_id, group_id=None):
    loan = get_scoped_or_404(Loan, loan_id)
    try:
        kind = (request.form.get("kind") or "").strip()
        if not kind:
            raise ValueError("Enter the collateral type, for example Land title or Vehicle logbook.")
        value = form_amount("estimated_value") if (request.form.get("estimated_value") or "").strip() else 0
        db.session.add(Collateral(group_id=g.group.id, loan_id=loan.id, kind=kind[:60],
                                  description=form_text("description", 300), estimated_value=value,
                                  file_id=_attach(loan), created_by=current_user.id))
        audit("loan.collateral_added", "loan", loan.id, {"kind": kind, "value": value}, group_id=g.group.id)
        db.session.commit()
        flash("Collateral recorded.", "success")
    except (ValueError, storage.UploadError) as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("lending.detail", loan_id=loan.id))


@bp.route("/<int:loan_id>/collateral/<int:cid>/status", methods=["POST"])
@tenant_route(official=True)
def collateral_status(loan_id, cid, group_id=None):
    c = scoped(Collateral, id=cid, loan_id=loan_id).first_or_404()
    new = request.form.get("status")
    if new not in COLLATERAL_STATUS:
        abort(400)
    c.status = new
    audit("loan.collateral_status", "loan", loan_id, {"collateral": c.id, "status": new}, group_id=g.group.id)
    db.session.commit()
    flash(f"Collateral marked {COLLATERAL_STATUS[new].lower()}.", "success")
    return redirect(url_for("lending.detail", loan_id=loan_id))


@bp.route("/<int:loan_id>/penalty", methods=["POST"])
@tenant_route(official=True)
def penalty(loan_id, group_id=None):
    loan = get_scoped_or_404(Loan, loan_id)
    today = local_today(g.group.timezone)
    try:
        if request.form.get("action") == "waive":
            n = lender_svc.waive_penalties(loan, request.form.get("reason"))
            audit("loan.penalty_waived", "loan", loan.id, {"reason": request.form.get("reason")}, group_id=g.group.id)
            msg = "Penalties waived."
        else:
            amount = form_amount("amount")
            lender_svc.collect_penalty(g.group, loan, current_user, amount, form_date("paid_on", required=False,
                                       default=today), form_text("payment_method", 40), form_text("reference", 80))
            audit("loan.penalty_collected", "loan", loan.id, {"amount": amount}, group_id=g.group.id)
            msg = "Penalty payment recorded."
        db.session.commit()
        flash(msg, "success")
    except (ValueError, lender_svc.LenderError, ledger.LedgerError) as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("lending.detail", loan_id=loan.id))


@bp.route("/<int:loan_id>/agreement.pdf")
@tenant_route(official=True, kind="LENDING")
def agreement_pdf(loan_id, group_id=None):
    from flask import Response
    from app.models.core import utcnow
    loan = get_scoped_or_404(Loan, loan_id)
    pdf = agreement.build(g.group, loan, utcnow(), current_user.full_name)
    audit("loan.agreement", "loan", loan.id, None, group_id=g.group.id)
    db.session.commit()
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="agreement-{loan.reference}.pdf"'})
