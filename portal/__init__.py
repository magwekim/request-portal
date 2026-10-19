"""Public Service Request Portal: the application factory.

Run it locally with:  python run.py   (or:  flask --app portal run)
"""
import os
from datetime import datetime, timedelta, timezone

from flask import Flask, g, render_template, request, session
from werkzeug.exceptions import HTTPException

from . import database, security, workflow

# The project root, where run.py and .env live.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_dotenv(path):
    """Read KEY=VALUE lines from a .env file. Real environment variables always win."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def create_app(test_config=None):
    if test_config is None:
        load_dotenv(os.path.join(BASE_DIR, ".env"))
    app = Flask(__name__)
    production = os.environ.get("APP_ENV", "development").lower() == "production"

    _configure(app, production, test_config)
    app.teardown_appcontext(database.close_db)

    # Locally (and in tests) the tables are created for you. In production this never runs
    # on a request: run "flask --app portal init-db" once instead (see the README).
    if not production:
        database.init_db(app)

    _add_request_hooks(app)
    _add_template_helpers(app)
    _add_error_pages(app)

    from . import cli
    from .routes import admin, officer, public, resident
    for module in (public, resident, officer, admin):
        for blueprint in module.BLUEPRINTS:
            app.register_blueprint(blueprint)
    cli.register(app)
    return app


def _configure(app, production, test_config):
    app.config.from_mapping(
        DATABASE_URL=os.environ.get("DATABASE_URL"),
        # Vercel rejects request bodies over 4.5 MB, so stay just under that.
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,   # the whole request: 3 files plus the form text
        MAX_FILE_BYTES=1300 * 1024,           # one attachment (3 x 1.3 MB fits in 4 MB)
        MAX_FILES=3,
        PER_PAGE=10,
        ADMIN_EMAIL=os.environ.get("ADMIN_EMAIL"),
        ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD"),
        ADMIN_NAME=os.environ.get("ADMIN_NAME"),
        REF_PREFIX=os.environ.get("REF_PREFIX", "PSR"),
        APP_NAME=os.environ.get("APP_NAME", "Public Service Request Portal"),
        COUNTY_NAME=os.environ.get("COUNTY_NAME", "County Government"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=production or os.environ.get("SESSION_COOKIE_SECURE") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=2),
    )
    if test_config:
        app.config.update(test_config)

    if not app.config.get("SECRET_KEY"):
        key = os.environ.get("SECRET_KEY", "")
        if production and (not key or key.startswith("change-me")):
            raise RuntimeError("Set a strong SECRET_KEY environment variable when "
                               "APP_ENV=production (see the README).")
        # In development a random key is fine; you're just signed out when the server restarts.
        app.config["SECRET_KEY"] = key or os.urandom(32).hex()

    if not app.config.get("DATABASE_URL"):
        raise RuntimeError("Set the DATABASE_URL environment variable (see the README).")


def _add_request_hooks(app):
    @app.before_request
    def load_user_and_check_csrf():
        g.user = None
        if request.endpoint == "static":
            return
        user_id = session.get("user_id")
        if user_id is not None:
            g.user = database.get_db().execute(
                "SELECT * FROM users WHERE id = ? AND is_active = 1", (user_id,)).fetchone()
            if g.user is None:            # deleted or deactivated since they signed in
                session.clear()
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            security.check_csrf()

    @app.after_request
    def add_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'self'")
        if g.get("user") is not None:
            response.headers["Cache-Control"] = "no-store"
        return response


def _add_template_helpers(app):
    @app.context_processor
    def inject_globals():
        return {
            "current_user": g.get("user"),
            "csrf_token": security.csrf_token,
            "app_name": app.config["APP_NAME"],
            "county_name": app.config["COUNTY_NAME"],
            "STATUSES": workflow.STATUSES,
            "current_year": datetime.now(timezone.utc).year,
            "progress_step": workflow.progress_step,
            "PROGRESS_STEPS": workflow.PROGRESS_STEPS,
        }

    @app.template_filter("filesize")
    def file_size(value):
        """12345 -> '12 KB' (for the attachment lists)."""
        value = value or 0
        if value >= 1024 * 1024:
            return f"{value / (1024 * 1024):.1f} MB"
        return f"{max(1, round(value / 1024))} KB"

    @app.template_filter("dt")
    def local_time(value):
        """Turn a stored UTC timestamp into East Africa Time (UTC+3) for display."""
        if not value:
            return ""
        try:
            moment = datetime.strptime(value, "%Y-%m-%d %H:%M:%S") + timedelta(hours=3)
        except ValueError:
            return value
        return moment.strftime("%d %b %Y, %H:%M") + " EAT"


def _add_error_pages(app):
    def show(code, title, message, template="errors/error.html"):
        return render_template(template, code=code, title=title, message=message), code

    @app.errorhandler(404)
    def not_found(error):
        return show(404, "Page not found",
                    "The page or record you are looking for does not exist, or you do not "
                    "have access to it.", "errors/404.html")

    @app.errorhandler(403)
    def forbidden(error):
        return show(403, "Access denied", "You do not have permission to view this page.",
                    "errors/403.html")

    @app.errorhandler(413)
    def too_large(error):
        each = app.config["MAX_FILE_BYTES"] / (1024 * 1024)
        total = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
        return show(413, "Upload too large",
                    f"The files you sent are too large. Each file may be up to {each:.1f} MB "
                    f"and all files together up to {total} MB.")

    @app.errorhandler(HTTPException)
    def http_error(error):
        return show(error.code or 500, error.name, error.description)

    @app.errorhandler(Exception)
    def server_error(error):
        app.logger.exception("Unhandled error")
        return show(500, "Something went wrong",
                    "An unexpected error occurred. Please try again later.")
