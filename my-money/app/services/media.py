"""Client documents and asset photos: what a lender must hold before lending."""
from app.models import ClientMedia, BorrowerDocument, Collateral, IDENTITY_CATEGORIES, StoredFile


def _live(rows):
    return [r for r in rows if r.file is not None and not r.file.deleted_at]


def items_for(group_id, borrower_id):
    rows = ClientMedia.query.filter_by(group_id=group_id, borrower_id=borrower_id).order_by(ClientMedia.id.desc()).all()
    return _live(rows)


def checklist(group_id, borrower_id, loan=None):
    """Returns a list of (label, ok). Documents uploaded on the older borrower page count too."""
    media = items_for(group_id, borrower_id)
    cats = {m.category for m in media}
    old = {d.doc_type for d in BorrowerDocument.query.filter_by(group_id=group_id, borrower_id=borrower_id).all()
           if d.file is not None and not d.file.deleted_at}
    has_id = bool(cats & set(IDENTITY_CATEGORIES)) or bool(old & {"ID_FRONT"})
    has_photo = "CLIENT_PHOTO" in cats or "PHOTO" in old
    out = [("ID card, driving permit or passport", has_id), ("Client photo", has_photo)]
    if loan is not None:
        has_collateral = Collateral.query.filter_by(loan_id=loan.id).count() > 0
        if has_collateral:
            assets = [m for m in media if m.category == "ASSET_PHOTO" and m.loan_id == loan.id]
            out.append(("Photo of the asset held as security", bool(assets)))
    return out


def missing_for_loan(group, loan):
    """Names of what is still missing. Applies to lending businesses only, and is on unless the lender turned it off."""
    if not group.is_lender or not getattr(group, "require_client_media", True):
        return []  # savings groups are never affected
    return [label for label, ok in checklist(group.id, loan.borrower_id, loan) if not ok]


def summary(group_id, borrower_ids):
    """{borrower_id: (done, total)} for the overview page, in two queries."""
    media = ClientMedia.query.filter(ClientMedia.group_id == group_id, ClientMedia.borrower_id.in_(borrower_ids)).all() \
        if borrower_ids else []
    old = BorrowerDocument.query.filter(BorrowerDocument.group_id == group_id,
                                        BorrowerDocument.borrower_id.in_(borrower_ids)).all() if borrower_ids else []
    live_ids = {f.id for f in StoredFile.query.filter(StoredFile.id.in_([m.file_id for m in media] + [d.file_id for d in old]),
                                                      StoredFile.deleted_at.is_(None)).all()} if (media or old) else set()
    cats, olds = {}, {}
    for m in media:
        if m.file_id in live_ids:
            cats.setdefault(m.borrower_id, set()).add(m.category)
    for d in old:
        if d.file_id in live_ids:
            olds.setdefault(d.borrower_id, set()).add(d.doc_type)
    out = {}
    for bid in borrower_ids:
        c, o = cats.get(bid, set()), olds.get(bid, set())
        done = int(bool(c & set(IDENTITY_CATEGORIES)) or "ID_FRONT" in o) + int("CLIENT_PHOTO" in c or "PHOTO" in o)
        out[bid] = (done, 2)
    return out
