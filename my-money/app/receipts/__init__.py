"""Sharing receipts: signed WhatsApp links and the public PDF view that those links open."""
from flask import Blueprint, Response, abort, current_app, g, jsonify, url_for

from app.extensions import db, limiter
from app.models import Group, utcnow
from app.services import receipts as rc
from app.services.audit import audit
from app.services.tenant import tenant_route

bp = Blueprint("receipts", __name__)


@bp.route("/g/<int:group_id>/receipts/<kind>/<int:obj_id>/share", methods=["POST"])
@tenant_route()
@limiter.limit("60 per hour")
def share(kind, obj_id, group_id=None):
    """Make a shareable link for one receipt the caller is allowed to open, with ready-to-send WhatsApp text."""
    try:
        r = rc.describe(g.group, kind, obj_id)
    except rc.ReceiptError:
        abort(404)
    if not r.allowed(g.is_official, g.membership.id):
        abort(404)
    url = url_for("receipts.public", token=rc.make_token(g.group.id, kind, obj_id), _external=True)
    text = rc.share_text(g.group, r, url)
    audit("receipt.shared", kind, obj_id, {"number": r.number}, group_id=g.group.id)
    db.session.commit()
    return jsonify(ok=True, url=url, text=text, number=r.number, title=r.title,
                   whatsapp=rc.wa_url(r.phone, text),            # opens the person's own chat when we know their number
                   whatsapp_choose=rc.wa_url(None, text),        # lets the sender pick the contact
                   has_phone=bool(rc.wa_number(r.phone)), days=int(current_app.config.get("RECEIPT_LINK_DAYS", 30)))


@bp.route("/r/<token>")
@limiter.limit("60 per hour")
def public(token):
    """Open a shared receipt from its link. Nothing but the signed token is needed, so it works for the person it was
    sent to without an account. Links expire (RECEIPT_LINK_DAYS) and always show the receipt's current state."""
    parsed = rc.read_token(token)
    if not parsed:
        abort(404)
    gid, kind, obj_id = parsed
    group = db.session.get(Group, gid)
    if group is None:
        abort(404)
    try:
        r = rc.describe(group, kind, obj_id)
    except rc.ReceiptError:
        abort(404)
    resp = Response(rc.render_pdf(group, r, utcnow()), mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f'inline; filename="receipt_{r.number}.pdf"'
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp
