from flask import Blueprint, jsonify, render_template
from sqlalchemy import text

from app.extensions import db
from app.services.seed import get_setting

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    return render_template("public/landing.html")


@bp.route("/about")
def about():
    return render_template("public/about.html")


@bp.route("/contact")
def contact():
    return render_template("public/contact.html", contact_email=get_setting("contact_email", ""))


@bp.route("/health")
def health():
    """Render health check: confirms the app can reach the database."""
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify(status="ok"), 200
    except Exception:
        return jsonify(status="database_unavailable"), 503
