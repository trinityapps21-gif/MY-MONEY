import os

from flask import Blueprint, Response, current_app, jsonify, render_template, request, send_from_directory
from sqlalchemy import text

from app.extensions import csrf, db
from app.services.seed import get_setting

bp = Blueprint("main", __name__)

# Used only if no real sw.js file can be found, so the browser never gets a 404.
_FALLBACK_SW = (
    "self.addEventListener('install', function () { self.skipWaiting(); });\n"
    "self.addEventListener('activate', function (e) { e.waitUntil(self.clients.claim()); });\n"
)


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
    """Serve the PWA service worker from the site root so it can control every page.
    Looks for the real file in a few likely places; falls back to a minimal worker."""
    static = current_app.static_folder
    app_dir = current_app.root_path
    candidates = (
        static,
        os.path.join(static, "js"),
        app_dir,
        os.path.dirname(app_dir),
    )
    resp = None
    for directory in candidates:
        if os.path.isfile(os.path.join(directory, "sw.js")):
            resp = send_from_directory(directory, "sw.js", mimetype="application/javascript")
            break
    if resp is None:
        current_app.logger.warning("sw.js file not found; serving minimal fallback service worker")
        resp = Response(_FALLBACK_SW, mimetype="application/javascript")
    # A service worker must always be re-checked by the browser so updates roll out.
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp
