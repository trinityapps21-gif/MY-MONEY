"""Loan consent forms: officials build them, anyone with the link fills them in,
and the respondent downloads a PDF copy of their answers."""
import json
import re
import secrets
from datetime import date

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func

from app.consent.models import ConsentField, ConsentForm, ConsentResponse
from app.extensions import csrf, db, limiter
from app.models import Group, utcnow
from app.services.audit import audit
from app.services.consent_pdf import build_pdf
from app.services.tenant import tenant_route
from app.services.timeutil import to_local

bp = Blueprint("consent", __name__)

OFFICER_ROLES = ("ADMIN", "TREASURER", "VICE_CHAIR")
FIELD_TYPES = [
    ("short_text", "Short answer"),
    ("long_text", "Paragraph"),
    ("single_choice", "Multiple choice (pick one)"),
    ("multi_choice", "Checkboxes (pick several)"),
    ("dropdown", "Dropdown"),
    ("date", "Date"),
    ("number", "Number"),
    ("agree", "Consent tick box (I agree)"),
]
TYPE_KEYS = {k for k, _ in FIELD_TYPES}
CHOICE_TYPES = {"single_choice", "multi_choice", "dropdown"}
KEY_RE = re.compile(r"^(\d+|n\d+)$")  # existing question id, or n1, n2... for new ones

DEFAULT_INTRO = ("Please read the loan terms carefully, then fill in this form to give your consent.\n"
                 "Your answers are shared only with the group officials.")
DEFAULT_FIELDS = [
    ("Full name", "short_text", None, True),
    ("Phone number", "short_text", None, True),
    ("Loan amount requested", "number", None, True),
    ("Purpose of the loan", "long_text", None, False),
    ("I have read and understood the loan terms and I give my consent to them.", "agree", None, True),
]


# ----------------------------------------------------------------- helpers
def _guard():
    if getattr(g.membership, "role", None) not in OFFICER_ROLES:
        abort(403)


def _form_or_404(form_id):
    return ConsentForm.query.filter_by(id=form_id, group_id=g.group.id).first_or_404()


def _clean_options(raw):
    seen, out = set(), []
    for line in (raw or "").splitlines():
        opt = line.strip()[:120]
        if opt and opt.lower() not in seen:
            seen.add(opt.lower())
            out.append(opt)
    return out[:30]


def _validate_field(label, ftype, options_raw):
    label = (label or "").strip()
    if not label:
        return None, "Every question needs some text."
    if ftype not in TYPE_KEYS:
        return None, "Choose a valid answer type."
    opts = _clean_options(options_raw) if ftype in CHOICE_TYPES else []
    if ftype in CHOICE_TYPES and len(opts) < 2:
        return None, "Add at least two choices, one per line."
    return (label[:300], ftype, "\n".join(opts) or None), None


def _card_from_field(fld):
    return {"key": str(fld.id), "label": fld.label, "type": fld.field_type, "options": fld.options or "",
            "required": bool(fld.required), "deleted": False, "error": None}


def _cards_from_request():
    """Rebuild the editor's question cards, in the order submitted, from the posted form."""
    f, cards, seen = request.form, [], set()
    for key in f.getlist("field_key"):
        if not KEY_RE.match(key) or key in seen:
            continue
        seen.add(key)
        cards.append({
            "key": key, "label": f.get(f"f_{key}_label") or "", "type": f.get(f"f_{key}_type") or "short_text",
            "options": f.get(f"f_{key}_options") or "", "required": f.get(f"f_{key}_required") == "1",
            "deleted": f.get(f"f_{key}_delete") == "1", "error": None})
    return cards


def _next_new(cards):
    return 1 + max([int(c["key"][1:]) for c in cards if c["key"].startswith("n")] or [0])


def _apply_form_edits(form):
    """Validate the posted editor and, only if everything is valid, apply it.

    Returns (errors, cards). errors maps a card key (or 'title' / '_form') to a message.
    Order follows the order the cards were submitted in. Existing questions are only
    deleted when explicitly flagged, so a partial post can never wipe a question.
    """
    f = request.form
    errors = {}
    title = (f.get("title") or "").strip()
    if len(title) < 3:
        errors["title"] = "Give the form a title (at least 3 characters)."

    cards = _cards_from_request()
    existing = {str(x.id): x for x in form.fields}
    plan, to_delete, handled = [], [], set()
    for c in cards:
        key = c["key"]
        fld = existing.get(key)
        if key.isdigit() and fld is None:
            continue  # stale id from another session
        if fld is not None:
            handled.add(key)
        if c["deleted"]:
            if fld is not None:
                to_delete.append(fld)
            continue
        if fld is None and not c["label"].strip():
            continue  # a new card the admin left blank
        data, err = _validate_field(c["label"], c["type"], c["options"])
        if err:
            c["error"] = errors[key] = err
            continue
        plan.append((fld, data, c["required"]))

    leftover = [x for x in form.fields if str(x.id) not in handled]
    if not errors and not plan and not leftover:
        errors["_form"] = "A form needs at least one question."
    if errors:
        return errors, cards

    form.title = title[:160]
    form.intro = (f.get("intro") or "").strip()[:4000] or None
    form.is_open = f.get("is_open") == "1"
    for fld in to_delete:
        form.fields.remove(fld)
    ordered = []
    for fld, data, required in plan:
        if fld is None:
            fld = ConsentField()
            form.fields.append(fld)
        fld.label, fld.field_type, fld.options = data
        fld.required = required
        ordered.append(fld)
    ordered += leftover
    for i, fld in enumerate(ordered, 1):
        fld.position = i
    return {}, cards


def _respondent_name(answers):
    """Label shown in the Responses list: a 'name' question if there is one, else the first short answer."""
    texts = [a for a in answers if a["type"] == "short_text" and a["value"]]
    pick = next((a for a in texts if "name" in str(a["label"]).lower()), texts[0] if texts else None)
    return str(pick["value"])[:160] if pick else None


def _pdf_response(form, group, resp):
    data = build_pdf(group, form, resp, to_local(resp.submitted_at, group.timezone))
    out = Response(data, mimetype="application/pdf")
    out.headers["Content-Disposition"] = f'attachment; filename="consent-{resp.reference}.pdf"'
    out.headers["Cache-Control"] = "private, no-store"
    return out


# ------------------------------------------------------- officials (logged in)
@bp.route("/g/<int:group_id>/consent/")
@tenant_route(official=True)
def index(group_id=None):
    _guard()
    forms = ConsentForm.query.filter_by(group_id=g.group.id).order_by(ConsentForm.created_at.desc()).all()
    counts = {}
    if forms:
        rows = db.session.query(ConsentResponse.form_id, func.count(ConsentResponse.id)) \
            .filter(ConsentResponse.form_id.in_([f.id for f in forms])).group_by(ConsentResponse.form_id).all()
        counts = {fid: n for fid, n in rows}
    return render_template("consent/index.html", forms=forms, counts=counts)


@bp.route("/g/<int:group_id>/consent/new", methods=["POST"])
@tenant_route(official=True)
def new(group_id=None):
    _guard()
    form = ConsentForm(group_id=g.group.id, title="Loan consent form", intro=DEFAULT_INTRO,
                       token=secrets.token_urlsafe(9), created_by=current_user.id)
    for i, (label, ftype, options, required) in enumerate(DEFAULT_FIELDS, 1):
        form.fields.append(ConsentField(position=i, label=label, field_type=ftype, options=options,
                                        required=required))
    db.session.add(form)
    db.session.flush()
    audit("consent.form_created", "consent_form", form.id, group_id=g.group.id)
    db.session.commit()
    flash("Consent form created. Edit it below, then share the link.", "success")
    return redirect(url_for("consent.edit", form_id=form.id))


@bp.route("/g/<int:group_id>/consent/<int:form_id>/edit", methods=["GET", "POST"])
@tenant_route(official=True)
def edit(form_id, group_id=None):
    _guard()
    form = _form_or_404(form_id)
    draft = None
    if request.method == "POST":
        errors, cards = _apply_form_edits(form)
        if not errors:
            audit("consent.form_updated", "consent_form", form.id, group_id=g.group.id)
            db.session.commit()
            flash("Form saved.", "success")
            return redirect(url_for("consent.edit", form_id=form.id))
        db.session.rollback()
        flash(errors.get("_form") or "Some items need fixing. They are highlighted below, and your edits are kept.",
              "error")
        draft = {"title": request.form.get("title") or "", "intro": request.form.get("intro") or "",
                 "is_open": request.form.get("is_open") == "1", "errors": errors}
    else:
        cards = [_card_from_field(x) for x in form.fields]
    count = ConsentResponse.query.filter_by(form_id=form.id).count()
    return render_template("consent/edit.html", form=form, cards=cards, draft=draft, next_new=_next_new(cards),
                           types=FIELD_TYPES, type_names=dict(FIELD_TYPES), choice_types=sorted(CHOICE_TYPES),
                           public_url=url_for("consent.public_form", token=form.token, _external=True),
                           response_count=count)


@bp.route("/g/<int:group_id>/consent/<int:form_id>/delete", methods=["POST"])
@tenant_route(official=True)
def delete(form_id, group_id=None):
    _guard()
    form = _form_or_404(form_id)
    if ConsentResponse.query.filter_by(form_id=form.id).count():
        flash("This form already has responses, so it can't be deleted. Turn off 'Accepting responses' to close it.",
              "error")
        return redirect(url_for("consent.edit", form_id=form.id))
    audit("consent.form_deleted", "consent_form", form.id, group_id=g.group.id)
    db.session.delete(form)
    db.session.commit()
    flash("Form deleted.", "success")
    return redirect(url_for("consent.index"))


@bp.route("/g/<int:group_id>/consent/<int:form_id>/responses")
@tenant_route(official=True)
def responses(form_id, group_id=None):
    _guard()
    form = _form_or_404(form_id)
    page = ConsentResponse.query.filter_by(form_id=form.id).order_by(ConsentResponse.id.desc()) \
        .paginate(page=request.args.get("page", 1, type=int), per_page=25, error_out=False)
    return render_template("consent/responses.html", form=form, page=page)


@bp.route("/g/<int:group_id>/consent/<int:form_id>/responses/<int:response_id>/pdf")
@tenant_route(official=True)
def response_pdf(form_id, response_id, group_id=None):
    _guard()
    form = _form_or_404(form_id)
    resp = ConsentResponse.query.filter_by(id=response_id, form_id=form.id).first_or_404()
    return _pdf_response(form, g.group, resp)


# ------------------------------------------------ public (anyone with the link)
def _public_context(token):
    form = ConsentForm.query.filter_by(token=token).first_or_404()
    group = db.session.get(Group, form.group_id)
    if group is None or group.status != "ACTIVE":
        abort(404)
    return form, group


def _collect_answers(form):
    answers, values, errors = [], {}, {}
    for fld in form.fields:
        key, ftype, options = f"q_{fld.id}", fld.field_type, fld.option_list
        if ftype == "multi_choice":
            chosen = [v for v in request.form.getlist(key) if v in options]
            values[key], value, empty = chosen, chosen, not chosen
        elif ftype == "agree":
            ticked = request.form.get(key) == "1"
            values[key], value, empty = ticked, ("Yes, I agree" if ticked else "No"), not ticked
        else:
            raw = (request.form.get(key) or "").strip()
            values[key] = raw
            value, empty = raw[:2000 if ftype == "long_text" else 300], not raw
            if raw and ftype in ("single_choice", "dropdown") and raw not in options:
                errors[key] = "Choose one of the options."
            elif raw and ftype == "number":
                try:
                    float(raw.replace(",", ""))
                except ValueError:
                    errors[key] = "Enter a number."
            elif raw and ftype == "date":
                try:
                    date.fromisoformat(raw)
                except ValueError:
                    errors[key] = "Enter a valid date."
        if empty and fld.required and key not in errors:
            errors[key] = "This question is required."
        answers.append({"label": fld.label, "type": ftype, "value": value})
    return answers, values, errors


@bp.route("/consent/<token>")
def public_form(token):
    form, group = _public_context(token)
    return render_template("consent/fill.html", form=form, group=group, values={}, errors={})


@bp.route("/consent/<token>", methods=["POST"])
@csrf.exempt
@limiter.limit("20 per hour")
def public_submit(token):
    form, group = _public_context(token)
    if not form.is_open:
        flash("This form is no longer accepting responses.", "error")
        return redirect(url_for("consent.public_form", token=token))
    if request.form.get("website"):  # honeypot field: real people never fill it
        return redirect(url_for("consent.public_form", token=token))
    answers, values, errors = _collect_answers(form)
    if errors:
        return render_template("consent/fill.html", form=form, group=group, values=values, errors=errors)
    respondent = _respondent_name(answers)
    resp = ConsentResponse(
        form_id=form.id, token=secrets.token_urlsafe(16), respondent=respondent,
        reference=f"CON-{utcnow():%y%m%d}-{secrets.token_hex(3).upper()}",
        answers_json=json.dumps(answers, ensure_ascii=False))
    db.session.add(resp)
    db.session.commit()
    return redirect(url_for("consent.public_done", token=token, resp_token=resp.token))


@bp.route("/consent/<token>/done/<resp_token>")
def public_done(token, resp_token):
    form, group = _public_context(token)
    resp = ConsentResponse.query.filter_by(token=resp_token, form_id=form.id).first_or_404()
    return render_template("consent/done.html", form=form, group=group, resp=resp)


@bp.route("/consent/r/<resp_token>/pdf")
def public_pdf(resp_token):
    resp = ConsentResponse.query.filter_by(token=resp_token).first_or_404()
    form = resp.form
    group = db.session.get(Group, form.group_id)
    if group is None:
        abort(404)
    return _pdf_response(form, group, resp)
