"""MY MONEY - multi-tenant savings group and lending platform.
Developed and Powered by Emtrixz Technology Company."""
import logging

import click
from flask import Flask, abort, g, jsonify, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from werkzeug.middleware.proxy_fix import ProxyFix

from app.extensions import csrf, db, limiter, login_manager, migrate
from config import Config, is_production


@event.listens_for(Engine, "connect")
def _sqlite_fk_pragma(dbapi_conn, _):
    """Enforce foreign keys on local SQLite (dev/tests). Turso enforces them natively."""
    try:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
    except Exception:  # pragma: no cover
        pass


def create_app(overrides=None):
    app = Flask(__name__)
    if overrides is None:
        app.config.update(Config.build())
    else:
        app.config.update({k: v for k, v in Config.__dict__.items() if k.isupper()})
        app.config.update(overrides)
    app.config["IS_PRODUCTION"] = is_production() and overrides is None
    if app.config["IS_PRODUCTION"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    logging.basicConfig(level=logging.INFO)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    from app import models  # noqa: F401  (register tables)
    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        u = db.session.get(User, int(user_id))
        return u if u and u.status == "ACTIVE" else None

    register_blueprints(app)
    register_template_helpers(app)
    register_hooks(app)
    register_errors(app)
    register_cli(app)
    return app


def register_blueprints(app):
    from app.announcements import bp as announcements_bp
    from app.auth import bp as auth_bp
    from app.borrowers import bp as borrowers_bp
    from app.community import bp as community_bp
    from app.feed import bp as feed_bp
    from app.files import bp as files_bp
    from app.groups import bp as groups_bp
    from app.imports import bp as imports_bp
    from app.lending import bp as lending_bp
    from app.main import bp as main_bp
    from app.members import bp as members_bp
    from app.notifications import bp as notifications_bp
    from app.platform_admin import bp as platform_bp
    from app.repayments import bp as repayments_bp
    from app.reports import bp as reports_bp
    from app.savings import bp as savings_bp
    from app.shares import bp as shares_bp
    from app.rules import bp as rules_bp
    from app.fines import bp as fines_bp
    from app.schedules import bp as schedules_bp
    from app.finance import bp as finance_bp

    for bp in (main_bp, auth_bp, groups_bp, members_bp, savings_bp, borrowers_bp, lending_bp, repayments_bp,
               feed_bp, community_bp, announcements_bp, reports_bp, notifications_bp, files_bp, platform_bp, imports_bp,
               shares_bp, rules_bp, fines_bp, schedules_bp, finance_bp):
        app.register_blueprint(bp)


def register_template_helpers(app):
    from app.models import ROLE_LABELS
    from app.services import money as m
    from app.services.loans import STATUS_LABELS
    from app.services.seed import get_setting
    from app.services.timeutil import to_local

    def _cur():
        grp = g.get("group")
        return (grp.currency, grp.currency_decimals) if grp else ("UGX", 0)

    app.jinja_env.globals.update(STATUS_LABELS=STATUS_LABELS, ROLE_LABELS=ROLE_LABELS)

    @app.template_global()
    def money(v):
        c, d = _cur()
        return m.fmt(v, c, d)

    @app.template_global()
    def money_c(v):
        c, d = _cur()
        return m.fmt_compact(v, c, d)

    @app.template_global()
    def money_plain(v):
        c, d = _cur()
        return m.fmt(v, c, d, prefix=False)

    @app.template_filter("pct")
    def pct(bp):
        return m.rate_pct(bp)

    @app.template_filter("ldt")
    def ldt(dt, fmt="%d %b %Y, %H:%M"):
        grp = g.get("group")
        loc = to_local(dt, grp.timezone if grp else "Africa/Kampala")
        return loc.strftime(fmt) if loc else ""

    @app.template_filter("ld")
    def ld(d, fmt="%d %b %Y"):
        return d.strftime(fmt) if d else ""

    @app.template_filter("ago")
    def ago(dt):
        from app.models import utcnow
        if not dt:
            return ""
        s = int((utcnow() - dt).total_seconds())
        if s < 60:
            return "just now"
        if s < 3600:
            return f"{s // 60}m ago"
        if s < 86400:
            return f"{s // 3600}h ago"
        if s < 86400 * 7:
            return f"{s // 86400}d ago"
        return ldt(dt, "%d %b %Y")

    @app.template_global()
    def group_logo_url(group):
        return url_for("files.serve", file_id=group.logo_file_id) if group and group.logo_file_id else None

    @app.template_global()
    def page_url(n):
        """Same page with a different ?page= (keeps filters and path params)."""
        args = request.args.to_dict()
        args["page"] = n
        return url_for(request.endpoint, **{**(request.view_args or {}), **args})

    @app.template_global()
    def url_with(**changes):
        """Current URL with query parameters replaced (empty value removes one)."""
        args = request.args.to_dict()
        args.pop("page", None)
        for k, v in changes.items():
            if v in (None, ""):
                args.pop(k, None)
            else:
                args[k] = v
        return url_for(request.endpoint, **{**(request.view_args or {}), **args})

    @app.template_global()
    def avatar_url(user):
        return url_for("files.serve", file_id=user.avatar_file_id) if user and user.avatar_file_id else None

    @app.context_processor
    def inject():
        unread, group_count, pending, resets = 0, 0, 0, 0
        if current_user.is_authenticated:
            from app.models import GroupMember, MemberApplication, Notification
            unread = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
            group_count = GroupMember.query.filter_by(user_id=current_user.id, status="ACTIVE").count()
            if g.get("group") is not None and g.get("is_official"):
                pending = MemberApplication.query.filter_by(group_id=g.group.id, status="PENDING").count()
                from app.models import PasswordResetRequest
                ids = db.session.query(GroupMember.user_id).filter(GroupMember.group_id == g.group.id,
                                                                  GroupMember.status == "ACTIVE")
                resets = PasswordResetRequest.query.filter(PasswordResetRequest.status == "PENDING",
                                                           PasswordResetRequest.user_id.in_(ids.scalar_subquery()),
                                                           PasswordResetRequest.user_id != current_user.id).count()
        return {
            "platform_name": get_setting("platform_name", "MY MONEY"),
            "company_line": "Developed and Powered by Emtrixz Technology Company.",
            "current_group": g.get("group"), "membership": g.get("membership"),
            "is_official": g.get("is_official", False), "unread_count": unread, "my_group_count": group_count,
            "pending_count_nav": pending, "reset_count_nav": resets, "ROLE_LABELS": ROLE_LABELS, "STATUS_LABELS": STATUS_LABELS,
        }


def register_hooks(app):
    @app.url_defaults
    def add_group_id(endpoint, values):
        """Group routes live under /g/<group_id>; inside a verified group request
        url_for fills the id in from the *verified* g.group, never from user input."""
        if "group_id" in values or g.get("group") is None:
            return
        if app.url_map.is_endpoint_expecting(endpoint, "group_id"):
            values["group_id"] = g.group.id

    @app.before_request
    def maintenance_mode():
        if request.endpoint in (None, "static", "main.health", "auth.login", "auth.logout"):
            return
        try:
            from app.services.seed import get_setting
            on = get_setting("maintenance_mode", "0") == "1"
        except Exception:
            return
        if on and not (current_user.is_authenticated and current_user.is_platform_admin):
            return render_template("errors/maintenance.html"), 503

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if app.config.get("IS_PRODUCTION"):
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.endpoint and request.endpoint != "static" and "files." not in request.endpoint:
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp


def register_errors(app):
    def wants_json():
        return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json" \
            or request.headers.get("X-Requested-With") == "fetch"

    for code, title, msg in ((403, "Access denied", "You do not have permission to do that."),
                             (404, "Page not found", "We could not find what you were looking for."),
                             (413, "File too large", "The uploaded file is too large."),
                             (429, "Too many requests", "Please slow down and try again shortly.")):
        def handler(e, code=code, title=title, msg=msg):
            if wants_json():
                return jsonify(error=msg), code
            return render_template("errors/error.html", code=code, title=title, message=msg), code
        app.register_error_handler(code, handler)

    @app.errorhandler(500)
    def server_error(e):
        db.session.rollback()
        if wants_json():
            return jsonify(error="Something went wrong."), 500
        return render_template("errors/error.html", code=500, title="Something went wrong",
                               message="An unexpected error occurred. Nothing was saved."), 500


def register_cli(app):
    from app.services.seed import seed_defaults

    @app.cli.command("seed")
    def seed_cmd():
        """Create roles, interest settings and platform defaults (idempotent)."""
        seed_defaults()
        click.echo("Defaults seeded.")

    @app.cli.command("create-platform-admin")
    @click.option("--name", prompt="Full name")
    @click.option("--phone", prompt="Phone number")
    @click.option("--email", default=lambda: app.config.get("PLATFORM_ADMIN_EMAIL") or None, prompt="Email")
    @click.password_option()
    def create_admin(name, phone, email, password):
        """Bootstrap the platform administrator (no default credentials exist)."""
        from app.auth import validate_password, normalize_phone
        from app.models import User
        from app.services.audit import audit
        from werkzeug.security import generate_password_hash

        seed_defaults()
        expected = app.config.get("PLATFORM_ADMIN_EMAIL")
        if expected and email.strip().lower() != expected.strip().lower():
            raise click.ClickException("Email must match PLATFORM_ADMIN_EMAIL.")
        err = validate_password(password)
        if err:
            raise click.ClickException(err)
        if User.query.filter_by(is_platform_admin=True).first():
            raise click.ClickException("A platform administrator already exists.")
        u = User(full_name=name.strip(), phone=normalize_phone(phone), email=email.strip().lower(),
                 password_hash=generate_password_hash(password), is_platform_admin=True)
        db.session.add(u)
        db.session.flush()
        audit("platform.admin_created", "user", u.id, user_id=u.id)
        db.session.commit()
        click.echo("Platform administrator created.")

    @app.cli.command("bootstrap-platform-admin")
    def bootstrap_admin():
        """Non-interactive, idempotent admin creation from environment variables."""
        import os
        from app.auth import validate_password, normalize_phone
        from app.models import User
        from app.services.audit import audit
        from werkzeug.security import generate_password_hash

        seed_defaults()
        if User.query.filter_by(is_platform_admin=True).first():
            click.echo("Platform administrator already exists; nothing to do.")
            return
        email = (os.environ.get("PLATFORM_ADMIN_EMAIL") or "").strip().lower()
        password = os.environ.get("PLATFORM_ADMIN_PASSWORD") or ""
        if not email or not password:
            click.echo("PLATFORM_ADMIN_EMAIL / PLATFORM_ADMIN_PASSWORD not set; skipping.")
            return
        err = validate_password(password)
        if err:
            raise click.ClickException(err)
        u = User(full_name=os.environ.get("PLATFORM_ADMIN_NAME", "Platform Admin"),
                 phone=normalize_phone(os.environ.get("PLATFORM_ADMIN_PHONE", "0700000000")),
                 email=email, password_hash=generate_password_hash(password),
                 is_platform_admin=True)
        db.session.add(u)
        db.session.flush()
        audit("platform.admin_created", "user", u.id, user_id=u.id)
        db.session.commit()
        click.echo("Platform administrator created.")
