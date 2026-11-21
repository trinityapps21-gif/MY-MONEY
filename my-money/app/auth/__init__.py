import re
import time
from urllib.parse import urlparse

import secrets

from flask import Blueprint, Response, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db, limiter
from app.models import GroupMember, User, utcnow
from app.services.audit import audit
from app.services import turnstile
from app.services.seed import get_setting, seed_defaults

bp = Blueprint("auth", __name__)

_DUMMY_HASH = generate_password_hash("not-a-real-password")


def normalize_phone(raw: str) -> str:
    raw = (raw or "").strip()
    plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    return ("+" if plus else "") + digits


def valid_phone(p: str) -> bool:
    return 7 <= len(re.sub(r"\D", "", p)) <= 15


def validate_password(pw: str):
    if len(pw or "") < 8:
        return "Password must be at least 8 characters."
    if not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
        return "Password must contain at least one letter and one number."
    return None


def register_user(full_name, phone, email, password, confirm):
    """Validate and create a user (no commit). Returns (user, error)."""
    full_name, phone, email = (full_name or "").strip(), normalize_phone(phone), (email or "").strip().lower() or None
    if len(full_name) < 2:
        return None, "Enter your full name."
    if not valid_phone(phone):
        return None, "Enter a valid phone number."
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return None, "Enter a valid email address."
    if password != confirm:
        return None, "Passwords do not match."
    err = validate_password(password)
    if err:
        return None, err
    if User.query.filter_by(phone=phone).first() or (email and User.query.filter_by(email=email).first()):
        return None, "An account with this phone number or email already exists. Please sign in instead."
    u = User(full_name=full_name, phone=phone, email=email, password_hash=generate_password_hash(password))
    db.session.add(u)
    db.session.flush()
    return u, None


def safe_next(target):
    if not target:
        return None
    p = urlparse(target)
    return target if not p.netloc and not p.scheme and target.startswith("/") and not target.startswith("//") else None


def home_for(user):
    if user.is_platform_admin:
        return url_for("platform.dashboard")
    return url_for("groups.select")


def _login(user):
    """Start a session. No "remember me" cookie: sessions end after 30 idle minutes (see enforce_idle_timeout)."""
    login_user(user, remember=False)
    session.permanent = True
    session["last_activity"] = int(time.time())
    user.last_login_at = utcnow()
    audit("auth.login", "user", user.id, user_id=user.id)
    db.session.commit()


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute; 60 per hour", methods=["POST"])
def login():
    return _login_view("auth/login.html")


@bp.route("/login/lending", methods=["GET", "POST"])
@limiter.limit("10 per minute; 60 per hour", methods=["POST"])
def login_lending():
    """Same account system and security as /login, with the lending-business look and wording."""
    return _login_view("auth/login_lending.html")


def _login_view(template):
    if current_user.is_authenticated:
        return redirect(home_for(current_user))
    if request.method == "POST" and not turnstile.check_request():
        return render_template(template), 400
    if request.method == "POST":
        ident = (request.form.get("identifier") or "").strip()
        password = request.form.get("password") or ""
        user = User.query.filter((User.email == ident.lower()) | (User.phone == normalize_phone(ident))).first() \
            if ident else None
        ok = check_password_hash(user.password_hash if user else _DUMMY_HASH, password)  # constant-ish time
        if user and ok:
            if user.status != "ACTIVE":
                flash("This account has been suspended. Contact your group administrator.", "error")
            else:
                _login(user)
                return redirect(safe_next(request.args.get("next")) or home_for(user))
        else:
            flash("Incorrect phone/email or password.", "error")
    if request.method == "GET" and request.args.get("idle") == "1":
        flash(f"You were signed out after {current_app.config['IDLE_TIMEOUT_MINUTES']} minutes of inactivity. Please sign in again.", "info")
    return render_template(template)


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    audit("auth.logout", "user", current_user.id)
    db.session.commit()
    logout_user()
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("main.index"))


@bp.route("/register", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def register():
    """Group registration: create an account (or use the signed-in one) and a new
    savings group; the creator becomes Group Administrator and owner."""
    from app.groups import create_group_from_form, group_form_context

    if get_setting("allow_group_registration", "1") != "1":
        flash("New group registration is currently closed.", "error")
        return redirect(url_for("main.index"))
    if request.method == "POST" and not current_user.is_authenticated and not turnstile.check_request():
        return render_template("auth/register.html", **group_form_context()), 400
    if request.method == "POST":
        seed_defaults()
        user = current_user if current_user.is_authenticated else None
        err = None
        if user is None:
            user, err = register_user(request.form.get("full_name"), request.form.get("phone"),
                                      request.form.get("email"), request.form.get("password") or "",
                                      request.form.get("confirm_password") or "")
        if not err:
            form = request.form.copy()
            form["business_type"] = "SAVINGS_GROUP"      # lending businesses register at /register/lending (needs approval)
            group, err = create_group_from_form(user, form, request.files.get("logo"))
        if err:
            db.session.rollback()
            flash(err, "error")
        else:
            db.session.commit()
            if not current_user.is_authenticated:
                _login(user)
            flash("Your group has been created. You are the Group Administrator.", "success")
            return redirect(url_for("groups.dashboard", group_id=group.id))
    return render_template("auth/register.html", **group_form_context())


@bp.route("/register/lending", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def register_lending():
    """Lending-business registration. Different details from a savings group (owner's National ID and photo,
    licence, address) and the business goes online only after the platform administrator approves it."""
    from app.groups import LENDER_KINDS, create_lending_from_form, group_form_context

    if get_setting("allow_group_registration", "1") != "1":
        flash("New registration is currently closed.", "error")
        return redirect(url_for("main.index"))
    ctx = {**group_form_context(), "kinds": LENDER_KINDS}
    if request.method == "POST" and not current_user.is_authenticated and not turnstile.check_request():
        return render_template("auth/register_lending.html", **ctx), 400
    if request.method == "POST":
        seed_defaults()
        user = current_user if current_user.is_authenticated else None
        err = None
        if user is None:
            user, err = register_user(request.form.get("full_name"), request.form.get("phone"),
                                      request.form.get("email"), request.form.get("password") or "",
                                      request.form.get("confirm_password") or "")
        if not err:
            group, err = create_lending_from_form(user, request.form, request.files.get("owner_id_photo"),
                                                  request.files.get("logo"))
        if err:
            db.session.rollback()
            flash(err, "error")
        else:
            db.session.commit()
            if not current_user.is_authenticated:
                _login(user)
            flash("Thank you. Your business has been sent for approval. You will be able to use it as soon as the "
                  "platform administrator approves it.", "success")
            return redirect(url_for("groups.select"))
    return render_template("auth/register_lending.html", **ctx)


# ---------------------------------------------------------------- password reset
# Forgot password = ask the group. The member files a request; a group official
# checks it is really that person and then sends a reset link. Anyone who still
# has their recovery code can reset on their own at /recover.
RESET_LINK_HOURS = 24


def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="password-reset")


def make_reset_link(req):
    """Signed link tied to one APPROVED request; it dies once the password changes."""
    token = _serializer().dumps({"id": req.user_id, "h": req.user.password_hash[-12:], "r": req.id})
    return url_for("auth.reset_password", token=token, _external=True)


def revoke_biometrics(user):
    """A password reset means the account may have been at risk: phones must be enrolled again."""
    from app.biometric import revoke_all
    revoke_all(user)


def _find_user(ident):
    ident = (ident or "").strip()
    if not ident:
        return None
    return User.query.filter((User.email == ident.lower()) | (User.phone == normalize_phone(ident))).first()


@bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def forgot_password():
    from app.models import PasswordResetRequest
    from app.services import notify
    if request.method == "POST" and not turnstile.check_request():
        return render_template("auth/forgot.html"), 400
    if request.method == "POST":
        user = _find_user(request.form.get("identifier"))
        note = (request.form.get("note") or "").strip()[:300] or None
        if user and user.status == "ACTIVE":
            memberships = [m for m in GroupMember.query.filter_by(user_id=user.id, status="ACTIVE").all()
                           if m.group.status == "ACTIVE"]
            pending = PasswordResetRequest.query.filter_by(user_id=user.id, status="PENDING").first()
            if memberships and not pending:
                req = PasswordResetRequest(user_id=user.id, note=note)
                db.session.add(req)
                db.session.flush()
                for m in memberships:
                    notify.notify(notify.official_ids(m.group_id, exclude=user.id), m.group_id, "MEMBERSHIP",
                                  "Password reset request",
                                  f"{user.full_name} says they forgot their password. Confirm it is really them "
                                  "before sending a reset link.", f"/g/{m.group_id}/members/password-resets")
                    audit("auth.reset_requested", "user", user.id, group_id=m.group_id, user_id=user.id)
                db.session.commit()
        # Same response whether or not the account exists (no account enumeration).
        flash("Request received. If the account exists, a group administrator has been asked to confirm it is you "
              "and send a reset link. You can also use your recovery code.", "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot.html")


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def reset_password(token):
    from app.models import PasswordResetRequest
    try:
        data = _serializer().loads(token, max_age=RESET_LINK_HOURS * 3600)
        user = db.session.get(User, data["id"])
        req = db.session.get(PasswordResetRequest, data["r"])
        if not user or user.password_hash[-12:] != data["h"]:  # token dies once the password changes
            raise BadSignature("stale")
        if not req or req.user_id != user.id or req.status != "APPROVED":  # only admin-approved links work
            raise BadSignature("not approved")
    except (BadSignature, SignatureExpired, KeyError):
        flash("This reset link is invalid or has expired. Ask your group administrator for a new one.", "error")
        return redirect(url_for("auth.forgot_password"))
    if request.method == "POST" and not turnstile.check_request():
        return render_template("auth/reset.html", token=token), 400
    if request.method == "POST":
        pw, confirm = request.form.get("password") or "", request.form.get("confirm_password") or ""
        err = "Passwords do not match." if pw != confirm else validate_password(pw)
        if err:
            flash(err, "error")
        else:
            user.password_hash = generate_password_hash(pw)
            revoke_biometrics(user)
            req.status, req.completed_at = "COMPLETED", utcnow()
            audit("auth.password_reset", "user", user.id, user_id=user.id, group_id=req.decided_group_id)
            db.session.commit()
            flash("Password updated. You can now sign in.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/reset.html", token=token)


# ---------------------------------------------------------------- recovery code
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I confusion


def new_recovery_code():
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(20))
    return "-".join(raw[i:i + 5] for i in range(0, 20, 5))


def normalize_code(raw):
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


def recovery_code_ok(user, raw):
    code = normalize_code(raw)
    return bool(user and user.recovery_code_hash and len(code) == 20 and check_password_hash(user.recovery_code_hash, code))


_CODE_PAGES = ("auth.recovery_code", "auth.recovery_code_download", "auth.recovery_code_confirm", "auth.logout")


@bp.before_app_request
def require_recovery_code():
    """First sign-in: every account must save a recovery code before using the app."""
    if request.method != "GET" or not current_user.is_authenticated or current_user.recovery_code_hash:
        return None
    ep = request.endpoint or ""
    if ep in _CODE_PAGES or ep in ("static", "main.health") or ep.startswith("files.") \
            or request.headers.get("X-Requested-With") == "fetch" or request.path.startswith("/notifications/recent"):
        return None
    return redirect(url_for("auth.recovery_code"))


@bp.route("/account/recovery-code")
@login_required
def recovery_code():
    code = session.get("pending_recovery_code")
    if not code:
        if current_user.recovery_code_hash:
            return redirect(home_for(current_user))
        code = session["pending_recovery_code"] = new_recovery_code()
    return render_template("auth/recovery_code.html", code=code, replacing=bool(current_user.recovery_code_hash))


@bp.route("/account/recovery-code/download")
@login_required
def recovery_code_download():
    code = session.get("pending_recovery_code")
    if not code:
        return redirect(url_for("auth.recovery_code"))
    body = (f"MY MONEY - ACCOUNT RECOVERY CODE\n\nAccount: {current_user.full_name} ({current_user.phone})\n"
            f"Recovery code: {code}\n\nKeep this file private. If you forget your password you can reset it at\n"
            f"{url_for('auth.recover', _external=True)}\nEach code works once; a new one is issued after use.\n")
    return Response(body, mimetype="text/plain",
                    headers={"Content-Disposition": "attachment; filename=my-money-recovery-code.txt"})


@bp.route("/account/recovery-code/confirm", methods=["POST"])
@login_required
def recovery_code_confirm():
    code = session.get("pending_recovery_code")
    if not code or not request.form.get("saved"):
        flash("Save your recovery code, then tick the box to continue.", "error")
        return redirect(url_for("auth.recovery_code"))
    current_user.recovery_code_hash = generate_password_hash(normalize_code(code))
    current_user.recovery_code_created_at = utcnow()
    audit("auth.recovery_code_issued", "user", current_user.id)
    db.session.commit()
    session.pop("pending_recovery_code", None)
    flash("Recovery code saved. Keep it somewhere safe.", "success")
    return redirect(home_for(current_user))


@bp.route("/account/recovery-code/new", methods=["POST"])
@login_required
@limiter.limit("5 per hour")
def new_recovery_code_request():
    if not check_password_hash(current_user.password_hash, request.form.get("current_password") or ""):
        flash("Your current password is incorrect.", "error")
        return redirect(url_for("groups.profile"))
    session["pending_recovery_code"] = new_recovery_code()
    return redirect(url_for("auth.recovery_code"))


@bp.route("/recover", methods=["GET", "POST"])
@limiter.limit("5 per hour; 20 per day", methods=["POST"])
def recover():
    if request.method == "POST" and not turnstile.check_request():
        return render_template("auth/recover.html"), 400
    if request.method == "POST":
        user = _find_user(request.form.get("identifier"))
        pw, confirm = request.form.get("password") or "", request.form.get("confirm_password") or ""
        err = "Passwords do not match." if pw != confirm else validate_password(pw)
        if err:
            flash(err, "error")
        else:
            if user is None:
                check_password_hash(_DUMMY_HASH, "x")  # keep timing similar
            if user and user.status == "ACTIVE" and recovery_code_ok(user, request.form.get("recovery_code")):
                user.password_hash = generate_password_hash(pw)
                revoke_biometrics(user)
                user.recovery_code_hash = user.recovery_code_created_at = None  # single use: a new one is issued at sign-in
                audit("auth.password_reset_recovery_code", "user", user.id, user_id=user.id)
                db.session.commit()
                flash("Password updated. Sign in; you will be given a new recovery code.", "success")
                return redirect(url_for("auth.login"))
            flash("The phone/email or recovery code is not correct.", "error")
    return render_template("auth/recover.html")


@bp.route("/account/password", methods=["POST"])
@login_required
@limiter.limit("10 per hour")
def change_password():
    cur, pw, confirm = request.form.get("current_password") or "", request.form.get("password") or "", \
        request.form.get("confirm_password") or ""
    if not check_password_hash(current_user.password_hash, cur):
        flash("Your current password is incorrect.", "error")
    elif pw != confirm:
        flash("New passwords do not match.", "error")
    elif validate_password(pw):
        flash(validate_password(pw), "error")
    else:
        current_user.password_hash = generate_password_hash(pw)
        audit("auth.password_changed", "user", current_user.id)
        db.session.commit()
        flash("Password changed.", "success")
    return redirect(request.referrer or url_for("groups.select"))


@bp.route("/session/keepalive", methods=["POST"])
@login_required
@limiter.limit("60 per hour")
def keepalive():
    """Called by the page while the person is really using it (and from the 'Stay signed in' prompt).
    The idle-timeout hook refreshes the session clock on every normal request, including this one."""
    return jsonify(ok=True)
