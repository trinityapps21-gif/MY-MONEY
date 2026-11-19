import os

from flask import Blueprint, Response, current_app, jsonify, redirect, render_template, request, send_from_directory, url_for
from flask_login import current_user, login_required
from sqlalchemy import text

from app.extensions import csrf, db
from app.services.seed import get_setting

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    return render_template("public/landing.html")


@bp.route("/savings", strict_slashes=False)
@login_required
def savings_shortcut():
    """/savings has no group in it. Send the person to their own savings page when they belong to one
    group, or to the group list to pick one."""
    from app.models import GroupMember
    rows = GroupMember.query.filter_by(user_id=current_user.id, status="ACTIVE").limit(2).all()
    if len(rows) == 1 and rows[0].group.business_type == "SAVINGS_GROUP":
        return redirect(url_for("savings.mine", group_id=rows[0].group_id))
    return redirect(url_for("groups.select"))


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


@bp.app_context_processor
def seo_context():
    """site_url: the public address of the site. Set SITE_URL (e.g. https://joinmymoney.com) in Render so
    canonical links, the sitemap and social previews always use your main domain; otherwise the visited host is used."""
    return {"site_url": (os.environ.get("SITE_URL") or request.url_root).strip().rstrip("/")}


@bp.route("/robots.txt")
def robots():
    base = (os.environ.get("SITE_URL") or request.url_root).strip().rstrip("/")
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /g/\n"
        "Disallow: /platform\n"
        "Disallow: /files/\n"
        "Disallow: /api/\n"
        f"Sitemap: {base}/sitemap.xml\n"
    )
    resp = Response(body, mimetype="text/plain")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@bp.route("/sitemap.xml")
def sitemap():
    base = (os.environ.get("SITE_URL") or request.url_root).strip().rstrip("/")
    pages = [("/", "1.0"), ("/about", "0.7"), ("/contact", "0.6"), ("/register", "0.8"), ("/login", "0.5")]
    items = "".join(f"<url><loc>{base}{path}</loc><priority>{prio}</priority></url>" for path, prio in pages)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</urlset>'
    resp = Response(xml, mimetype="application/xml")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@bp.route("/favicon.ico")
def favicon():
    """Google and browsers ask for /favicon.ico by default."""
    resp = send_from_directory(os.path.join(current_app.static_folder, "img"), "favicon.ico", mimetype="image/x-icon")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@bp.route("/sw.js")
def service_worker():
    """The service worker must live at the site root so it can control every page.
    (It was registered at /sw.js but nothing served that address, so the app never cached anything.)"""
    resp = send_from_directory(current_app.static_folder, "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp
