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
    PERMANENT_SESSION_LIFETIME = timedelta(hours=12)
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_DURATION = timedelta(days=7)
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
                "pool_pre_ping": True,
                "pool_recycle": 240,
            }
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
