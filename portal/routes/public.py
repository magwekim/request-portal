"""Public pages, authentication and profile."""
from flask import (Blueprint, flash, g, redirect, render_template, request, session, url_for)

from .. import accounts, database, queries, security, workflow
from ..database import IntegrityError, get_db
from ..security import login_required
from ..utils import clean

bp = Blueprint("public", __name__)
BLUEPRINTS = [bp]


@bp.route("/")
def home():
    return render_template("home.html")


@bp.route("/dashboard")
@login_required
def dashboard():
    return redirect(url_for(f"{g.user['role']}.dashboard"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("public.dashboard"))
    next_url = security.safe_next(request.values.get("next"))
    if request.method == "GET":
        return render_template("auth/login.html", form={}, errors={}, next_url=next_url)

    email = clean(request.form.get("email")).lower()
    password = request.form.get("password", "")
    ip = security.client_ip()
    key_user, key_ip = f"login:{ip}:{email}", f"login-ip:{ip}"
    if security.is_rate_limited(key_user, 5, 300) or security.is_rate_limited(key_ip, 30, 300):
        flash("Too many failed sign-in attempts. Please wait a few minutes and try again.", "error")
        return render_template("auth/login.html", form={"email": email}, errors={},
                               next_url=next_url), 429
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    valid = security.verify_password(user["password_hash"] if user else security.DUMMY_HASH, password)
    if user and valid:
        if not user["is_active"]:
            flash("This account has been deactivated. Please contact an administrator.", "error")
            return render_template("auth/login.html", form={"email": email}, errors={},
                                   next_url=next_url), 403
        security.clear_attempts(key_user)
        session.clear()                      # new session id contents (prevents fixation)
        session["user_id"] = user["id"]
        session.permanent = True
        flash(f"Welcome back, {user['full_name']}.", "success")
        return redirect(next_url or url_for("public.dashboard"))
    security.record_attempt(key_user, key_ip)
    flash("Invalid email or password.", "error")
    return render_template("auth/login.html", form={"email": email}, errors={},
                           next_url=next_url), 401


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("public.home"))


@bp.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("public.dashboard"))
    if request.method == "GET":
        return render_template("auth/register.html", form={}, errors={})
    key = f"register:{security.client_ip()}"
    if security.is_rate_limited(key, 10, 3600):
        flash("Too many registrations from this address. Please try again later.", "error")
        return render_template("auth/register.html", form={}, errors={}), 429
    security.record_attempt(key)
    db = get_db()
    values, errors = accounts.validate_new_user(request.form, db)
    if not errors:
        try:
            # Role is fixed here: public registration can only ever create residents.
            user_id = accounts.create_user(db, values, "resident")
            workflow.audit(db, user_id, "user_registered", "user", user_id)
            db.commit()
        except IntegrityError:
            db.rollback()
            errors["email"] = "An account with this email already exists."
    if errors:
        return render_template("auth/register.html", form=request.form, errors=errors), 400
    flash("Your account has been created. You can now sign in.", "success")
    return redirect(url_for("public.login"))


@bp.route("/track", methods=["GET", "POST"])
def track():
    if request.method == "GET":
        return render_template("track.html", form={}, result=None)
    key = f"track:{security.client_ip()}"
    if security.is_rate_limited(key, 10, 300):
        flash("Too many lookups. Please wait a few minutes and try again.", "error")
        return render_template("track.html", form={}, result=None), 429
    security.record_attempt(key)
    reference = clean(request.form.get("reference")).upper()
    email = clean(request.form.get("email")).lower()
    db = get_db()
    # Reference number alone is never enough: the resident's email must also match.
    req = db.execute(
        """SELECT r.id, r.reference_number, r.status, r.created_at, r.updated_at,
                  d.name AS department, c.name AS category
           FROM service_requests r
           JOIN users u ON u.id = r.resident_id
           JOIN departments d ON d.id = r.department_id
           JOIN request_categories c ON c.id = r.category_id
           WHERE r.reference_number = ? AND u.email = ?""", (reference, email)).fetchone()
    if req is None:
        flash("No matching request found. Check the reference number and email address.", "error")
        return render_template("track.html", form={"reference": reference, "email": email},
                               result=None), 404
    history = queries.public_history(db, req["id"])
    return render_template("track.html", form={"reference": reference, "email": email},
                           result={"request": req, "history": history})


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    errors = {}
    if request.method == "POST":
        name, phone = clean(request.form.get("full_name")), clean(request.form.get("phone_number"))
        if not 2 <= len(name) <= 100:
            errors["full_name"] = "Enter your full name (2 to 100 characters)."
        if phone and not accounts.PHONE_RE.match(phone):
            errors["phone_number"] = "Enter a valid phone number, e.g. +254 712 345 678."
        if not errors:
            db = get_db()
            db.execute("UPDATE users SET full_name = ?, phone_number = ? WHERE id = ?",
                       (name, phone or None, g.user["id"]))
            workflow.audit(db, g.user["id"], "profile_updated", "user", g.user["id"])
            db.commit()
            flash("Profile updated.", "success")
            return redirect(url_for("public.profile"))
        return render_template("auth/profile.html", form=request.form, errors=errors), 400
    return render_template("auth/profile.html", form={}, errors={})


@bp.route("/profile/password", methods=["POST"])
@login_required
def change_password():
    key = f"pwchange:{g.user['id']}"
    if security.is_rate_limited(key, 5, 600):
        flash("Too many attempts. Please wait before trying again.", "error")
        return redirect(url_for("public.profile"))
    current, new, confirm = (request.form.get("current_password", ""),
                             request.form.get("new_password", ""),
                             request.form.get("confirm_password", ""))
    problem = security.password_error(new)
    if not security.verify_password(g.user["password_hash"], current):
        security.record_attempt(key)
        flash("Your current password is incorrect.", "error")
    elif problem:
        flash(problem, "error")
    elif new != confirm:
        flash("The new passwords do not match.", "error")
    else:
        db = get_db()
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                   (database.hash_password(new), g.user["id"]))
        workflow.audit(db, g.user["id"], "password_changed", "user", g.user["id"])
        db.commit()
        flash("Password changed.", "success")
    return redirect(url_for("public.profile"))
