from datetime import date

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db
from app.models import Borrower, BorrowerDocument, DOC_TYPES, Loan, StoredFile
from app.services import credit, loans as loan_svc, officers, portal, storage
from app.services.audit import audit
from app.services.forms import form_amount, form_choice, form_date, form_text
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
    if g.group.is_lender:  # KYC
        b.date_of_birth = form_date("date_of_birth", required=False, default=None)
        if b.date_of_birth and b.date_of_birth >= local_today(g.group.timezone):
            raise ValueError("Date of birth must be in the past.")
        b.address, b.occupation, b.employer = form_text("address", 250), form_text("occupation", 120), form_text("employer", 150)
        b.monthly_income = form_amount("monthly_income") if (request.form.get("monthly_income") or "").strip() else None
        b.next_of_kin_name, b.next_of_kin_phone = form_text("next_of_kin_name", 150), form_text("next_of_kin_phone", 30)
        b.next_of_kin_relation = form_text("next_of_kin_relation", 60)
        oid = request.form.get("officer_id", type=int)
        if oid and not officers.valid_officer(g.group.id, oid):
            raise ValueError("Choose one of your staff as the loan officer.")
        b.officer_id = oid or None


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
            hit = credit.match_blacklisted(g.group.id, b.id_number, b.phone, exclude_id=b.id) if g.group.is_lender else None
            db.session.commit()
            if hit:
                flash(f"Warning: this ID number or phone matches {hit.name}, who is blacklisted"
                      f"{(': ' + hit.blacklist_reason) if hit.blacklist_reason else ''}.", "error")
            flash("Borrower registered.", "success")
            return redirect(url_for("borrowers.detail", borrower_id=b.id))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("borrowers/form.html", b=None, categories=CATEGORIES,
                           today=local_today(g.group.timezone), officer_opts=officers.officer_options(g.group.id))


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
    docs = scoped(BorrowerDocument, borrower_id=b.id).order_by(BorrowerDocument.id.desc()).all()
    photo = next((d for d in docs if d.doc_type == "PHOTO" and not d.file.deleted_at), None)
    docs = [d for d in docs if not d.file.deleted_at]
    return render_template("borrowers/detail.html", b=b, loans=loans, categories=CATEGORIES, today=today,
                           docs=docs, photo=photo, doc_types=DOC_TYPES,
                           hist=credit.history(b, today) if g.group.is_lender else None,
                           portal_url=portal.link_for(b) if (g.group.is_lender and b.portal_nonce) else None,
                           officer_opts=officers.officer_options(g.group.id),
                           officer_names=dict(officers.officer_options(g.group.id)),
                           eff=lambda l: loan_svc.effective_status(l, today), outstanding=loan_svc.outstanding)


@bp.route("/<int:borrower_id>/documents", methods=["POST"])
@tenant_route(official=True)
def add_document(borrower_id, group_id=None):
    b = get_scoped_or_404(Borrower, borrower_id)
    f = request.files.get("file")
    try:
        if not f or not f.filename:
            raise ValueError("Choose a file or take a photo.")
        dtype = form_choice("doc_type", DOC_TYPES, "document type")
        sf = storage.store_upload(f, group_id=g.group.id, user_id=current_user.id, kind="kyc",
                                  related=("borrower", b.id))
        db.session.add(BorrowerDocument(group_id=g.group.id, borrower_id=b.id, doc_type=dtype, file_id=sf.id,
                                        note=form_text("note", 200), created_by=current_user.id))
        audit("borrower.document_added", "borrower", b.id, {"type": dtype}, group_id=g.group.id)
        db.session.commit()
        flash("Document saved.", "success")
    except (ValueError, storage.UploadError) as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("borrowers.detail", borrower_id=b.id))


@bp.route("/<int:borrower_id>/documents/<int:doc_id>/delete", methods=["POST"])
@tenant_route(official=True)
def delete_document(borrower_id, doc_id, group_id=None):
    d = scoped(BorrowerDocument, id=doc_id, borrower_id=borrower_id).first_or_404()
    storage.delete_file(db.session.get(StoredFile, d.file_id), current_user.id, reason="borrower document removed")
    audit("borrower.document_removed", "borrower", borrower_id, {"type": d.doc_type}, group_id=g.group.id)
    db.session.commit()
    flash("Document removed.", "success")
    return redirect(url_for("borrowers.detail", borrower_id=borrower_id))


@bp.route("/<int:borrower_id>/blacklist", methods=["POST"])
@tenant_route(official=True, kind="LENDING")
def blacklist(borrower_id, group_id=None):
    b = get_scoped_or_404(Borrower, borrower_id)
    try:
        if request.form.get("action") == "remove":
            credit.unblacklist(b)
            audit("borrower.unblacklisted", "borrower", b.id, group_id=g.group.id)
            msg = "Removed from the blacklist."
        else:
            credit.blacklist(b, request.form.get("reason"))
            audit("borrower.blacklisted", "borrower", b.id, {"reason": b.blacklist_reason}, group_id=g.group.id)
            msg = "Borrower blacklisted. New loans and applications are blocked."
        db.session.commit()
        flash(msg, "success")
    except credit.CreditError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("borrowers.detail", borrower_id=b.id))


@bp.route("/<int:borrower_id>/portal", methods=["POST"])
@tenant_route(official=True, kind="LENDING")
def portal_link(borrower_id, group_id=None):
    """Show, text, or revoke the borrower's private payment/account link."""
    b = get_scoped_or_404(Borrower, borrower_id)
    action = request.form.get("action")
    try:
        if action == "rotate":
            portal.rotate(b)
            audit("borrower.portal_rotated", "borrower", b.id, group_id=g.group.id)
            db.session.commit()
            flash("The old link no longer works. Use 'Show link' to get the new one.", "success")
        else:
            portal.token_for(b)
            db.session.commit()
            flash("The borrower link is ready below. Copy it and send it to the borrower.", "success")
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("borrowers.detail", borrower_id=b.id))
