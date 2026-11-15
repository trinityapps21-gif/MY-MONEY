import smtplib
from email.message import EmailMessage

from flask import current_app


def send_email(to: str, subject: str, body: str) -> bool:
    """Send via SMTP when configured. Without SMTP the message is only logged
    (never shown to the requester), so password reset needs SMTP in production."""
    cfg = current_app.config
    if not (cfg.get("SMTP_HOST") and cfg.get("SMTP_FROM")):
        current_app.logger.warning("SMTP not configured; email to %s not sent (subject: %s)", to, subject)
        if not current_app.config.get("IS_PRODUCTION"):
            current_app.logger.info("DEV EMAIL to %s\n%s\n%s", to, subject, body)
        return False
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg["SMTP_FROM"], to, subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=15) as s:
            s.starttls()
            if cfg.get("SMTP_USER"):
                s.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            s.send_message(msg)
        return True
    except Exception:  # pragma: no cover
        current_app.logger.exception("Failed to send email")
        return False
