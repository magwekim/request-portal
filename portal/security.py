"""Security building blocks: password checks, CSRF protection, rate limiting and the
login / role decorators that guard every protected page."""
import hmac
import os
import random
import re
import secrets
import time
from functools import wraps

from flask import abort, flash, g, redirect, request, session, url_for
from werkzeug.security import check_password_hash

from .database import get_db, hash_password


DUMMY_HASH = hash_password(secrets.token_hex(8))  # equalises timing for unknown emails


def verify_password(password_hash, password):
    try:
        return check_password_hash(password_hash, password)
    except ValueError:
        return False


def password_error(password):
    if len(password) < 8:
        return "Password must be at least 8 characters long."
    if len(password) > 128:
        return "Password must be at most 128 characters long."
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "Password must contain at least one letter and one number."
    return None


def safe_next(target):
    """Only allow local, relative redirect targets (prevents open redirects)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def check_csrf():
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    expected = session.get("_csrf", "")
    if not expected or not hmac.compare_digest(sent, expected):
        abort(400, description="Your session expired or the form was invalid. "
                               "Please go back, reload the page and try again.")


# Rate limiting
# Attempts are saved in the "rate_limits" table (not in memory) because Vercel can run each
# request on a different server, and memory is not shared between them.

def is_rate_limited(key, limit, window):
    """True if `key` already made `limit` or more attempts in the last `window` seconds."""
    count = get_db().execute(
        "SELECT COUNT(*) AS n FROM rate_limits WHERE key = ? AND attempted_at >= ?",
        (key, time.time() - window)).fetchone()["n"]
    return count >= limit


def record_attempt(*keys):
    """Remember one more attempt for each key (one query, saved straight away)."""
    db = get_db()
    now = time.time()
    db.execute("INSERT INTO rate_limits (key, attempted_at) VALUES " + ", ".join(["(?, ?)"] * len(keys)),
               tuple(v for k in keys for v in (k, now)))
    if random.random() < 0.02:            # now and then, forget attempts older than a day
        db.execute("DELETE FROM rate_limits WHERE attempted_at < ?", (now - 86400,))
    db.commit()


def clear_attempts(key):
    """Forget all attempts for `key` (e.g. after a successful login)."""
    db = get_db()
    db.execute("DELETE FROM rate_limits WHERE key = ?", (key,))
    db.commit()


def client_ip():
    """The visitor's IP address, used as part of the rate-limit key."""
    # On Vercel every request arrives through Vercel's proxy, so request.remote_addr would be
    # the proxy. Vercel puts the real visitor address first in X-Forwarded-For (and overwrites
    # anything the visitor sent). We only trust that header when running on Vercel.
    if os.environ.get("VERCEL"):
        forwarded = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return request.remote_addr or "unknown"


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            flash("Please sign in to continue.", "info")
            return redirect(url_for("public.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if g.user["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator
