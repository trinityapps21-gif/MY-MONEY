"""Import existing group records (CSV / Excel / PDF) with preview, confirm and undo."""
import csv
import io
from datetime import date

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db, limiter
from app.models import ImportBatch
from app.services import importer, storage, workbook as wb_svc
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
    "shares": [("Member", "member", ""), ("Shares", "units", ""), ("Price per share", "unit_price", "money"),
               ("Amount", "amount", "money"), ("Date", "date", "")],
    "income": [("Category", "category", ""), ("Amount", "amount", "money"), ("Date", "date", ""),
               ("Description", "description", "")],
    "expenses": [("Expense", "title", ""), ("Category", "category", ""), ("Amount", "amount", "money"),
                 ("Date", "date", "")],
}
NEXT = {
    "members": "Imported members can sign in after tapping <b>Forgot password</b> on the login page; you approve each one under <b>Password Resets</b>. Next, import their contributions.",
    "contributions": "Contributions are in the ledger and on member statements. Next, import loans if the group has any.",
    "loans": "Loans are active with their original terms and schedules. Next, import the repayments already received.",
    "repayments": "Repayments were applied to each loan's oldest instalment first, interest before principal.",
    "shares": "Shares are on each member's account with a receipt, and the money is in the ledger.",
    "income": "Income is in the ledger and on the Income page.",
    "expenses": "Expenses are in the ledger and on the Expenses page.",
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
    singles = ImportBatch.query.filter(ImportBatch.group_id == g.group.id, ImportBatch.bundle.is_(None)).order_by(
        ImportBatch.id.desc()).limit(30).all()
    history = [{"bundle": None, "b": b, "at": b.created_at} for b in singles]
    grouped = {}
    for b in ImportBatch.query.filter(ImportBatch.group_id == g.group.id, ImportBatch.bundle.isnot(None)).order_by(
            ImportBatch.id.desc()).limit(300).all():
        grouped.setdefault(b.bundle, []).append(b)
    for token, rows in list(grouped.items())[:15]:
        history.append({"bundle": token, "b": rows[0], "at": rows[0].created_at, "status": wb_svc.status_of(rows),
                        "sheets": len(rows), "rows": sum(x.total_rows for x in rows),
                        "done": sum(x.applied_rows for x in rows)})
    history.sort(key=lambda h: h["at"], reverse=True)
    return render_template("imports/index.html", specs=SPECS, history=history)


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


@bp.route("/workbook-template.xlsx")
@tenant_route(official=True)
def workbook_template(group_id=None):
    return Response(wb_svc.template_workbook(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=my-money-full-import-template.xlsx"})


@bp.route("/workbook", methods=["POST"])
@tenant_route(official=True)
@limiter.limit("20 per hour")
def workbook_upload(group_id=None):
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Select the workbook to upload.", "error")
        return redirect(url_for("imports.index"))
    raw = f.read(importer.MAX_BYTES + 1)
    try:
        ext, mime = storage.validate_document(raw, f.filename, f.mimetype, importer.MAX_BYTES)
        if ext != "xlsx":
            raise ImportFileError("Upload the Excel workbook (.xlsx). Use the workbook template for the layout.")
        token, batches, ignored = wb_svc.build_bundle(g.group, current_user, f.filename, raw, _archive(raw, f.filename, mime))
        db.session.commit()
    except (storage.UploadError, ImportFileError) as e:
        db.session.rollback()
        flash(str(e), "error")
        return redirect(url_for("imports.index"))
    if ignored:
        flash("These sheets were not recognised and were ignored: " + ", ".join(ignored) + ".", "info")
    return redirect(url_for("imports.bundle", token=token))


def _bundle(token):
    batches = wb_svc.get_bundle(g.group.id, token)
    if not batches:
        abort(404)
    return batches


@bp.route("/full/<token>")
@tenant_route(official=True)
def bundle(token, group_id=None):
    batches = _bundle(token)
    return render_template("imports/bundle.html", batches=batches, specs=SPECS, token=token,
                           status=wb_svc.status_of(batches), filename=batches[0].filename, created=batches[0].created_at,
                           creator=batches[0].creator, ready=sum(b.ok_rows for b in batches),
                           problems=sum(b.error_rows for b in batches),
                           applied=sum(b.applied_rows for b in batches), total=sum(b.total_rows for b in batches),
                           source_file_id=batches[0].source_file_id, hint=wb_svc.APPLY_HINT)


@bp.route("/full/<token>/apply", methods=["POST"])
@tenant_route(official=True)
def bundle_apply(token, group_id=None):
    _bundle(token)
    try:
        done = wb_svc.apply_bundle(g.group, current_user, token)
        audit("import.bundle_applied", "import_batch", None, {"bundle": token, "applied": done}, group_id=g.group.id)
        db.session.commit()
    except ImportFileError as e:
        db.session.rollback()
        flash(str(e), "error")
        return redirect(url_for("imports.bundle", token=token))
    flash(f"Imported {sum(done.values())} row(s) across {len(done)} sheet(s).", "success")
    return redirect(url_for("imports.bundle", token=token))


@bp.route("/full/<token>/cancel", methods=["POST"])
@tenant_route(official=True)
def bundle_cancel(token, group_id=None):
    _bundle(token)
    wb_svc.cancel_bundle(g.group, current_user, token)
    audit("import.bundle_cancelled", "import_batch", None, {"bundle": token}, group_id=g.group.id)
    db.session.commit()
    flash("Import cancelled. Nothing was saved.", "success")
    return redirect(url_for("imports.index"))


@bp.route("/full/<token>/undo", methods=["POST"])
@tenant_route(official=True)
def bundle_undo(token, group_id=None):
    _bundle(token)
    try:
        kept = wb_svc.undo_bundle(g.group, current_user, token, local_today(g.group.timezone))
        audit("import.bundle_undone", "import_batch", None, {"bundle": token, "kept_members": kept}, group_id=g.group.id)
        db.session.commit()
    except (ImportFileError, ValueError) as e:
        db.session.rollback()
        flash(str(e), "error")
        return redirect(url_for("imports.bundle", token=token))
    flash("The whole import was undone. Reversal entries were added to the ledger." +
          (f" Kept (they have records): {', '.join(sorted(set(kept)))}." if kept else ""), "success")
    return redirect(url_for("imports.bundle", token=token))


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
