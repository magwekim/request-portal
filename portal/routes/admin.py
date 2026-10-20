"""Administrator pages: dashboard, requests, assignment, users, departments, categories, audit."""

import csv
import io

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request,
                   url_for)

from .. import accounts, database, queries, utils, workflow
from ..database import IntegrityError, get_db, paginate
from ..queries import REQUEST_FROM, REQUEST_LIST_COLUMNS
from ..security import role_required
from ..utils import clean, to_int
from ..workflow import TERMINAL_STATUSES
from .officer import render_staff_detail

bp = Blueprint("admin", __name__, url_prefix="/admin")
BLUEPRINTS = [bp]


def _page(db, from_sql, where, params, columns, order):
    return paginate(db, from_sql, where, params, columns, order,
                    utils.page_number(request.args), current_app.config["PER_PAGE"])


@bp.route("/dashboard")
@role_required("admin")
def dashboard():
    db = get_db()
    stats = queries.request_stats(db)
    unassigned = db.execute(
        "SELECT COUNT(*) FROM service_requests WHERE assigned_officer_id IS NULL "
        "AND status NOT IN ('Resolved','Rejected')").fetchone()[0]
    recent = db.execute(f"SELECT {REQUEST_LIST_COLUMNS} FROM {REQUEST_FROM} "
                        "ORDER BY r.created_at DESC, r.id DESC LIMIT 5").fetchall()
    return render_template("admin/dashboard.html", stats=stats, unassigned=unassigned,
                           by_department=queries.stats_by_department(db),
                           by_category=queries.stats_by_category(db),
                           avg_hours=queries.average_resolution_hours(db), recent=recent)


@bp.route("/requests")
@role_required("admin")
def all_requests():
    db = get_db()
    where, params, filters = queries.request_filters(request.args)
    page = _page(db, REQUEST_FROM, where, params, REQUEST_LIST_COLUMNS,
                 "r.created_at DESC, r.id DESC")
    return render_template(
        "admin/requests.html", page=page, filters=filters,
        departments=db.execute("SELECT id, name FROM departments ORDER BY name").fetchall(),
        categories=db.execute("SELECT id, name, department_id FROM request_categories "
                              "ORDER BY name").fetchall())


def _csv_safe(value):
    """Stop spreadsheet programs from running a cell that starts with = + - or @ as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


@bp.route("/requests/export.csv")
@role_required("admin")
def export_requests():
    """Download the requests that match the current filters as a CSV file."""
    db = get_db()
    where, params, _ = queries.request_filters(request.args)
    rows = db.execute(
        f"SELECT r.reference_number, r.title, c.name AS category, d.name AS department, "
        f"r.location, r.status, u.full_name AS resident, o.full_name AS officer, "
        f"r.created_at, r.resolved_at FROM {REQUEST_FROM} WHERE {where} "
        f"ORDER BY r.created_at DESC, r.id DESC", params).fetchall()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Reference", "Title", "Category", "Department", "Location", "Status",
                     "Resident", "Assigned officer", "Submitted (UTC)", "Resolved (UTC)"])
    for r in rows:
        writer.writerow([_csv_safe(r[key]) for key in (
            "reference_number", "title", "category", "department", "location", "status",
            "resident", "officer", "created_at", "resolved_at")])
    return Response(out.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=service-requests.csv"})


@bp.route("/requests/<int:request_id>")
@role_required("admin")
def request_detail(request_id):
    return render_staff_detail(request_id)


@bp.route("/assignments")
@role_required("admin")
def assignments():
    db = get_db()
    show = request.args.get("show", "unassigned")
    where = "r.status NOT IN ('Resolved','Rejected')"
    if show != "all":
        show = "unassigned"
        where += " AND r.assigned_officer_id IS NULL"
    page = _page(db, REQUEST_FROM, where, [], REQUEST_LIST_COLUMNS, "r.created_at ASC, r.id ASC")
    officers = db.execute("SELECT id, full_name, department_id FROM users WHERE role = 'officer' "
                          "AND is_active = 1 ORDER BY full_name").fetchall()
    return render_template("admin/assignments.html", page=page, show=show, officers=officers)


@bp.route("/requests/<int:request_id>/assign", methods=["POST"])
@role_required("admin")
def assign(request_id):
    db = get_db()
    req = queries.fetch_request(db, request_id)
    if req is None:
        abort(404)
    if request.form.get("back") == "list":
        target = url_for("admin.assignments")
    else:
        target = url_for("admin.request_detail", request_id=request_id)

    if req["status"] in TERMINAL_STATUSES:
        flash("Closed requests cannot be reassigned.", "error")
        return redirect(target)

    # Never trust ids from the browser: check every one of them again here.
    dept_id = to_int(request.form.get("department_id"))
    cat_id = to_int(request.form.get("category_id"))
    officer_id = to_int(request.form.get("officer_id"))

    dept = db.execute("SELECT id, name FROM departments WHERE id = ? AND is_active = 1",
                      (dept_id,)).fetchone()
    # A request may keep its current category even if that category was switched off later.
    keeps_routing = (dept_id, cat_id) == (req["department_id"], req["category_id"])
    cat = db.execute("SELECT id FROM request_categories WHERE id = ? AND department_id = ?"
                     + ("" if keeps_routing else " AND is_active = 1"),
                     (cat_id, dept_id)).fetchone()
    if dept is None or cat is None:
        flash("Choose an active department and a category that belongs to it.", "error")
        return redirect(target)

    officer = None
    if officer_id is not None:
        officer = db.execute("SELECT id, full_name FROM users WHERE id = ? AND role = 'officer' "
                             "AND is_active = 1 AND department_id = ?",
                             (officer_id, dept_id)).fetchone()
        if officer is None:
            flash("Choose an active officer who works in the selected department.", "error")
            return redirect(target)

    new_officer_id = officer["id"] if officer else None
    if (dept_id, cat_id, new_officer_id) == (req["department_id"], req["category_id"],
                                             req["assigned_officer_id"]):
        flash("No changes were made.", "info")
        return redirect(target)

    note = f"Routed to {dept['name']}; assigned to {officer['full_name'] if officer else 'no officer'}"
    try:
        db.execute("UPDATE service_requests SET department_id = ?, category_id = ?, "
                   "assigned_officer_id = ? WHERE id = ?",
                   (dept_id, cat_id, new_officer_id, request_id))
        workflow.audit(db, g.user["id"], "request_assigned", "service_request", request_id, note)
        workflow.add_history(db, request_id, g.user["id"], req["status"], req["status"],
                             None, note)
        workflow.touch_request(db, request_id)
        db.commit()
        flash("Assignment saved.", "success")
    except workflow.WorkflowError as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(target)


@bp.route("/users")
@role_required("admin")
def users():
    return _render_users(get_db(), {}, {})


def _render_users(db, form, errors, status=200):
    role = request.args.get("role", "")
    q = clean(request.args.get("q"))
    where, params = ["1=1"], []
    if role in workflow.ROLES:
        where.append("u.role = ?")
        params.append(role)
    else:
        role = ""
    if q:
        where.append("(u.full_name ILIKE ? ESCAPE '\\' OR u.email ILIKE ? ESCAPE '\\')")
        params += [utils.like_pattern(q)] * 2
    page = _page(db, "users u LEFT JOIN departments d ON d.id = u.department_id",
                 " AND ".join(where), params,
                 "u.id, u.full_name, u.email, u.phone_number, u.role, u.is_active, u.created_at, "
                 "u.department_id, d.name AS department", "u.created_at DESC, u.id DESC")
    departments = db.execute("SELECT id, name FROM departments WHERE is_active = 1 "
                             "ORDER BY name").fetchall()
    return render_template("admin/users.html", page=page, departments=departments, role=role,
                           q=q, form=form, errors=errors), status


@bp.route("/users/create-officer", methods=["POST"])
@role_required("admin")
def create_officer():
    """Create a staff account: a department officer, or another application-wide administrator."""
    db = get_db()
    role = request.form.get("role", "officer")
    if role not in ("officer", "admin"):
        abort(400)
    values, errors = accounts.validate_new_user(request.form, db)
    dept_id = None
    if role == "officer":
        dept_id = to_int(request.form.get("department_id"))
        if db.execute("SELECT 1 FROM departments WHERE id = ? AND is_active = 1",
                      (dept_id,)).fetchone() is None:
            errors["department_id"] = "Select a department for the officer."
    if errors:
        return _render_users(db, request.form, errors, 400)
    try:
        if role == "officer":
            database.create_department_admin(db, g.user["id"], values["full_name"],
                                             values["email"], values["password"], dept_id,
                                             values["phone_number"])
        else:
            user_id = accounts.create_user(db, values, role, dept_id)
            workflow.audit(db, g.user["id"], f"{role}_created", "user", user_id, values["email"])
        db.commit()
    except IntegrityError:
        db.rollback()
        return _render_users(db, request.form, {"email": "That email is already in use."}, 400)
    label = "Administrator" if role == "admin" else "Officer"
    flash(f"{label} account created for {values['full_name']}.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>/toggle", methods=["POST"])
@role_required("admin")
def toggle_user(user_id):
    if user_id == g.user["id"]:
        flash("You cannot deactivate your own account.", "error")
        return redirect(url_for("admin.users"))
    db = get_db()
    user, now_active = _flip_active(db, "users", user_id, "full_name", "user", "user")
    db.commit()
    flash(f"{user['label']} has been {'activated' if now_active else 'deactivated'}.", "success")
    came_from_here = request.referrer and request.referrer.startswith(request.host_url)
    return redirect(request.referrer if came_from_here else url_for("admin.users"))


@bp.route("/users/<int:user_id>/department", methods=["POST"])
@role_required("admin")
def set_officer_department(user_id):
    db = get_db()
    user = db.execute("SELECT id, full_name, role FROM users WHERE id = ?", (user_id,)).fetchone()
    dept_id = to_int(request.form.get("department_id"))
    dept = db.execute("SELECT id, name FROM departments WHERE id = ? AND is_active = 1",
                      (dept_id,)).fetchone()
    if user is None:
        abort(404)
    if user["role"] != "officer" or dept is None:
        flash("Only officers can be assigned to an active department.", "error")
    else:
        db.execute("UPDATE users SET department_id = ? WHERE id = ?", (dept_id, user_id))
        workflow.audit(db, g.user["id"], "officer_department_changed", "user", user_id, dept["name"])
        db.commit()
        flash(f"{user['full_name']} now works in {dept['name']}.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/departments")
@role_required("admin")
def departments():
    db = get_db()
    rows = db.execute(
        """SELECT d.*, (SELECT COUNT(*) FROM request_categories c WHERE c.department_id = d.id) AS
                  category_count,
                  (SELECT COUNT(*) FROM users u WHERE u.department_id = d.id AND u.role = 'officer')
                  AS officer_count
           FROM departments d ORDER BY d.name""").fetchall()
    return render_template("admin/departments.html", departments=rows, form={}, errors={})


def _name_and_description(form):
    """Validate the name/description pair shared by departments and categories."""
    name, description = clean(form.get("name")), clean(form.get("description"))
    errors = {}
    if not 3 <= len(name) <= 100:
        errors["name"] = "Name must be 3 to 100 characters."
    if len(description) > 300:
        errors["description"] = "Description must be at most 300 characters."
    return name, description, errors


def _flip_active(db, table, row_id, label_column, audit_prefix, entity_type):
    """Switch a row's is_active flag, write the audit entry, and return (row, new_value)."""
    row = db.execute(f"SELECT {label_column} AS label, is_active FROM {table} WHERE id = ?",
                     (row_id,)).fetchone()
    if row is None:
        abort(404)
    new_value = 0 if row["is_active"] else 1
    db.execute(f"UPDATE {table} SET is_active = ? WHERE id = ?", (new_value, row_id))
    workflow.audit(db, g.user["id"],
                   f"{audit_prefix}_{'activated' if new_value else 'deactivated'}",
                   entity_type, row_id, row["label"])
    return row, new_value


@bp.route("/departments/create", methods=["POST"])
@role_required("admin")
def create_department():
    db = get_db()
    name, description, errors = _name_and_description(request.form)
    if not errors:
        try:
            cur = db.execute("INSERT INTO departments (name, description) VALUES (?, ?)",
                             (name, description))
            workflow.audit(db, g.user["id"], "department_created", "department", cur.lastrowid, name)
            db.commit()
            flash("Department created.", "success")
            return redirect(url_for("admin.departments"))
        except IntegrityError:
            db.rollback()
            errors["name"] = "A department with this name already exists."
    rows = db.execute("SELECT d.*, 0 AS category_count, 0 AS officer_count FROM departments d "
                      "ORDER BY d.name").fetchall()
    return render_template("admin/departments.html", departments=rows, form=request.form,
                           errors=errors), 400


@bp.route("/departments/<int:dept_id>/update", methods=["POST"])
@role_required("admin")
def update_department(dept_id):
    db = get_db()
    if db.execute("SELECT 1 FROM departments WHERE id = ?", (dept_id,)).fetchone() is None:
        abort(404)
    name, description, errors = _name_and_description(request.form)
    if errors:
        flash(" ".join(errors.values()), "error")
        return redirect(url_for("admin.departments"))
    try:
        db.execute("UPDATE departments SET name = ?, description = ? WHERE id = ?",
                   (name, description, dept_id))
        workflow.audit(db, g.user["id"], "department_updated", "department", dept_id, name)
        db.commit()
        flash("Department updated.", "success")
    except IntegrityError:
        db.rollback()
        flash("A department with this name already exists.", "error")
    return redirect(url_for("admin.departments"))


@bp.route("/departments/<int:dept_id>/toggle", methods=["POST"])
@role_required("admin")
def toggle_department(dept_id):
    db = get_db()
    _, now_active = _flip_active(db, "departments", dept_id, "name", "department", "department")
    db.commit()
    flash(f"Department {'activated' if now_active else 'deactivated'}.", "success")
    return redirect(url_for("admin.departments"))


def _render_categories(db, form, errors, status=200):
    rows = db.execute(
        """SELECT c.*, d.name AS department,
                  (SELECT COUNT(*) FROM service_requests r WHERE r.category_id = c.id) AS used
           FROM request_categories c JOIN departments d ON d.id = c.department_id
           ORDER BY d.name, c.name""").fetchall()
    departments = db.execute("SELECT id, name FROM departments WHERE is_active = 1 "
                             "ORDER BY name").fetchall()
    return render_template("admin/categories.html", categories=rows, departments=departments,
                           form=form, errors=errors), status


@bp.route("/categories")
@role_required("admin")
def categories():
    return _render_categories(get_db(), {}, {})


@bp.route("/categories/create", methods=["POST"])
@role_required("admin")
def create_category():
    db = get_db()
    dept_id = to_int(request.form.get("department_id"))
    name, description, errors = _name_and_description(request.form)
    if db.execute("SELECT 1 FROM departments WHERE id = ? AND is_active = 1",
                  (dept_id,)).fetchone() is None:
        errors["department_id"] = "Select a department."
    if not errors:
        try:
            cur = db.execute("INSERT INTO request_categories (department_id, name, description) "
                             "VALUES (?, ?, ?)", (dept_id, name, description))
            workflow.audit(db, g.user["id"], "category_created", "request_category",
                          cur.lastrowid, name)
            db.commit()
            flash("Category created.", "success")
            return redirect(url_for("admin.categories"))
        except IntegrityError:
            db.rollback()
            errors["name"] = "This department already has a category with that name."
    return _render_categories(db, request.form, errors, 400)


@bp.route("/categories/<int:category_id>/update", methods=["POST"])
@role_required("admin")
def update_category(category_id):
    db = get_db()
    row = db.execute("SELECT id FROM request_categories WHERE id = ?", (category_id,)).fetchone()
    if row is None:
        abort(404)
    name, description, errors = _name_and_description(request.form)
    if errors:
        flash(" ".join(errors.values()), "error")
        return redirect(url_for("admin.categories"))
    try:
        db.execute("UPDATE request_categories SET name = ?, description = ? WHERE id = ?",
                   (name, description, category_id))
        workflow.audit(db, g.user["id"], "category_updated", "request_category", category_id, name)
        db.commit()
        flash("Category updated.", "success")
    except IntegrityError:
        db.rollback()
        flash("This department already has a category with that name.", "error")
    return redirect(url_for("admin.categories"))


@bp.route("/categories/<int:category_id>/toggle", methods=["POST"])
@role_required("admin")
def toggle_category(category_id):
    db = get_db()
    _, now_active = _flip_active(db, "request_categories", category_id, "name",
                                 "category", "request_category")
    db.commit()
    flash(f"Category {'activated' if now_active else 'deactivated'}.", "success")
    return redirect(url_for("admin.categories"))


@bp.route("/audit")
@role_required("admin")
def audit_log():
    db = get_db()
    action = clean(request.args.get("action"))
    actions = [r["action"] for r in db.execute(
        "SELECT DISTINCT action FROM audit_logs ORDER BY action")]
    where, params = "1=1", []
    if action in actions:
        where, params = "a.action = ?", [action]
    else:
        action = ""
    page = _page(db, "audit_logs a LEFT JOIN users u ON u.id = a.user_id", where, params,
                 "a.id, a.action, a.entity_type, a.entity_id, a.details, a.created_at, "
                 "u.full_name AS user_name, u.role AS user_role", "a.id DESC")
    return render_template("admin/audit.html", page=page, actions=actions, action=action)
