"""The request workflow: roles, statuses, allowed transitions, who may see or process a
request, and the functions that move a request along while recording history and audit logs."""
import uuid

from flask import current_app

from .database import utcnow


ROLES = ("resident", "officer", "admin")


# The citizen-facing flow is  Pending -> In Progress -> Resolved.
# "Awaiting Information" (waiting for the resident) and "Rejected" (closed without a fix)
# are side branches an officer can use when needed.
STATUSES = ["Pending", "In Progress", "Awaiting Information", "Resolved", "Rejected"]


TERMINAL_STATUSES = ("Resolved", "Rejected")


# Server-side workflow: current status -> statuses it may move to.
TRANSITIONS = {
    "Pending": ("In Progress", "Awaiting Information", "Rejected"),
    "In Progress": ("Resolved", "Awaiting Information", "Rejected"),
    "Awaiting Information": ("Pending", "In Progress", "Rejected"),
    "Resolved": (),
    "Rejected": (),
}


class WorkflowError(Exception):
    """Raised when a status change or assignment is not allowed."""


def audit(db, user_id, action, entity_type, entity_id=None, details=None):
    db.execute(
        "INSERT INTO audit_logs (user_id, action, entity_type, entity_id, details, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, action, entity_type, entity_id, details, utcnow()))


def add_history(db, request_id, user_id, old_status, new_status,
                public_comment=None, internal_note=None):
    db.execute(
        "INSERT INTO request_history (request_id, changed_by, old_status, new_status, "
        "public_comment, internal_note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (request_id, user_id, old_status, new_status, public_comment or None,
         internal_note or None, utcnow()))


def can_view_request(user, req):
    if user["role"] == "admin":
        return True
    if user["role"] == "resident":
        return req["resident_id"] == user["id"]
    if user["role"] == "officer":
        return (req["assigned_officer_id"] == user["id"]
                or (user["department_id"] is not None
                    and req["department_id"] == user["department_id"]))
    return False


def can_process_request(user, req):
    """Admins may process anything; officers only requests assigned to them or
    unassigned requests in their own department."""
    if user["role"] == "admin":
        return True
    if user["role"] == "officer":
        if req["assigned_officer_id"] == user["id"]:
            return True
        return (req["assigned_officer_id"] is None and user["department_id"] is not None
                and req["department_id"] == user["department_id"])
    return False


def officer_scope(user):
    return ("(r.assigned_officer_id = ? OR r.department_id = ?)",
            (user["id"], user["department_id"]))


PROGRESS_STEPS = ["Pending", "In Progress", "Resolved"]


def progress_step(status):
    """Position of a status on the Pending -> In Progress -> Resolved tracker (0, 1 or 2).
    "Awaiting Information" counts as the step the request was on (Pending); Rejected is a
    closed, non-resolved outcome and returns None."""
    if status == "Rejected":
        return None
    return {"In Progress": 1, "Resolved": 2}.get(status, 0)


def create_service_request(db, resident_id, department_id, category_id,
                           title, description, location):
    """Insert a request and generate its unique reference, e.g. PSR-2026-000001."""
    now = utcnow()
    cur = db.execute(
        "INSERT INTO service_requests (reference_number, resident_id, department_id, category_id, "
        "title, description, location, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'Pending', ?, ?)",
        (f"TMP-{uuid.uuid4().hex}", resident_id, department_id, category_id,
         title, description, location or None, now, now))
    request_id = cur.lastrowid
    reference = f"{current_app.config['REF_PREFIX']}-{now[:4]}-{request_id:06d}"
    db.execute("UPDATE service_requests SET reference_number = ? WHERE id = ?",
               (reference, request_id))
    add_history(db, request_id, resident_id, None, "Pending")
    audit(db, resident_id, "request_submitted", "service_request", request_id, reference)
    return request_id, reference


def change_status(db, req, new_status, user, public_comment=None,
                  internal_note=None, resolution_details=None):
    """Validate and apply a status transition, recording history and an audit entry."""
    old_status = req["status"]
    if new_status not in STATUSES:
        raise WorkflowError("Unknown status.")
    if new_status not in TRANSITIONS.get(old_status, ()):
        raise WorkflowError(f"A request cannot move from '{old_status}' to '{new_status}'.")
    if new_status == "Resolved" and not resolution_details:
        raise WorkflowError("Resolution details are required to resolve a request.")
    if new_status == "Rejected" and not public_comment:
        raise WorkflowError("A public explanation is required to reject a request.")
    if new_status == "Awaiting Information" and not public_comment:
        raise WorkflowError("Tell the resident what additional information is needed.")
    now = utcnow()
    cur = db.execute(
        "UPDATE service_requests SET status = ?, updated_at = ?, "
        "resolution_details = CASE WHEN ? = 'Resolved' THEN ? ELSE resolution_details END, "
        "resolved_at = CASE WHEN ? = 'Resolved' THEN ? ELSE resolved_at END "
        "WHERE id = ? AND status = ?",
        (new_status, now, new_status, resolution_details, new_status, now, req["id"], old_status))
    if cur.rowcount != 1:
        raise WorkflowError("This request was changed by someone else. Reload and try again.")
    add_history(db, req["id"], user["id"], old_status, new_status, public_comment, internal_note)
    audit(db, user["id"], "status_changed", "service_request", req["id"],
          f"{old_status} -> {new_status}")


def touch_request(db, request_id):
    db.execute("UPDATE service_requests SET updated_at = ? WHERE id = ?", (utcnow(), request_id))
