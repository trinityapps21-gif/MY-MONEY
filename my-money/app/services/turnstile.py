"""Cloudflare Turnstile (bot protection) for login, sign up and every password-reset form.

Set TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY (Cloudflare dashboard -> Turnstile).
With both set, every protected POST must carry a valid token or it is rejected.
Without them the check is switched off (local development and tests) and a warning
is logged at start-up in production.

Cloudflare's published test keys, for development only:
  site key   1x00000000000000000000AA  /  secret 1x0000000000000000000000000000000AA   (always passes)
  site key   2x00000000000000000000AB  /  secret 2x0000000000000000000000000000000AA   (always fails)
"""
import logging

import requests
from flask import current_app, flash, request

log = logging.getLogger("app.turnstile")

FIELD = "cf-turnstile-response"


def site_key() -> str:
    return (current_app.config.get("TURNSTILE_SITE_KEY") or "").strip()


def enabled() -> bool:
    return bool(site_key() and (current_app.config.get("TURNSTILE_SECRET_KEY") or "").strip())


def verify_token(token: str, remote_ip: str | None = None) -> tuple[bool, str]:
    """Ask Cloudflare whether a widget token is genuine. Fails closed on any error."""
    if not enabled():
        return True, ""
    token = (token or "").strip()
    if not token:
        return False, "Please complete the security check."
    if len(token) > 2048:
        return False, "The security check failed. Please try again."
    payload = {"secret": current_app.config["TURNSTILE_SECRET_KEY"], "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip
    try:
        res = requests.post(current_app.config["TURNSTILE_VERIFY_URL"], data=payload,
                            timeout=current_app.config.get("TURNSTILE_TIMEOUT", 6))
        data = res.json()
    except Exception:  # network error, bad JSON, timeout
        log.exception("Turnstile verification could not be completed")
        return False, "We could not run the security check. Please try again in a moment."
    if data.get("success") is True:
        return True, ""
    log.warning("Turnstile rejected a token: %s", data.get("error-codes"))
    return False, "The security check failed. Please try again."


def check_request(flash_errors: bool = True) -> bool:
    """Verify the token posted with the current form. Flashes the reason when it fails."""
    ok, msg = verify_token(request.form.get(FIELD) or (request.get_json(silent=True) or {}).get(FIELD),
                           request.remote_addr)
    if not ok and flash_errors:
        flash(msg, "error")
    return ok
