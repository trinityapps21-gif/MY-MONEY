"""Fingerprint / face / device-unlock sign-in for the installed phone app (WebAuthn passkeys).

A user enrols once while signed in (they must re-enter their password). After that the login page offers
"Sign in with fingerprint or face". Passwords keep working. Cloudflare Turnstile is checked before a
biometric sign-in challenge is issued, exactly as for a password login.

Only the public key is stored. The phone checks the fingerprint/face itself (user verification is REQUIRED)
and signs a one-time challenge; the server verifies that signature.
"""
import json
import time

from flask import Blueprint, Response, current_app, flash, jsonify, redirect, request, session, url_for
from flask_login import current_user, login_required
from webauthn import (generate_authentication_options, generate_registration_options, options_to_json,
                      verify_authentication_response, verify_registration_response)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorAttachment, AuthenticatorSelectionCriteria,
                                      PublicKeyCredentialDescriptor, ResidentKeyRequirement,
                                      UserVerificationRequirement)
from werkzeug.security import check_password_hash

from app.extensions import db, limiter
from app.models import User, WebAuthnCredential, utcnow
from app.services import turnstile
from app.services.audit import audit

bp = Blueprint("biometric", __name__)

MAX_CREDENTIALS = 5
CHALLENGE_SECONDS = 180


def rp_id() -> str:
    return current_app.config.get("WEBAUTHN_RP_ID") or request.host.split(":")[0]


def origin() -> str:
    return current_app.config.get("WEBAUTHN_ORIGIN") or request.host_url.rstrip("/")


def _json_options(options) -> Response:
    return Response(options_to_json(options), mimetype="application/json")


def _fresh(key):
    """Pop a stored challenge; None when missing or older than CHALLENGE_SECONDS."""
    data = session.pop(key, None)
    if not data or time.time() - data.get("t", 0) > CHALLENGE_SECONDS:
        return None
    return base64url_to_bytes(data["c"])


def _store(key, challenge: bytes):
    session[key] = {"c": bytes_to_base64url(challenge), "t": time.time()}


def revoke_all(user):
    """Remove every enrolled phone for a user (used when their password is reset)."""
    WebAuthnCredential.query.filter_by(user_id=user.id).delete()


# ------------------------------------------------------------------ enrol (signed in)
@bp.route("/account/biometric/register/options", methods=["POST"])
@login_required
@limiter.limit("10 per hour")
def register_options():
    body = request.get_json(silent=True) or {}
    if not check_password_hash(current_user.password_hash, body.get("current_password") or ""):
        return jsonify(error="Your current password is incorrect."), 400
    existing = WebAuthnCredential.query.filter_by(user_id=current_user.id).all()
    if len(existing) >= MAX_CREDENTIALS:
        return jsonify(error=f"You can enable biometric sign-in on up to {MAX_CREDENTIALS} devices. Remove one first."), 400
    opts = generate_registration_options(
        rp_id=rp_id(), rp_name=current_app.config["WEBAUTHN_RP_NAME"],
        user_id=str(current_user.id).encode(), user_name=current_user.email or current_user.phone,
        user_display_name=current_user.full_name,
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id)) for c in existing],
        authenticator_selection=AuthenticatorSelectionCriteria(
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,       # the phone's own fingerprint / face sensor
            resident_key=ResidentKeyRequirement.REQUIRED,                    # lets the phone offer the account without typing
            user_verification=UserVerificationRequirement.REQUIRED))
    _store("wa_reg", opts.challenge)
    return _json_options(opts)


@bp.route("/account/biometric/register/verify", methods=["POST"])
@login_required
@limiter.limit("10 per hour")
def register_verify():
    body = request.get_json(silent=True) or {}
    challenge = _fresh("wa_reg")
    if not challenge or not body.get("credential"):
        return jsonify(error="That took too long. Please try again."), 400
    try:
        ver = verify_registration_response(credential=body["credential"], expected_challenge=challenge,
                                           expected_rp_id=rp_id(), expected_origin=origin(),
                                           require_user_verification=True)
    except Exception:
        current_app.logger.warning("Biometric enrolment failed verification", exc_info=True)
        return jsonify(error="We could not verify this device. Please try again."), 400
    cid = bytes_to_base64url(ver.credential_id)
    if WebAuthnCredential.query.filter_by(credential_id=cid).first():
        return jsonify(error="This device is already enabled."), 400
    label = (body.get("label") or "").strip()[:80] or "This device"
    db.session.add(WebAuthnCredential(user_id=current_user.id, credential_id=cid,
                                      public_key=bytes_to_base64url(ver.credential_public_key),
                                      sign_count=ver.sign_count, label=label))
    audit("auth.biometric_enabled", "user", current_user.id)
    db.session.commit()
    flash("Biometric sign-in is on for this device.", "success")
    return jsonify(ok=True)


@bp.route("/account/biometric/<int:cred_id>/delete", methods=["POST"])
@login_required
def delete(cred_id):
    cred = WebAuthnCredential.query.filter_by(id=cred_id, user_id=current_user.id).first_or_404()
    db.session.delete(cred)
    audit("auth.biometric_removed", "user", current_user.id)
    db.session.commit()
    flash("Biometric sign-in removed for that device.", "success")
    return redirect(request.referrer or url_for("groups.profile"))


# ------------------------------------------------------------------ sign in (not signed in)
@bp.route("/login/biometric/options", methods=["POST"])
@limiter.limit("20 per minute; 100 per hour")
def login_options():
    if current_user.is_authenticated:
        return jsonify(error="You are already signed in."), 400
    body = request.get_json(silent=True) or {}
    ok, msg = turnstile.verify_token(body.get(turnstile.FIELD), request.remote_addr)  # same bot check as a password login
    if not ok:
        return jsonify(error=msg, turnstile=True), 400
    opts = generate_authentication_options(rp_id=rp_id(), user_verification=UserVerificationRequirement.REQUIRED)
    _store("wa_auth", opts.challenge)
    return _json_options(opts)


@bp.route("/login/biometric/verify", methods=["POST"])
@limiter.limit("20 per minute; 100 per hour")
def login_verify():
    from app.auth import _login, home_for, safe_next
    body = request.get_json(silent=True) or {}
    cred_json = body.get("credential")
    challenge = _fresh("wa_auth")
    generic = jsonify(error="Biometric sign-in did not work. Use your password instead."), 400
    if not challenge or not isinstance(cred_json, dict) or not cred_json.get("id"):
        return generic
    stored = WebAuthnCredential.query.filter_by(credential_id=str(cred_json["id"])).first()
    if stored is None:
        return generic
    user = db.session.get(User, stored.user_id)
    try:
        handle = ((cred_json.get("response") or {}).get("userHandle"))
        if handle and base64url_to_bytes(handle) != str(stored.user_id).encode():
            return generic
        ver = verify_authentication_response(
            credential=cred_json, expected_challenge=challenge, expected_rp_id=rp_id(), expected_origin=origin(),
            credential_public_key=base64url_to_bytes(stored.public_key),
            credential_current_sign_count=int(stored.sign_count or 0), require_user_verification=True)
    except Exception:
        current_app.logger.warning("Biometric sign-in failed verification", exc_info=True)
        return generic
    if not user or user.status != "ACTIVE":
        return jsonify(error="This account has been suspended. Contact your group administrator."), 403
    stored.sign_count, stored.last_used_at = ver.new_sign_count, utcnow()
    _login(user)
    audit("auth.login_biometric", "user", user.id, user_id=user.id)
    db.session.commit()
    return jsonify(ok=True, redirect=safe_next(body.get("next")) or home_for(user))
