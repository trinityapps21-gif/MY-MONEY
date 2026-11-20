"""Client documents page for lending businesses: ID card, driving permit, passport, client photo and
pictures of the asset held as security. Officials only. Files are stored at ImageKit and shown only to officials."""
from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db
from app.models import Borrower, ClientMedia, Collateral, Loan, MEDIA_CATEGORIES, StoredFile
from app.services import media as media_svc, storage
from app.services.audit import audit
from app.services.forms import form_choice, form_text
from app.services.tenant import get_scoped_or_404, scoped, tenant_route

bp = Blueprint("media", __name__, url_prefix="/g/<int:group_id>/media")


@bp.route("/")
@tenant_route(official=True, kind="LENDING")
def index(group_id=None):
    q = (request.args.get("q") or "").strip()
    query = scoped(Borrower)
    if q:
        query = query.filter(Borrower.name.ilike(f"%{q}%"))
    borrowers = query.order_by(Borrower.name).limit(300).all()
    return render_template("media/index.html", borrowers=borrowers, q=q,
                           summary=media_svc.summary(g.group.id, [b.id for b in borrowers]))


@bp.route("/settings", methods=["POST"])
@tenant_route(official=True, kind="LENDING")
def settings(group_id=None):
    g.group.require_client_media = request.form.get("require_client_media") == "1"
    audit("group.media_requirement", "group", g.group.id, {"required": g.group.require_client_media}, group_id=g.group.id)
    db.session.commit()
    flash("Saved. Loans " + ("now need" if g.group.require_client_media else "no longer need") +
          " the client documents before disbursement.", "success")
    return redirect(request.referrer or url_for("media.index"))


@bp.route("/<int:borrower_id>")
@tenant_route(official=True, kind="LENDING")
def borrower(borrower_id, group_id=None):
    b = get_scoped_or_404(Borrower, borrower_id)
    items = media_svc.items_for(g.group.id, b.id)
    loans = scoped(Loan, borrower_id=b.id).order_by(Loan.created_at.desc()).all()
    collaterals = {l.id: Collateral.query.filter_by(loan_id=l.id).all() for l in loans}
    return render_template("media/borrower.html", b=b, items=items, loans=loans, collaterals=collaterals,
                           categories=MEDIA_CATEGORIES, checks=media_svc.checklist(g.group.id, b.id))


@bp.route("/<int:borrower_id>/upload", methods=["POST"])
@tenant_route(official=True, kind="LENDING")
def upload(borrower_id, group_id=None):
    b = get_scoped_or_404(Borrower, borrower_id)
    try:
        cat = form_choice("category", MEDIA_CATEGORIES, "document type")
        files = [f for f in request.files.getlist("files") if f and f.filename]
        if not files:
            raise ValueError("Choose a photo or take one with the camera.")
        if len(files) > 10:
            raise ValueError("Add at most 10 files at a time.")
        loan = coll = None
        if cat == "ASSET_PHOTO":
            loan = get_scoped_or_404(Loan, int(request.form.get("loan_id") or 0))
            if loan.borrower_id != b.id:
                raise ValueError("That loan belongs to another client.")
            cid = int(request.form.get("collateral_id") or 0)
            if cid:
                coll = Collateral.query.filter_by(id=cid, loan_id=loan.id).first()
        for f in files:
            sf = storage.store_upload(f, group_id=g.group.id, user_id=current_user.id, kind="kyc",
                                      related=("borrower", b.id))
            db.session.add(ClientMedia(group_id=g.group.id, borrower_id=b.id, loan_id=loan.id if loan else None,
                                       collateral_id=coll.id if coll else None, category=cat, file_id=sf.id,
                                       caption=form_text("caption", 200), created_by=current_user.id))
        audit("client_media.added", "borrower", b.id, {"category": cat, "count": len(files)}, group_id=g.group.id)
        db.session.commit()
        flash(f"{len(files)} file(s) saved.", "success")
    except (ValueError, storage.UploadError) as e:
        db.session.rollback()
        flash(str(e) if str(e) else "Please check the form and try again.", "error")
    return redirect(url_for("media.borrower", borrower_id=b.id))


@bp.route("/<int:borrower_id>/<int:media_id>/delete", methods=["POST"])
@tenant_route(official=True, kind="LENDING")
def delete(borrower_id, media_id, group_id=None):
    m = scoped(ClientMedia, id=media_id, borrower_id=borrower_id).first_or_404()
    storage.delete_file(db.session.get(StoredFile, m.file_id), current_user.id, reason="client media removed")
    audit("client_media.removed", "borrower", borrower_id, {"category": m.category}, group_id=g.group.id)
    db.session.commit()
    flash("File removed.", "success")
    return redirect(url_for("media.borrower", borrower_id=borrower_id))
