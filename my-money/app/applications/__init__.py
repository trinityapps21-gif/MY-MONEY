"""Loan applications: request -> appraisal -> decision -> loan."""
from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db
from app.models import APPLICATION_STATUS, Borrower, LoanApplication, LoanProduct
from app.models.core import utcnow
from app.services import credit, officers
from app.services.audit import audit
from app.services.forms import form_amount, form_text
from app.services.tenant import get_scoped_or_404, scoped, tenant_route
from app.services.timeutil import local_today

bp = Blueprint("applications", __name__, url_prefix="/g/<int:group_id>/applications")


@bp.route("/")
@tenant_route(official=True, kind="LENDING")
def index(group_id=None):
    status = request.args.get("status")
    q = scoped(LoanApplication)
    if status in APPLICATION_STATUS:
        q = q.filter(LoanApplication.status == status)
    page = q.order_by(LoanApplication.id.desc()).paginate(page=request.args.get("page", 1, type=int), per_page=20,
                                                          error_out=False)
    return render_template("applications/index.html", page=page, status=status, labels=APPLICATION_STATUS)


@bp.route("/new", methods=["GET", "POST"])
@tenant_route(official=True, kind="LENDING")
def new(group_id=None):
    if request.method == "POST":
        try:
            b = scoped(Borrower, id=request.form.get("borrower_id", type=int) or 0).first()
            if b is None:
                raise ValueError("Select a borrower.")
            if b.blacklisted:
                raise ValueError("This borrower is blacklisted" + (f": {b.blacklist_reason}" if b.blacklist_reason else "."))
            amount = form_amount("amount")
            if amount <= 0:
                raise ValueError("Enter the amount requested.")
            prod = None
            if request.form.get("product_id"):
                prod = scoped(LoanProduct, id=request.form.get("product_id", type=int), active=True).first()
                if prod is None:
                    raise ValueError("Select a product from this business.")
            a = LoanApplication(group_id=g.group.id, borrower_id=b.id, product_id=prod.id if prod else None,
                                amount=amount, purpose=form_text("purpose", 300), officer_id=b.officer_id,
                                created_by=current_user.id)
            db.session.add(a)
            db.session.flush()
            audit("application.created", "loan_application", a.id, {"amount": amount}, group_id=g.group.id)
            db.session.commit()
            flash("Application recorded.", "success")
            return redirect(url_for("applications.detail", app_id=a.id))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("applications/new.html", borrowers=scoped(Borrower).filter_by(blacklisted=False)
                           .order_by(Borrower.name).all(), products=scoped(LoanProduct, active=True).all(),
                           preselect=request.args.get("borrower", type=int))


@bp.route("/<int:app_id>")
@tenant_route(official=True, kind="LENDING")
def detail(app_id, group_id=None):
    a = get_scoped_or_404(LoanApplication, app_id)
    hist = credit.history(a.borrower, local_today(g.group.timezone))
    return render_template("applications/detail.html", a=a, hist=hist, labels=APPLICATION_STATUS,
                           officer_names=dict(officers.officer_options(g.group.id)))


@bp.route("/<int:app_id>/appraise", methods=["POST"])
@tenant_route(official=True, kind="LENDING")
def appraise(app_id, group_id=None):
    a = get_scoped_or_404(LoanApplication, app_id)
    try:
        if a.status not in ("SUBMITTED", "UNDER_REVIEW"):
            raise ValueError("This application has already been decided.")
        money = lambda n: form_amount(n) if (request.form.get(n) or "").strip() else None  # noqa: E731
        a.monthly_income, a.monthly_expenses = money("monthly_income"), money("monthly_expenses")
        a.recommended_amount = money("recommended_amount")
        a.appraisal_notes = (request.form.get("appraisal_notes") or "").strip()[:2000] or None
        a.status = "UNDER_REVIEW"
        audit("application.appraised", "loan_application", a.id, None, group_id=g.group.id)
        db.session.commit()
        flash("Appraisal saved.", "success")
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("applications.detail", app_id=a.id))


@bp.route("/<int:app_id>/decide", methods=["POST"])
@tenant_route(official=True, kind="LENDING")
def decide(app_id, group_id=None):
    a = get_scoped_or_404(LoanApplication, app_id)
    action = request.form.get("action")
    reason = (request.form.get("reason") or "").strip()
    try:
        if a.status not in ("SUBMITTED", "UNDER_REVIEW"):
            raise ValueError("This application has already been decided.")
        if action == "approve":
            if a.borrower.blacklisted:
                raise ValueError("This borrower is blacklisted.")
            a.status = "APPROVED"
        elif action == "reject":
            if not reason:
                raise ValueError("Give a reason for rejecting.")
            a.status = "REJECTED"
        else:
            raise ValueError("Choose approve or reject.")
        a.decision_reason, a.decided_by, a.decided_at = reason[:300] or None, current_user.id, utcnow()
        audit(f"application.{action}", "loan_application", a.id, {"reason": reason}, group_id=g.group.id)
        db.session.commit()
        flash("Application " + ("approved. Create the loan when ready." if action == "approve" else "rejected."), "success")
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("applications.detail", app_id=a.id))
