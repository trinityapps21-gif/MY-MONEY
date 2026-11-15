import secrets
from datetime import date

from flask import g, request

from app.services.money import to_minor


def form_amount(name="amount"):
    return to_minor(request.form.get(name), g.group.currency_decimals)


def form_date(name, required=True, default=None):
    raw = (request.form.get(name) or "").strip()
    if not raw:
        if required and default is None:
            raise ValueError("Select a date.")
        return default
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValueError("Enter a valid date.")


def form_choice(name, allowed, label):
    v = (request.form.get(name) or "").strip()
    if v not in allowed:
        raise ValueError(f"Select a valid {label}.")
    return v


def form_text(name, maxlen=300):
    return (request.form.get(name) or "").strip()[:maxlen] or None


def new_idem_key():
    return secrets.token_hex(16)
