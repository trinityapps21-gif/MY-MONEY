"""Centralised tenant isolation and permission enforcement.

Every group route lives under /g/<group_id>/... . The group id in the URL is
NEVER trusted: ``tenant_route`` loads the logged-in user's own ACTIVE
membership for that group and aborts with 404 if there is none (404, not 403,
so group existence is not disclosed). All data access afterwards goes through
``scoped`` / ``get_scoped_or_404`` which always filter by the verified group.
"""
from functools import wraps

from flask import abort, g, render_template
from flask_login import current_user, login_required

from app.models import GroupMember, OFFICIAL_ROLES


def tenant_route(official=False, kind=None):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapper(*args, **kwargs):
            group_id = kwargs.pop("group_id")
            membership = GroupMember.query.filter_by(
                group_id=group_id, user_id=current_user.id, status="ACTIVE"
            ).first()
            if membership is None:
                abort(404)
            if membership.group.status != "ACTIVE":
                return render_template("errors/group_suspended.html", group=membership.group), 403
            if official and membership.role not in OFFICIAL_ROLES:
                abort(403)
            if kind and membership.group.business_type != kind:  # e.g. savings pages in a lending business
                abort(404)
            g.group = membership.group
            g.membership = membership
            g.is_official = membership.role in OFFICIAL_ROLES
            return view(*args, **kwargs)

        return wrapper

    return decorator


def platform_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_platform_admin:
            abort(404)
        return view(*args, **kwargs)

    return wrapper


def scoped(model, **filters):
    """Query for ``model`` restricted to the verified current group."""
    return model.query.filter_by(group_id=g.group.id, **filters)


def get_scoped_or_404(model, object_id):
    obj = scoped(model).filter_by(id=object_id).first()
    if obj is None:
        abort(404)
    return obj
