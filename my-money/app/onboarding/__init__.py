"""One-time welcome sequence for savings-group accounts.

Founders (the person who created the group): CEO splash screen, then the "Founding Billionaire" onboarding modal.
Members who joined by invitation: the CEO splash screen only.
The flag lives on the membership row (per user and per group), so it follows the person across devices."""
import os

from flask import Blueprint, current_app, g, jsonify

from app.extensions import db
from app.models import utcnow
from app.services.tenant import tenant_route

bp = Blueprint("onboarding", __name__, url_prefix="/g/<int:group_id>/onboarding")


def static_asset(rel):
    """`rel` if that file exists under app/static, else None (so a missing image never causes a broken picture)."""
    rel = (rel or "").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return None
    return rel if os.path.isfile(os.path.join(current_app.static_folder, rel)) else None


def context():
    """What the welcome partial needs, or None when this person has nothing to see. Cheap: no queries."""
    m, grp = g.get("membership"), g.get("group")
    if m is None or grp is None or not m.onboarding_pending or m.status != "ACTIVE" or grp.business_type != "SAVINGS_GROUP":
        return None
    return {"kind": "FOUNDER" if grp.owner_id == m.user_id else "MEMBER",
            "ceo_image": static_asset(current_app.config.get("ONBOARDING_CEO_IMAGE")),
            "logo": static_asset(current_app.config.get("ONBOARDING_BRAND_LOGO"))}


@bp.route("/complete", methods=["POST"])
@tenant_route()
def complete(group_id=None):
    """Record that this person's welcome has been shown. Idempotent; only ever touches the caller's own membership."""
    m = g.membership
    if m.onboarding_pending:
        m.onboarding_pending = False
        m.onboarding_completed_at = utcnow()
        db.session.commit()
    return jsonify(ok=True)
