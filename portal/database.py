"""PostgreSQL access layer (psycopg 3, parameterised queries everywhere).

Every query in the app looks like this:

    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    row["email"]      # columns by name ...
    row[0]            # ... or by position

``?`` placeholders are converted to PostgreSQL's ``%s`` automatically, and INSERT statements
give back ``lastrowid`` (the id of the new row). Keeping this in one place means the rest of
the app stays plain and short.
"""
import os
from datetime import datetime, timezone
from math import ceil

import psycopg
from flask import current_app, g
from psycopg.errors import IntegrityError  # re-exported: routes use "except IntegrityError"
from werkzeug.security import generate_password_hash

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

# Reference data created only when the departments table is empty.
DEFAULT_DEPARTMENTS = [
    ("Water and Sanitation", "Water supply, sewerage and sanitation services.",
     ["Water supply interruption", "Burst or leaking pipe", "Sewer blockage"]),
    ("Roads, Transport and Public Works", "County roads, drainage and street lighting.",
     ["Pothole or damaged road", "Faulty street light", "Blocked drainage"]),
    ("Health Services", "County health facilities and public health.",
     ["Health facility complaint", "Public health nuisance"]),
    ("Environment and Waste Management", "Garbage collection and environmental protection.",
     ["Missed garbage collection", "Illegal dumping"]),
    ("Trade, Licensing and Markets", "Business permits, markets and trading spaces.",
     ["Business permit query", "Market stall complaint"]),
]


def utcnow():
    """Current UTC time as 'YYYY-MM-DD HH:MM:SS' (the format used in every table)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def hash_password(password):
    try:
        return generate_password_hash(password)
    except (ValueError, AttributeError):  # Python built without scrypt
        return generate_password_hash(password, method="pbkdf2:sha256")


# A small wrapper that gives psycopg the simple interface described above

class Row(dict):
    """One result row. Works as row["name"] and as row[0], and dict(row) works too."""

    def __init__(self, keys, values):
        super().__init__(zip(keys, values))
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


def _make_row(cursor):
    """psycopg row factory: build a Row from the column names of the current query."""
    keys = [col.name for col in cursor.description] if cursor.description else []
    return lambda values: Row(keys, values)


class Result:
    """What db.execute() returns: fetchone / fetchall / iteration / rowcount / lastrowid."""

    def __init__(self, cursor, lastrowid=None):
        self._cursor = cursor
        self.lastrowid = lastrowid
        self.rowcount = cursor.rowcount

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def __iter__(self):
        return iter(self._cursor.fetchall())


class Database:
    """Wraps one psycopg connection."""

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=()):
        # psycopg uses %s as the placeholder (and treats a real % specially), so convert.
        sql = sql.replace("%", "%%").replace("?", "%s")
        is_insert = sql.lstrip().upper().startswith("INSERT")
        if is_insert and "RETURNING" not in sql.upper():
            # Every table has an "id" column; ask PostgreSQL for the new one (= lastrowid).
            cursor = self.conn.execute(sql + " RETURNING id", params)
            return Result(cursor, lastrowid=cursor.fetchone()["id"])
        return Result(self.conn.execute(sql, params))

    def run_script(self, script):
        """Run several SQL statements at once (used only for schema.sql)."""
        self.conn.execute(script)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


def connect(database_url):
    """Open a new PostgreSQL connection."""
    conn = psycopg.connect(
        database_url,
        row_factory=_make_row,
        # Hosted databases (Neon, Supabase...) put a connection pooler in front of Postgres.
        # Poolers do not support "prepared statements", so switch them off.
        prepare_threshold=None,
    )
    return Database(conn)


def get_db():
    """One connection per request, stored on Flask's `g` and closed afterwards."""
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE_URL"])
    return g.db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()  # an uncommitted transaction is discarded here


def init_db(app):
    """Create tables and default departments if they do not exist yet.

    Safe to run again and again. On Vercel run it once from your own computer with:
        flask --app portal init-db
    """
    db = connect(app.config["DATABASE_URL"])
    try:
        with open(SCHEMA_PATH, encoding="utf-8") as fh:
            db.run_script(fh.read())
        if db.execute("SELECT COUNT(*) AS n FROM departments").fetchone()["n"] == 0:
            for name, description, categories in DEFAULT_DEPARTMENTS:
                cur = db.execute(
                    "INSERT INTO departments (name, description) VALUES (?, ?)", (name, description))
                for cat in categories:
                    db.execute(
                        "INSERT INTO request_categories (department_id, name) VALUES (?, ?)",
                        (cur.lastrowid, cat))
        ensure_default_admin(db, app.config)
        db.commit()
    finally:
        db.close()


# User accounts
# The first administrator is created by init_db from ADMIN_EMAIL / ADMIN_PASSWORD when none
# exists. After that, administrators add officers and more administrators from the Users page.
# Public sign-up can only ever create residents.

def insert_user(db, full_name, email, password, role, department_id=None, phone_number=None):
    """Insert one user row and return the new id. Callers do the validation and the commit."""
    cur = db.execute(
        "INSERT INTO users (full_name, email, phone_number, password_hash, role, department_id, "
        "is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
        (full_name, email, phone_number, hash_password(password), role, department_id, utcnow()))
    return cur.lastrowid


def log_action(db, user_id, action, entity_id, details):
    """Write a user-related audit entry (workflow.audit can't be imported here: it imports us)."""
    db.execute(
        "INSERT INTO audit_logs (user_id, action, entity_type, entity_id, details, created_at) "
        "VALUES (?, ?, 'user', ?, ?, ?)", (user_id, action, entity_id, details, utcnow()))


def _check_credentials(email, password):
    email = email.strip().lower()
    if "@" not in email or len(password) < 8:
        raise ValueError("A valid email and a password of at least 8 characters are required.")
    return email


def create_admin(db, full_name, email, password):
    """Create an administrator and return the new user id."""
    email = _check_credentials(email, password)
    user_id = insert_user(db, full_name.strip() or "System Administrator", email, password, "admin")
    log_action(db, user_id, "admin_bootstrapped", user_id, email)
    return user_id


def ensure_default_admin(db, config):
    """Create the first administrator from configuration, only if no administrator exists."""
    email, password = config.get("ADMIN_EMAIL"), config.get("ADMIN_PASSWORD")
    if not email or not password:
        return
    if db.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone():
        return
    try:
        create_admin(db, config.get("ADMIN_NAME") or "System Administrator", email, password)
        print(f"Created the first administrator account: {email.strip().lower()}")
    except (ValueError, IntegrityError) as exc:
        print(f"Could not create the administrator from ADMIN_EMAIL/ADMIN_PASSWORD: {exc}")


def create_department_admin(db, created_by, full_name, email, password, department_id,
                            phone_number=None):
    """Create an officer tied to one department, on behalf of an active administrator.

    Raises ValueError for bad input or when ``created_by`` is not an active administrator.
    """
    creator = db.execute("SELECT role, is_active FROM users WHERE id = ?", (created_by,)).fetchone()
    if not creator or creator["role"] != "admin" or not creator["is_active"]:
        raise ValueError("Only an active application administrator can create department admins.")
    email = _check_credentials(email, password)
    dept = db.execute("SELECT name FROM departments WHERE id = ? AND is_active = 1",
                      (department_id,)).fetchone()
    if dept is None:
        raise ValueError("Select an active department.")
    user_id = insert_user(db, full_name.strip(), email, password, "officer", department_id,
                          phone_number)
    log_action(db, created_by, "department_admin_created", user_id, f"{email} ({dept['name']})")
    return user_id


def paginate(db, from_sql, where, params, columns, order, page, per_page):
    """Run a COUNT and a LIMIT/OFFSET query. SQL fragments are constants; values are bound."""
    total = db.execute(f"SELECT COUNT(*) FROM {from_sql} WHERE {where}", params).fetchone()[0]
    pages = max(1, ceil(total / per_page))
    page = min(max(page, 1), pages)
    rows = db.execute(
        f"SELECT {columns} FROM {from_sql} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, per_page, (page - 1) * per_page]).fetchall()
    return {"rows": rows, "page": page, "pages": pages, "total": total, "per_page": per_page}
