"""MY MONEY configuration. Developed and Powered by Emtrixz Technology Company.

Production uses one central Turso (libSQL) database. A local SQLite file is only
used for development and automated tests and is refused in production.
"""
import os
import secrets
from datetime import timedelta

from dotenv import load_dotenv

load_dotenv()


def is_production() -> bool:
    return os.environ.get("FLASK_ENV", "development").lower() == "production"


def turso_uri() -> tuple[str, dict]:
    """Return (sqlalchemy_uri, connect_args) for the Turso database."""
    url = os.environ.get("TURSO_DATABASE_URL", "").strip()
    token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    host = url.replace("libsql://", "").replace("https://", "").replace("wss://", "").rstrip("/")
    return f"sqlite+libsql://{host}?secure=true", {"auth_token": token}


class Config:
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MAX_CONTENT_LENGTH = 30 * 1024 * 1024  # whole-request ceiling (several photos per post)
    LOGO_MAX_BYTES = 2 * 1024 * 1024
    ATTACHMENT_MAX_BYTES = 5 * 1024 * 1024  # per file
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Sliding window: the cookie is renewed on every request, so 30 idle minutes end the session.
    PERMANENT_SESSION_LIFETIME = timedelta(minutes=max(1, int(os.environ.get("IDLE_TIMEOUT_MINUTES", "30") or 30)))
    SESSION_REFRESH_EACH_REQUEST = True
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_DURATION = timedelta(days=7)
    SEND_FILE_MAX_AGE_DEFAULT = 31536000  # /static files are versioned (?v=...), so they can be cached for a year
    WTF_CSRF_TIME_LIMIT = 60 * 60 * 8
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_HEADERS_ENABLED = True
    PLATFORM_ADMIN_EMAIL = os.environ.get("PLATFORM_ADMIN_EMAIL", "")
    # ---- File storage: two external providers behind one interface -------------
    # Uploadcare = ALL images (logos, avatars, community/announcement images)
    # ImageKit   = documents and non-image attachments (PDF, Word, statements, receipts)
    # Turso      = file metadata only (table stored_files)
    STORAGE_BACKEND = "providers"  # "memory" is for automated tests only
    UPLOADCARE_PUBLIC_KEY = os.environ.get("UPLOADCARE_PUBLIC_KEY", "")
    UPLOADCARE_SECRET_KEY = os.environ.get("UPLOADCARE_SECRET_KEY", "")
    UPLOADCARE_UPLOAD_URL = os.environ.get("UPLOADCARE_UPLOAD_URL", "https://upload.uploadcare.com/base/")
    UPLOADCARE_API_URL = os.environ.get("UPLOADCARE_API_URL", "https://api.uploadcare.com")
    UPLOADCARE_CDN_BASE = os.environ.get("UPLOADCARE_CDN_BASE", "https://ucarecdn.com").rstrip("/")
    UPLOADCARE_SIGNED_UPLOADS = os.environ.get("UPLOADCARE_SIGNED_UPLOADS", "0") == "1"
    IMAGEKIT_PRIVATE_KEY = os.environ.get("IMAGEKIT_PRIVATE_KEY", "")
    IMAGEKIT_PUBLIC_KEY = os.environ.get("IMAGEKIT_PUBLIC_KEY", "")
    IMAGEKIT_URL_ENDPOINT = os.environ.get("IMAGEKIT_URL_ENDPOINT", "").rstrip("/")
    IMAGEKIT_UPLOAD_URL = os.environ.get("IMAGEKIT_UPLOAD_URL", "https://upload.imagekit.io/api/v1/files/upload")
    IMAGEKIT_API_URL = os.environ.get("IMAGEKIT_API_URL", "https://api.imagekit.io/v1")
    SIGNED_URL_SECONDS = int(os.environ.get("SIGNED_URL_SECONDS", "300") or 300)
    STORAGE_HTTP_TIMEOUT = 30
    # Optional SMTP for password reset emails
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or 587)
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_FROM = os.environ.get("SMTP_FROM", "")
    # ---- Cloudflare Turnstile (bot check on login, sign up and password reset) ----
    TURNSTILE_SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY", "").strip()
    TURNSTILE_SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY", "").strip()
    TURNSTILE_VERIFY_URL = os.environ.get("TURNSTILE_VERIFY_URL", "https://challenges.cloudflare.com/turnstile/v0/siteverify")
    TURNSTILE_TIMEOUT = 6
    # ---- Sessions: automatic sign-out after inactivity (web and installed app) ----
    IDLE_TIMEOUT_MINUTES = max(1, int(os.environ.get("IDLE_TIMEOUT_MINUTES", "30") or 30))
    IDLE_WARNING_SECONDS = 60
    # ---- Biometric / device unlock (WebAuthn passkeys) ----
    # Credentials are tied to the website address; set WEBAUTHN_RP_ID (e.g. joinmymoney.com) if the app
    # is reachable on more than one address, and WEBAUTHN_ORIGIN to the exact https origin users open.
    WEBAUTHN_RP_ID = os.environ.get("WEBAUTHN_RP_ID", "").strip()
    WEBAUTHN_ORIGIN = os.environ.get("WEBAUTHN_ORIGIN", "").strip().rstrip("/")
    WEBAUTHN_RP_NAME = os.environ.get("WEBAUTHN_RP_NAME", "MY MONEY")
    # 1 = offer biometric sign-in only inside the installed app (home-screen app); 0 = browsers too
    BIOMETRIC_INSTALLED_APP_ONLY = os.environ.get("BIOMETRIC_INSTALLED_APP_ONLY", "1") == "1"
    # ---- Speed ----
    DB_POOL_PRE_PING = os.environ.get("DB_POOL_PRE_PING", "1") == "1"
    STATIC_MAX_AGE = 31536000
    TESTING = False

    @classmethod
    def build(cls) -> dict:
        prod = is_production()
        cfg = {k: v for k, v in cls.__dict__.items() if k.isupper()}
        cfg["APP_BASE_URL"] = os.environ.get("APP_BASE_URL", "").strip()      # https://your-app.onrender.com (used in SMS links)
        secret = os.environ.get("SECRET_KEY", "").strip()
        if prod:
            missing = [
                name for name in ("SECRET_KEY", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN")
                if not os.environ.get(name, "").strip()
            ]
            if missing:
                raise RuntimeError(
                    "Missing required environment variables for production: " + ", ".join(missing)
                )
            uri, connect_args = turso_uri()
            cfg["SESSION_COOKIE_SECURE"] = True
            cfg["REMEMBER_COOKIE_SECURE"] = True
            cfg["PREFERRED_URL_SCHEME"] = "https"
            cfg["SQLALCHEMY_ENGINE_OPTIONS"] = {
                "connect_args": connect_args,
                "pool_pre_ping": cls.DB_POOL_PRE_PING,
                "pool_recycle": 240,
            }
            if not (cfg.get("TURNSTILE_SITE_KEY") and cfg.get("TURNSTILE_SECRET_KEY")):
                import logging
                logging.getLogger("app.turnstile").warning(
                    "TURNSTILE_SITE_KEY / TURNSTILE_SECRET_KEY are not set: login, sign up and reset forms are NOT bot-protected.")
        else:
            if os.environ.get("TURSO_DATABASE_URL"):
                uri, connect_args = turso_uri()
                cfg["SQLALCHEMY_ENGINE_OPTIONS"] = {"connect_args": connect_args, "pool_pre_ping": True}
            else:
                os.makedirs("instance", exist_ok=True)
                uri = "sqlite:///" + os.path.abspath(os.path.join("instance", "dev.db"))
            secret = secret or secrets.token_hex(32)  # dev only; sessions reset on restart
        cfg["SECRET_KEY"] = secret
        cfg["SQLALCHEMY_DATABASE_URI"] = uri
        return cfg
