from datetime import date

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db
from app.models import Borrower, Loan
from app.services import loans as loan_svc
from app.services.audit import audit
from app.services.forms import form_date, form_text
from app.services.tenant import get_scoped_or_404, scoped, tenant_route
from app.services.timeutil import local_today

bp = Blueprint("borrowers", __name__, url_prefix="/g/<int:group_id>/borrowers")

CATEGORIES = ["Individual", "Small Business", "Farmer", "Trader", "Salaried Worker", "Company", "Other"]


def _fill(b):
    name = (request.form.get("name") or "").strip()
    if len(name) < 2:
        raise ValueError("Enter the borrower's full or business name.")
    cat = request.form.get("category") or "Individual"
    if cat not in CATEGORIES:
        raise ValueError("Select a valid borrower category.")
    b.name, b.category = name[:150], cat
    b.phone, b.location = form_text("phone", 30), form_text("location", 150)
    b.id_number, b.notes = form_text("id_number", 60), form_text("notes", 2000)
    b.registered_on = form_date("registered_on", required=False, default=local_today(g.group.timezone))


@bp.route("/")
@tenant_route(official=True)
def index(group_id=None):
    q = scoped(Borrower)
    term = (request.args.get("q") or "").strip()
    if term:
        like = f"%{term}%"
        q = q.filter(Borrower.name.ilike(like) | Borrower.phone.ilike(like) | Borrower.location.ilike(like))
    page = q.order_by(Borrower.name).paginate(page=request.args.get("page", 1, type=int), per_page=15, error_out=False)
    counts = {b.id: Loan.query.filter_by(group_id=g.group.id, borrower_id=b.id).count() for b in page.items}
    return render_template("borrowers/index.html", page=page, q=term, counts=counts, categories=CATEGORIES,
                           today=local_today(g.group.timezone))


@bp.route("/new", methods=["GET", "POST"])
@tenant_route(official=True)
def new(group_id=None):
    if request.method == "POST":
        b = Borrower(group_id=g.group.id, created_by=current_user.id, name="x", registered_on=date.today())
        try:
            _fill(b)
            db.session.add(b)
            db.session.flush()
            audit("borrower.created", "borrower", b.id, group_id=g.group.id)
            db.session.commit()
            flash("Borrower registered.", "success")
            return redirect(url_for("borrowers.detail", borrower_id=b.id))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("borrowers/form.html", b=None, categories=CATEGORIES,
                           today=local_today(g.group.timezone))


@bp.route("/<int:borrower_id>", methods=["GET", "POST"])
@tenant_route(official=True)
def detail(borrower_id, group_id=None):
    b = get_scoped_or_404(Borrower, borrower_id)
    if request.method == "POST":
        try:
            _fill(b)
            audit("borrower.updated", "borrower", b.id, group_id=g.group.id)
            db.session.commit()
            flash("Borrower updated.", "success")
            return redirect(url_for("borrowers.detail", borrower_id=b.id))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")
    loans = scoped(Loan, borrower_id=b.id).order_by(Loan.created_at.desc()).all()
    today = local_today(g.group.timezone)
    return render_template("borrowers/detail.html", b=b, loans=loans, categories=CATEGORIES, today=today,
                           eff=lambda l: loan_svc.effective_status(l, today), outstanding=loan_svc.outstanding)
