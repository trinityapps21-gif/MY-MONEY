import json

from flask import has_request_context, request
from flask_login import current_user

from app.extensions import db
from app.models import AuditLog


def audit(action, entity_type=None, entity_id=None, details=None, group_id=None, user_id=None):
    """Append an audit record to the current session (caller commits, so the
    audit row is atomic with the change it describes)."""
    if user_id is None and has_request_context() and getattr(current_user, "is_authenticated", False):
        user_id = current_user.id
    ip = request.remote_addr if has_request_context() else None
    row = AuditLog(
        group_id=group_id,
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=json.dumps(details, default=str) if details is not None else None,
        ip_address=ip,
    )
    db.session.add(row)
    return row
