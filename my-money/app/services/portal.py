"""Borrower portal links. Borrowers have no login: each gets a private, revocable link that
shows only their own loans and lets them pay. The link carries a per-borrower nonce, so
rotating the nonce kills every link already sent."""
import secrets

from flask import current_app, has_request_context, request, url_for
from itsdangerous import BadSignature, URLSafeSerializer

from app.extensions import db
from app.models import Borrower, Group


def _signer():
    return URLSafeSerializer(current_app.config["SECRET_KEY"], salt="borrower-portal")


def token_for(borrower: Borrower) -> str:
    if not borrower.portal_nonce:
        borrower.portal_nonce = secrets.token_hex(6)
        db.session.flush()
    return _signer().dumps({"g": borrower.group_id, "b": borrower.id, "n": borrower.portal_nonce})


def rotate(borrower: Borrower):
    borrower.portal_nonce = secrets.token_hex(6)


def resolve(token):
    """-> (group, borrower) or (None, None)."""
    try:
        d = _signer().loads(token)
        group = db.session.get(Group, int(d["g"]))
        b = Borrower.query.filter_by(id=int(d["b"]), group_id=int(d["g"])).first()
    except (BadSignature, KeyError, ValueError, TypeError):
        return None, None
    if not group or not b or not b.portal_nonce or d.get("n") != b.portal_nonce or not group.is_lender:
        return None, None
    return group, b


def base_url() -> str:
    cfg = (current_app.config.get("APP_BASE_URL") or "").rstrip("/")
    if cfg:
        return cfg
    return request.url_root.rstrip("/") if has_request_context() else ""


def link_for(borrower: Borrower) -> str:
    path = url_for("portal.home", token=token_for(borrower))
    return base_url() + path
