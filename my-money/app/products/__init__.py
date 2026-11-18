"""Loan products for lending businesses: saved terms, fees and penalty rules."""
from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db
from app.models import LoanProduct
from app.services import lender, loans as loan_svc
from app.services.audit import audit
from app.services.forms import form_amount, form_choice, form_text
from app.services.money import parse_rate_bp
from app.services.tenant import get_scoped_or_404, scoped, tenant_route

bp = Blueprint("products", __name__, url_prefix="/g/<int:group_id>/products")

PERIODS = {"WEEK": "per week", "MONTH": "per month", "YEAR": "per year"}
METHODS = {"FLAT": "Flat interest", "REDUCING": "Reducing balance"}
UNITS = {"MONTH": "months", "WEEK": "weeks"}
FREQS = {"MONTHLY": "Monthly", "WEEKLY": "Weekly"}
CHARGES = {"NONE": "None", "FLAT": "Flat amount", "PERCENT": "Percent"}


def _fill(p):
    name = (request.form.get("name") or "").strip()
    if len(name) < 2:
        raise ValueError("Enter a product name.")
    dup = scoped(LoanProduct).filter(LoanProduct.name == name, LoanProduct.id != (p.id or 0)).first()
    if dup:
        raise ValueError("You already have a product with this name.")
    rate_bp, period = parse_rate_bp(request.form.get("rate")), form_choice("rate_period", PERIODS, "interest period")
    s = loan_svc.get_interest_settings()
    lender.check_rate_cap(g.group, rate_bp, period, loan_svc.platform_lender_cap())
    duration = request.form.get("duration", type=int)
    if not duration or not 0 < duration <= 600:
        raise ValueError("Enter a valid duration.")
    d = g.group.currency_decimals
    mn = form_amount("min_amount") if request.form.get("min_amount") else 0
    mx = form_amount("max_amount") if request.form.get("max_amount") else 0
    if mx and mx < mn:
        raise ValueError("The maximum amount cannot be below the minimum.")
    p.name, p.description = name[:100], form_text("description", 300)
    p.method, p.rate_bp, p.rate_period = form_choice("method", METHODS, "interest method"), rate_bp, period
    p.duration, p.duration_unit = duration, form_choice("duration_unit", UNITS, "duration unit")
    p.frequency, p.min_amount, p.max_amount = form_choice("frequency", FREQS, "repayment frequency"), mn, mx
    for pre in ("processing_fee", "insurance_fee", "penalty"):
        kind = request.form.get(f"{pre}_type") or "NONE"
        setattr(p, f"{pre}_type", kind)
        setattr(p, f"{pre}_value", lender.parse_charge(kind, request.form.get(f"{pre}_value"), d))
    grace = request.form.get("penalty_grace_days", type=int) or 0
    if not 0 <= grace <= 365:
        raise ValueError("Grace days must be between 0 and 365.")
    p.penalty_grace_days = grace
    p.active = request.form.get("active") == "1"


def _ctx(p=None):
    return dict(p=p, periods=PERIODS, methods=METHODS, units=UNITS, freqs=FREQS, charges=CHARGES,
                s=loan_svc.get_interest_settings(), lender_cap=loan_svc.platform_lender_cap())


@bp.route("/")
@tenant_route(official=True, kind="LENDING")
def index(group_id=None):
    items = scoped(LoanProduct).order_by(LoanProduct.active.desc(), LoanProduct.name).all()
    return render_template("products/index.html", items=items, periods=PERIODS, methods=METHODS, units=UNITS)


@bp.route("/new", methods=["GET", "POST"])
@tenant_route(official=True, kind="LENDING")
def new(group_id=None):
    p = LoanProduct(group_id=g.group.id, created_by=current_user.id, name="x", rate_bp=0)
    if request.method == "POST":
        try:
            _fill(p)
            db.session.add(p)
            db.session.flush()
            audit("product.created", "loan_product", p.id, {"name": p.name}, group_id=g.group.id)
            db.session.commit()
            flash("Loan product created.", "success")
            return redirect(url_for("products.index"))
        except (ValueError, lender.LenderError) as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("products/form.html", **_ctx(None))


@bp.route("/<int:product_id>", methods=["GET", "POST"])
@tenant_route(official=True, kind="LENDING")
def edit(product_id, group_id=None):
    p = get_scoped_or_404(LoanProduct, product_id)
    if request.method == "POST":
        try:
            _fill(p)
            audit("product.updated", "loan_product", p.id, {"name": p.name}, group_id=g.group.id)
            db.session.commit()
            flash("Product saved. Existing loans keep the terms they were created with.", "success")
            return redirect(url_for("products.index"))
        except (ValueError, lender.LenderError) as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("products/form.html", **_ctx(p))
