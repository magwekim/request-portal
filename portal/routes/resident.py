"""Resident pages: dashboard, submit, list, detail, reply."""
from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request,
                   url_for)

from .. import queries, uploads, utils, workflow
from ..database import get_db, paginate
from ..queries import REQUEST_FROM, REQUEST_LIST_COLUMNS
from ..security import role_required
from ..utils import clean

bp = Blueprint("resident", __name__, url_prefix="/resident")
BLUEPRINTS = [bp]


def _category_choices(db):
    """Active categories, each with its department name (the form groups them by department)."""
    return db.execute(
        """SELECT c.id, c.name, c.department_id, d.name AS department
           FROM request_categories c JOIN departments d ON d.id = c.department_id
           WHERE c.is_active = 1 AND d.is_active = 1 ORDER BY d.name, c.name""").fetchall()


@bp.route("/dashboard")
@role_required("resident")
def dashboard():
    db = get_db()
    stats = queries.request_stats(db, "r.resident_id = ?", (g.user["id"],))
    recent = db.execute(
        f"SELECT {REQUEST_LIST_COLUMNS} FROM {REQUEST_FROM} WHERE r.resident_id = ? "
        "ORDER BY r.updated_at DESC, r.id DESC LIMIT 5", (g.user["id"],)).fetchall()
    return render_template("resident/dashboard.html", stats=stats, recent=recent)


@bp.route("/requests")
@role_required("resident")
def my_requests():
    db = get_db()
    where, params, filters = queries.request_filters(
        request.args, "r.resident_id = ?", (g.user["id"],))
    page = paginate(db, REQUEST_FROM, where, params, REQUEST_LIST_COLUMNS,
                    "r.created_at DESC, r.id DESC", utils.page_number(request.args),
                    current_app.config["PER_PAGE"])
    return render_template("resident/requests.html", page=page, filters=filters)


@bp.route("/requests/new", methods=["GET", "POST"])
@role_required("resident")
def submit():
    """Citizen files a request: category, location and description are required; the photo
    is optional. The department is taken from the category, so residents never pick it."""
    db = get_db()
    categories = _category_choices(db)
    if request.method == "GET":
        return render_template("resident/submit.html", form={}, errors={}, categories=categories)
    form, errors = request.form, {}
    cat_id = utils.to_int(form.get("category_id"))
    title, description, location = (clean(form.get("title")), clean(form.get("description")),
                                    clean(form.get("location")))
    category = next((c for c in categories if c["id"] == cat_id), None)
    if category is None:
        errors["category_id"] = "Select the type of problem you are reporting."
    if title and not 5 <= len(title) <= 150:
        errors["title"] = "A title, if you add one, must be 5 to 150 characters."
    if not 10 <= len(description) <= 5000:
        errors["description"] = "Describe the problem in 10 to 5000 characters."
    if not 3 <= len(location) <= 200:
        errors["location"] = "Tell us where the problem is (3 to 200 characters)."
    valid, upload_errors = uploads.validate_uploads(request.files.getlist("evidence"))
    if upload_errors:
        errors["evidence"] = " ".join(upload_errors)
    if errors:
        return render_template("resident/submit.html", form=form, errors=errors,
                               categories=categories), 400
    if not title:                                   # e.g. "Faulty street light - Kiambu Road"
        title = f"{category['name']} - {location}"[:150]
    try:
        request_id, reference = workflow.create_service_request(
            db, g.user["id"], category["department_id"], category["id"], title, description,
            location)
        uploads.store_uploads(db, valid, request_id, g.user["id"])
        db.commit()
    except Exception:
        db.rollback()   # also discards the attachments, they are part of the same transaction
        raise
    flash(f"Your request has been submitted. Reference number: {reference}. "
          "Keep it to track progress.", "success")
    return redirect(url_for("resident.request_detail", request_id=request_id))


def _own_request(db, request_id):
    req = queries.fetch_request(db, request_id)
    if req is None or req["resident_id"] != g.user["id"]:
        abort(404)  # same response for "missing" and "not yours" (no enumeration)
    return req


@bp.route("/requests/<int:request_id>")
@role_required("resident")
def request_detail(request_id):
    db = get_db()
    req = _own_request(db, request_id)
    history = queries.public_history(db, request_id)   # never includes internal notes
    attachments = db.execute(
        "SELECT id, original_filename, content_type, created_at, octet_length(content) AS size "
        "FROM attachments WHERE request_id = ? "
        "ORDER BY id", (request_id,)).fetchall()
    return render_template("resident/request_detail.html", req=req, history=history,
                           attachments=attachments, errors={}, form={})


@bp.route("/requests/<int:request_id>/reply", methods=["POST"])
@role_required("resident")
def reply(request_id):
    db = get_db()
    req = _own_request(db, request_id)
    message = clean(request.form.get("message"))
    if req["status"] != "Awaiting Information":
        flash("Additional information can only be added when the county asks for it.", "error")
        return redirect(url_for("resident.request_detail", request_id=request_id))
    valid, upload_errors = uploads.validate_uploads(request.files.getlist("evidence"))
    problem = None
    if not 5 <= len(message) <= 2000:
        problem = "Your reply must be 5 to 2000 characters."
    elif upload_errors:
        problem = " ".join(upload_errors)
    if problem:
        flash(problem, "error")
        return redirect(url_for("resident.request_detail", request_id=request_id))
    try:
        # The target status is fixed by the server; the browser never chooses it.
        workflow.change_status(db, req, "Pending", g.user, public_comment=message)
        uploads.store_uploads(db, valid, request_id, g.user["id"])
        db.commit()
    except workflow.WorkflowError as exc:
        db.rollback()
        flash(str(exc), "error")
    except Exception:
        db.rollback()
        raise
    else:
        flash("Thank you. Your information has been sent to the county officer.", "success")
    return redirect(url_for("resident.request_detail", request_id=request_id))
