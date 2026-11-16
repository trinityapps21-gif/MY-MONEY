"""Import existing group records (CSV / Excel / PDF) with preview, confirm and undo."""
import csv
import io
from datetime import date

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db, limiter
from app.models import ImportBatch
from app.services import importer, storage
from app.services.audit import audit
from app.services.importer import ImportFileError, ScannedPdf, SPECS
from app.services.money import fmt
from app.services.tenant import tenant_route
from app.services.timeutil import local_today

bp = Blueprint("imports", __name__, url_prefix="/g/<int:group_id>/import")

# preview columns: (label, key, kind-of-format)
COLS = {
    "members": [("Name", "full_name", ""), ("Phone", "phone", ""), ("Role", "role", ""), ("Email", "email", "")],
    "contributions": [("Member", "member", ""), ("Amount", "amount", "money"), ("Date", "date", ""),
                      ("Category", "category", ""), ("Reference", "reference", "")],
    "loans": [("Borrower", "borrower_name", ""), ("Principal", "principal", "money"), ("Rate", "rate_bp", "rate"),
              ("Method", "method", ""), ("Term", "duration", "term"), ("Lent on", "disbursement_date", ""),
              ("Your ref", "loan_ref", "")],
    "repayments": [("Loan", "loan", ""), ("Amount", "amount", "money"), ("Date", "date", ""),
                   ("Method", "payment_method", ""), ("Reference", "reference", "")],
}
NEXT = {
    "members": "Imported members can sign in after tapping <b>Forgot password</b> on the login page; you approve each one under <b>Password Resets</b>. Next, import their contributions.",
    "contributions": "Contributions are in the ledger and on member statements. Next, import loans if the group has any.",
    "loans": "Loans are active with their original terms and schedules. Next, import the repayments already received.",
    "repayments": "Repayments were applied to each loan's oldest instalment first, interest before principal.",
}


def _cell(group, kind, key, fmt_kind, d):
    v = d.get(key)
    if v in (None, ""):
        return "-"
    if fmt_kind == "money":
        return fmt(v, group.currency, group.currency_decimals)
    if fmt_kind == "rate":
        return f"{v / 100:g}% / {d['rate_period'].lower()}"
    if fmt_kind == "term":
        return f"{v} {d['duration_unit'].lower()}s"
    return str(v)


def _archive(raw, filename, mime):
    """Keep the original upload (ImageKit, private) so there is always a source record."""
    try:
        return storage.store_bytes(raw, filename, mime, group_id=g.group.id, user_id=current_user.id, kind="import")
    except storage.UploadError:
        return None


@bp.route("/")
@tenant_route(official=True)
def index(group_id=None):
    batches = ImportBatch.query.filter_by(group_id=g.group.id).order_by(ImportBatch.id.desc()).limit(30).all()
    return render_template("imports/index.html", specs=SPECS, batches=batches)


@bp.route("/template/<kind>.<fmt>")
@tenant_route(official=True)
def template(kind, fmt, group_id=None):
    if kind not in SPECS or fmt not in ("csv", "xlsx"):
        abort(404)
    if fmt == "csv":
        body, mime = importer.template_csv(kind), "text/csv"
    else:
        body, mime = importer.template_xlsx(kind), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return Response(body, mimetype=mime, headers={"Content-Disposition": f"attachment; filename=my-money-{kind}-template.{fmt}"})


@bp.route("/upload", methods=["POST"])
@tenant_route(official=True)
@limiter.limit("30 per hour")
def upload(group_id=None):
    kind = request.form.get("kind")
    f = request.files.get("file")
    if kind not in SPECS or not f or not f.filename:
        flash("Choose what you are importing and select a file.", "error")
        return redirect(url_for("imports.index"))
    raw = f.read(importer.MAX_BYTES + 1)
    try:
        ext, mime = storage.validate_document(raw, f.filename, f.mimetype, importer.MAX_BYTES)
        if ext not in importer.ALLOWED_EXT:
            raise storage.UploadError("Upload a .csv, .xlsx or .pdf file.")
    except storage.UploadError as e:
        flash(str(e), "error")
        return redirect(url_for("imports.index"))
    try:
        headers, body = importer.read_table(raw, f.filename)
        rows = importer.validate(g.group, kind, headers, body)
    except ScannedPdf as e:
        src = _archive(raw, f.filename, mime)
        if src is None:
            flash(f"{e} It could not be saved either because file storage is not configured.", "error")
            return redirect(url_for("imports.index"))
        b = ImportBatch(group_id=g.group.id, kind=kind, filename=f.filename[:200], source_file_id=src.id,
                        status="CANCELLED", rows_json="[]", created_by=current_user.id)
        db.session.add(b)
        audit("import.reference_saved", "import_batch", None, {"filename": f.filename}, group_id=g.group.id)
        db.session.commit()
        flash(f"{e} It was saved as a reference document in Import history. Type its figures into the template, "
              "or upload a text-based PDF / Excel / CSV instead.", "info")
        return redirect(url_for("imports.index"))
    except ImportFileError as e:
        flash(str(e), "error")
        return redirect(url_for("imports.index"))
    src = _archive(raw, f.filename, mime)
    batch = importer.create_batch(g.group, current_user, kind, f.filename, rows, src)
    db.session.commit()
    return redirect(url_for("imports.batch", batch_id=batch.id))


def _get(batch_id):
    b = ImportBatch.query.filter_by(id=batch_id, group_id=g.group.id).first()
    if b is None:
        abort(404)
    return b


@bp.route("/batch/<int:batch_id>")
@tenant_route(official=True)
def batch(batch_id, group_id=None):
    b = _get(batch_id)
    rows = importer.batch_rows(b)
    cols = COLS[b.kind]
    shown = [{"n": r["n"], "errors": r["errors"], "warnings": r["warnings"],
              "cells": [_cell(g.group, b.kind, k, fk, r["d"]) for _l, k, fk in cols]} for r in rows[:300]]
    return render_template("imports/batch.html", b=b, spec=SPECS[b.kind], cols=[c[0] for c in cols], rows=shown,
                           hidden=max(0, len(rows) - 300), next_hint=NEXT[b.kind])


@bp.route("/batch/<int:batch_id>/apply", methods=["POST"])
@tenant_route(official=True)
def apply(batch_id, group_id=None):
    b = _get(batch_id)
    try:
        n = importer.apply_batch(g.group, current_user, b)
        db.session.commit()
    except ImportFileError as e:
        db.session.rollback()
        flash(str(e), "error")
        return redirect(url_for("imports.batch", batch_id=b.id))
    flash(f"Imported {n} row(s).", "success")
    return redirect(url_for("imports.batch", batch_id=b.id))


@bp.route("/batch/<int:batch_id>/cancel", methods=["POST"])
@tenant_route(official=True)
def cancel(batch_id, group_id=None):
    b = _get(batch_id)
    if b.status == "PREVIEW":
        b.status = "CANCELLED"
        audit("import.cancelled", "import_batch", b.id, group_id=g.group.id)
        db.session.commit()
        flash("Import cancelled. Nothing was saved.", "success")
    return redirect(url_for("imports.index"))


@bp.route("/batch/<int:batch_id>/undo", methods=["POST"])
@tenant_route(official=True)
def undo(batch_id, group_id=None):
    b = _get(batch_id)
    try:
        kept = importer.undo_batch(g.group, current_user, b, local_today(g.group.timezone))
        db.session.commit()
    except (ImportFileError, ValueError) as e:
        db.session.rollback()
        flash(str(e), "error")
        return redirect(url_for("imports.batch", batch_id=b.id))
    flash("Import undone. Reversal entries were added to the ledger." +
          (f" Kept (they have records): {', '.join(kept)}." if kept else ""), "success")
    return redirect(url_for("imports.batch", batch_id=b.id))


@bp.route("/batch/<int:batch_id>/errors.csv")
@tenant_route(official=True)
def errors_csv(batch_id, group_id=None):
    b = _get(batch_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["file_row", "problem"])
    for r in importer.batch_rows(b):
        if r["errors"]:
            w.writerow([r["n"], "; ".join(r["errors"])])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=import-{b.id}-problems.csv"})
