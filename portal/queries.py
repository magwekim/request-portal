"""Reusable SQL for listing, filtering and counting service requests."""
from .utils import clean, like_pattern, parse_date, to_int
from .workflow import STATUSES


REQUEST_FROM = """service_requests r
    JOIN departments d ON d.id = r.department_id
    JOIN request_categories c ON c.id = r.category_id
    JOIN users u ON u.id = r.resident_id
    LEFT JOIN users o ON o.id = r.assigned_officer_id"""


REQUEST_LIST_COLUMNS = """r.id, r.reference_number, r.title, r.status, r.created_at, r.updated_at,
    r.assigned_officer_id, r.department_id, r.category_id, d.name AS department, c.name AS category,
    u.full_name AS resident_name, o.full_name AS officer_name"""


def fetch_request(db, request_id):
    return db.execute(
        f"""SELECT r.*, d.name AS department, c.name AS category,
                   u.full_name AS resident_name, u.email AS resident_email,
                   u.phone_number AS resident_phone, o.full_name AS officer_name
            FROM {REQUEST_FROM} WHERE r.id = ?""", (request_id,)).fetchone()


def public_history(db, request_id):
    """History a resident may see: status changes and public comments, never internal notes."""
    return db.execute(
        """SELECT h.id, h.old_status, h.new_status, h.public_comment, h.created_at,
                  u.role AS actor_role
           FROM request_history h JOIN users u ON u.id = h.changed_by
           WHERE h.request_id = ? AND (h.old_status IS NULL OR h.old_status <> h.new_status
                 OR h.public_comment IS NOT NULL) ORDER BY h.id""", (request_id,)).fetchall()


def request_filters(args, scope_where="1=1", scope_params=()):
    """Build a WHERE clause from whitelisted query-string filters."""
    where, params = [scope_where], list(scope_params)
    filters = {k: clean(args.get(k)) for k in
               ("q", "status", "department", "category", "date_from", "date_to")}
    if filters["status"] in STATUSES:
        where.append("r.status = ?")
        params.append(filters["status"])
    else:
        filters["status"] = ""
    for key, column in (("department", "r.department_id"), ("category", "r.category_id")):
        value = to_int(filters[key])
        if value is not None:
            where.append(f"{column} = ?")
            params.append(value)
        else:
            filters[key] = ""
    if filters["q"]:
        where.append("(r.title ILIKE ? ESCAPE '\\' OR r.reference_number ILIKE ? ESCAPE '\\')")
        params += [like_pattern(filters["q"])] * 2
    date_from = parse_date(filters["date_from"])
    if date_from:
        # Dates are text like "2026-10-19 08:30:00"; the first 10 characters are the date.
        where.append("substr(r.created_at, 1, 10) >= ?")
        params.append(date_from)
    else:
        filters["date_from"] = ""
    date_to = parse_date(filters["date_to"])
    if date_to:
        where.append("substr(r.created_at, 1, 10) <= ?")
        params.append(date_to)
    else:
        filters["date_to"] = ""
    return " AND ".join(where), params, filters


def request_stats(db, where="1=1", params=()):
    # COUNT(*) FILTER (WHERE ...) counts only the rows that match the condition.
    row = db.execute(
        f"""SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE r.status NOT IN ('Resolved','Rejected')) AS open_cases,
                   COUNT(*) FILTER (WHERE r.status IN ('Submitted','Under Review','Assigned')) AS pending,
                   COUNT(*) FILTER (WHERE r.status = 'Awaiting Information') AS awaiting_info,
                   COUNT(*) FILTER (WHERE r.status = 'In Progress') AS in_progress,
                   COUNT(*) FILTER (WHERE r.status = 'Resolved') AS resolved,
                   COUNT(*) FILTER (WHERE r.status = 'Rejected') AS rejected
            FROM service_requests r WHERE {where}""", params).fetchone()
    return dict(row)


def stats_by_department(db):
    table = {d["id"]: {"name": d["name"], "counts": {s: 0 for s in STATUSES}, "total": 0}
             for d in db.execute("SELECT id, name FROM departments ORDER BY name")}
    for row in db.execute("SELECT department_id, status, COUNT(*) AS n FROM service_requests "
                          "GROUP BY department_id, status"):
        entry = table[row["department_id"]]
        entry["counts"][row["status"]] = row["n"]
        entry["total"] += row["n"]
    return list(table.values())
