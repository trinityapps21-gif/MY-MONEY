"""Group rules: everything the group's own officials (admin, treasurer, vice
chair) control - savings minimum, share price, contribution frequency, loan
fines and the loan interest policy. The platform admin has no access here.

The late-fine terms are frozen on each loan when it is created (services/fine_terms.py), so changing them here only
affects loans created afterwards. A change is recorded in the audit trail, announced to the other officials and
highlighted on this screen, so nobody is surprised that older loans keep their older terms."""
from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import db
from app.models import utcnow
from app.services import fine_terms, notify
from app.services.audit import audit
from app.services.groupcfg import get_settings
from app.services.money import parse_rate_bp, to_minor
from app.services.tenant import tenant_route

bp = Blueprint("rules", __name__, url_prefix="/g/<int:group_id>/rules")


def _int(name, lo, hi, label):
    try:
        v = int(request.form.get(name) or "")
    except ValueError:
        raise ValueError(f"Enter a whole number for {label}.")
    if not lo <= v <= hi:
        raise ValueError(f"{label} must be between {lo} and {hi}.")
    return v


@bp.route("/", methods=["GET", "POST"])
@tenant_route(official=True)
def index(group_id=None):
    s = get_settings(g.group.id)
    dec = g.group.currency_decimals
    if request.method == "POST":
        old_terms = fine_terms.values(s)
        try:
            s.min_savings_balance = to_minor(request.form.get("min_savings_balance") or "0", dec)
            price = to_minor(request.form.get("share_price") or "0", dec)
            if price <= 0:
                raise ValueError("Share price must be above zero.")
            s.share_price = price
            s.leaderboard_on = request.form.get("leaderboard_on") == "1"
            period = request.form.get("contribution_period")
            if period not in ("WEEKLY", "MONTHLY", "ANNUAL"):
                raise ValueError("Choose a contribution frequency.")
            s.contribution_period = period
            s.reminder_days = _int("reminder_days", 1, 30, "reminder days")
            s.fine_grace_days = _int("fine_grace_days", 0, 90, "grace days")
            s.fine_period_days = _int("fine_period_days", 1, 90, "fine period days")
            ftype = request.form.get("fine_type")
            if ftype not in ("PERCENT", "FIXED"):
                raise ValueError("Choose how fines are charged.")
            s.fine_type = ftype
            s.fine_rate_bp = parse_rate_bp(request.form.get("fine_rate") or "0")
            if s.fine_rate_bp > 100000:
                raise ValueError("Fine rate is too high.")
            s.fine_fixed = to_minor(request.form.get("fine_fixed") or "0", dec)
            s.fine_capped = request.form.get("fine_capped") == "1"
            default, mn, mx = (parse_rate_bp(request.form.get(k)) for k in ("default_rate", "min_rate", "max_rate"))
            if not (mn <= default <= mx):
                raise ValueError("The default loan rate must sit between the minimum and maximum rates.")
            if mx > 100000:
                raise ValueError("Maximum loan rate is too high.")
            flat, reducing = request.form.get("flat_enabled") == "1", request.form.get("reducing_enabled") == "1"
            if not (flat or reducing):
                raise ValueError("Enable at least one interest method.")
            dp = request.form.get("default_period")
            if dp not in ("WEEK", "MONTH", "YEAR"):
                raise ValueError("Select a default interest period.")
            s.default_rate_bp, s.min_rate_bp, s.max_rate_bp = default, mn, mx
            s.flat_enabled, s.reducing_enabled, s.default_period = flat, reducing, dp
            s.allow_custom_rates = request.form.get("allow_custom_rates") == "1"
            s.updated_at = utcnow()
            new_terms = fine_terms.values(s)
            audit("group.rules_changed", "group_settings", s.id, group_id=g.group.id)
            if new_terms != old_terms:
                s.fine_terms_since = utcnow()
                kept = fine_terms.count_on_other_terms(g.group.id, new_terms)   # open loans created under other terms
                audit("group.fine_terms_changed", "group_settings", s.id,
                      {"old": old_terms, "new": new_terms, "open_loans_keeping_earlier_terms": kept}, group_id=g.group.id)
                diff = "; ".join(f"{c['label']}: {c['old']} to {c['new']}" for c in
                                 fine_terms.changes(old_terms, new_terms, g.group.currency, dec))
                notify.notify(notify.official_ids(g.group.id, exclude=current_user.id), g.group.id, "FINE",
                              "Late-fine terms changed",
                              f"{current_user.full_name} changed the late-fine terms ({diff}). They apply to loans created from now on; "
                              f"{kept} open loan(s) keep the terms they were created with.", f"/g/{g.group.id}/rules/")
                note = (f"{kept} existing loan(s) keep the terms they started with." if kept
                        else "No existing loan is affected.")
                db.session.commit()
                flash(f"Late-fine terms updated. They apply only to loans created from now on. {note}", "warning")
            else:
                db.session.commit()
                flash("Group rules saved. Loan interest changes apply to new loans only.", "success")
            return redirect(url_for("rules.index"))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")
            s = get_settings(g.group.id)
    return render_template("rules/index.html", s=s, fine_text=fine_terms.describe(s, g.group.currency, dec),
                           fine_in_force=fine_terms.in_force(g.group, s),
                           fine_other_loans=fine_terms.count_on_other_terms(g.group.id, s))
