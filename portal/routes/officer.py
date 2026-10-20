"""Officer pages and the request-processing actions shared by officers and admins."""
import io

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request,
                   send_file, url_for)

from .. import queries, utils, workflow
from ..database import get_db, paginate
from ..queries import REQUEST_FROM, REQUEST_LIST_COLUMNS
from ..security import login_required, role_required
from ..utils import clean
from ..workflow import TERMINAL_STATUSES, TRANSITIONS

officer_bp = Blueprint("officer", __name__, url_prefix="/officer")
staff_bp = Blueprint("staff", __name__, url_prefix="/staff")
files_bp = Blueprint("files", __name__)
BLUEPRINTS = [officer_bp, staff_bp, files_bp]


@officer_bp.route("/dashboard")
@role_required("officer")
def dashboard():
    db = get_db()
    scope, params = workflow.officer_scope(g.user)
    stats = queries.request_stats(db, scope, params)
    mine = db.execute(
        "SELECT COUNT(*) FROM service_requests r WHERE r.assigned_officer_id = ? "
        "AND r.status NOT IN ('Resolved','Rejected')", (g.user["id"],)).fetchone()[0]
    queue = db.execute(
        f"SELECT {REQUEST_LIST_COLUMNS} FROM {REQUEST_FROM} WHERE {scope} "
        "AND r.status = 'Pending' "
        "ORDER BY r.created_at ASC LIMIT 5", params).fetchall()
    dept = db.execute("SELECT name FROM departments WHERE id = ?",
                      (g.user["department_id"],)).fetchone()
    return render_template("officer/dashboard.html", stats=stats, mine=mine, queue=queue,
                           department=dept["name"] if dept else "")


@officer_bp.route("/requests")
@role_required("officer")
def requests_list():
    db = get_db()
    scope, scope_params = workflow.officer_scope(g.user)
    view = request.args.get("view", "all")
    if view == "mine":
        scope, scope_params = "r.assigned_officer_id = ?", (g.user["id"],)
    elif view == "unassigned":
        scope, scope_params = ("r.assigned_officer_id IS NULL AND r.department_id = ?",
                               (g.user["department_id"],))
    else:
        view = "all"
    where, params, filters = queries.request_filters(request.args, scope, scope_params)
    page = paginate(db, REQUEST_FROM, where, params, REQUEST_LIST_COLUMNS,
                    "r.created_at DESC, r.id DESC", utils.page_number(request.args),
                    current_app.config["PER_PAGE"])
    departments = db.execute("SELECT id, name FROM departments ORDER BY name").fetchall()
    categories = db.execute("SELECT id, name, department_id FROM request_categories "
                            "ORDER BY name").fetchall()
    return render_template("officer/requests.html", page=page, filters=filters, view=view,
                           departments=departments, categories=categories)


@officer_bp.route("/requests/<int:request_id>")
@role_required("officer")
def request_detail(request_id):
    return render_staff_detail(request_id)


def render_staff_detail(request_id):
    """Shared detail/processing page for officers and administrators."""
    db = get_db()
    req = queries.fetch_request(db, request_id)
    if req is None or not workflow.can_view_request(g.user, req):
        abort(404)
    history = db.execute(
        """SELECT h.id, h.old_status, h.new_status, h.public_comment, h.internal_note,
                  h.created_at, u.full_name AS actor_name, u.role AS actor_role
           FROM request_history h JOIN users u ON u.id = h.changed_by
           WHERE h.request_id = ? ORDER BY h.id""", (request_id,)).fetchall()
    attachments = db.execute(
        "SELECT a.id, a.original_filename, a.content_type, a.created_at, "
        "octet_length(a.content) AS size, u.full_name AS uploader "
        "FROM attachments a JOIN users u ON u.id = a.uploaded_by "
        "WHERE a.request_id = ? ORDER BY a.id", (request_id,)).fetchall()
    can_process = workflow.can_process_request(g.user, req)
    allowed = TRANSITIONS.get(req["status"], ()) if can_process else ()
    extra = {}
    if g.user["role"] == "admin":
        extra = {
            "departments": db.execute("SELECT id, name FROM departments WHERE is_active = 1 "
                                      "ORDER BY name").fetchall(),
            "categories": db.execute("SELECT id, name, department_id FROM request_categories "
                                     "WHERE is_active = 1 ORDER BY name").fetchall(),
            "officers": db.execute("SELECT id, full_name, department_id FROM users WHERE "
                                   "role = 'officer' AND is_active = 1 ORDER BY full_name"
                                   ).fetchall(),
        }
    can_claim = (g.user["role"] == "officer" and can_process and req["assigned_officer_id"] is None
                 and req["status"] not in TERMINAL_STATUSES)
    return render_template(
        "officer/request_detail.html", req=req, history=history, attachments=attachments,
        can_process=can_process, can_claim=can_claim,
        status_options=[s for s in allowed if s != "Awaiting Information"],
        can_request_info="Awaiting Information" in allowed,
        is_closed=req["status"] in TERMINAL_STATUSES,
        back_url=url_for("admin.all_requests" if g.user["role"] == "admin"
                         else "officer.requests_list"), **extra)


def _detail_url(request_id):
    endpoint = "admin.request_detail" if g.user["role"] == "admin" else "officer.request_detail"
    return url_for(endpoint, request_id=request_id)


def _load_for_processing(db, request_id):
    req = queries.fetch_request(db, request_id)
    if req is None or not workflow.can_view_request(g.user, req):
        abort(404)
    if not workflow.can_process_request(g.user, req):
        abort(403)
    return req


def _run(request_id, action, success_message):
    """Load + authorise the request, run `action(db, req)`, commit, flash, redirect."""
    db = get_db()
    req = _load_for_processing(db, request_id)
    try:
        action(db, req)
        db.commit()
        flash(success_message, "success")
    except workflow.WorkflowError as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(_detail_url(request_id))


@staff_bp.route("/requests/<int:request_id>/status", methods=["POST"])
@role_required("officer", "admin")
def update_status(request_id):
    f = request.form
    return _run(request_id, lambda db, req: workflow.change_status(
        db, req, clean(f.get("new_status")), g.user,
        public_comment=clean(f.get("public_comment")) or None,
        internal_note=clean(f.get("internal_note")) or None,
        resolution_details=clean(f.get("resolution_details")) or None),
        "Status updated.")


@staff_bp.route("/requests/<int:request_id>/request-info", methods=["POST"])
@role_required("officer", "admin")
def request_info(request_id):
    return _run(request_id, lambda db, req: workflow.change_status(
        db, req, "Awaiting Information", g.user,
        public_comment=clean(request.form.get("public_comment")) or None),
        "The resident has been asked for additional information.")


def _note_only(db, req, public_comment, internal_note):
    workflow.add_history(db, req["id"], g.user["id"], req["status"], req["status"],
                        public_comment, internal_note)
    workflow.touch_request(db, req["id"])
    workflow.audit(db, g.user["id"], "public_response" if public_comment else "internal_note",
                  "service_request", req["id"])


@staff_bp.route("/requests/<int:request_id>/respond", methods=["POST"])
@role_required("officer", "admin")
def respond(request_id):
    message = clean(request.form.get("public_comment"))

    def action(db, req):
        if not 2 <= len(message) <= 2000:
            raise workflow.WorkflowError("A public response must be 2 to 2000 characters.")
        _note_only(db, req, message, None)
    return _run(request_id, action, "Your response is now visible to the resident.")


@staff_bp.route("/requests/<int:request_id>/note", methods=["POST"])
@role_required("officer", "admin")
def add_note(request_id):
    note = clean(request.form.get("internal_note"))

    def action(db, req):
        if not 2 <= len(note) <= 2000:
            raise workflow.WorkflowError("An internal note must be 2 to 2000 characters.")
        _note_only(db, req, None, note)
    return _run(request_id, action, "Internal note saved (not visible to the resident).")


@staff_bp.route("/requests/<int:request_id>/claim", methods=["POST"])
@role_required("officer")
def claim(request_id):
    def action(db, req):
        if req["assigned_officer_id"] is not None or req["status"] in TERMINAL_STATUSES:
            raise workflow.WorkflowError("This request can no longer be claimed.")
        cur = db.execute("UPDATE service_requests SET assigned_officer_id = ? "
                         "WHERE id = ? AND assigned_officer_id IS NULL", (g.user["id"], req["id"]))
        if cur.rowcount != 1:
            raise workflow.WorkflowError("Another officer already took this request.")
        note = f"Claimed by {g.user['full_name']}"
        workflow.audit(db, g.user["id"], "request_claimed", "service_request", req["id"])
        _note_only(db, req, None, note)
    return _run(request_id, action, "The request is now assigned to you.")


def _visible_attachment(attachment_id):
    """Load one attachment, or 404 if it is missing or the user may not see its request."""
    db = get_db()
    row = db.execute(
        "SELECT original_filename, content_type, content, request_id FROM attachments "
        "WHERE id = ?", (attachment_id,)).fetchone()
    req = queries.fetch_request(db, row["request_id"]) if row else None
    # Same answer for "does not exist" and "not yours" so ids cannot be guessed.
    if row is None or req is None or not workflow.can_view_request(g.user, req):
        abort(404)
    return row


@files_bp.route("/attachments/<int:attachment_id>")
@login_required
def download_attachment(attachment_id):
    row = _visible_attachment(attachment_id)
    # The file bytes come out of the database and are sent as a download.
    return send_file(io.BytesIO(row["content"]), mimetype=row["content_type"],
                     as_attachment=True, download_name=row["original_filename"])


@files_bp.route("/attachments/<int:attachment_id>/view")
@login_required
def view_attachment(attachment_id):
    """Show a photo inside the page (thumbnails and the enlarged view). PDFs are never shown
    inline: only the two image types that upload checking has already verified."""
    row = _visible_attachment(attachment_id)
    if row["content_type"] not in ("image/png", "image/jpeg"):
        abort(404)
    return send_file(io.BytesIO(row["content"]), mimetype=row["content_type"],
                     as_attachment=False, download_name=row["original_filename"])
