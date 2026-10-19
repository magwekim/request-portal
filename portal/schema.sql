-- Public Service Request System - PostgreSQL schema.
-- Safe to run more than once: every statement is IF NOT EXISTS.
--
-- Quick guide to the column types:
--   * SERIAL   = a number that counts up by itself (1, 2, 3...), used for every id
--   * CITEXT   = text that ignores upper/lower case, so "Ann@x.com" and "ann@x.com" match
--   * created_at and friends are plain TEXT in 'YYYY-MM-DD HH:MM:SS' (UTC time)

-- Case-insensitive text type (used for emails and names).
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE IF NOT EXISTS departments (
    id          SERIAL PRIMARY KEY,
    name        CITEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    full_name     TEXT NOT NULL,
    email         CITEXT NOT NULL UNIQUE,
    phone_number  TEXT,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('resident', 'officer', 'admin')),
    department_id INTEGER REFERENCES departments (id),
    is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at    TEXT NOT NULL DEFAULT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'),
    CHECK (role <> 'officer' OR department_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_users_role ON users (role);
CREATE INDEX IF NOT EXISTS idx_users_department ON users (department_id);

CREATE TABLE IF NOT EXISTS request_categories (
    id            SERIAL PRIMARY KEY,
    department_id INTEGER NOT NULL REFERENCES departments (id),
    name          CITEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    UNIQUE (department_id, name)
);

CREATE TABLE IF NOT EXISTS service_requests (
    id                  SERIAL PRIMARY KEY,
    reference_number    TEXT NOT NULL UNIQUE,
    resident_id         INTEGER NOT NULL REFERENCES users (id),
    department_id       INTEGER NOT NULL REFERENCES departments (id),
    category_id         INTEGER NOT NULL REFERENCES request_categories (id),
    assigned_officer_id INTEGER REFERENCES users (id),
    title               TEXT NOT NULL,
    description         TEXT NOT NULL,
    location            TEXT,
    status              TEXT NOT NULL DEFAULT 'Submitted' CHECK (status IN (
        'Submitted', 'Under Review', 'Assigned', 'Awaiting Information',
        'In Progress', 'Resolved', 'Rejected')),
    resolution_details  TEXT,
    created_at          TEXT NOT NULL DEFAULT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'),
    updated_at          TEXT NOT NULL DEFAULT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'),
    resolved_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_resident ON service_requests (resident_id);
CREATE INDEX IF NOT EXISTS idx_requests_department ON service_requests (department_id);
CREATE INDEX IF NOT EXISTS idx_requests_officer ON service_requests (assigned_officer_id);
CREATE INDEX IF NOT EXISTS idx_requests_status ON service_requests (status);
CREATE INDEX IF NOT EXISTS idx_requests_created ON service_requests (created_at);

CREATE TABLE IF NOT EXISTS request_history (
    id             SERIAL PRIMARY KEY,
    request_id     INTEGER NOT NULL REFERENCES service_requests (id) ON DELETE CASCADE,
    changed_by     INTEGER NOT NULL REFERENCES users (id),
    old_status     TEXT,
    new_status     TEXT NOT NULL CHECK (new_status IN (
        'Submitted', 'Under Review', 'Assigned', 'Awaiting Information',
        'In Progress', 'Resolved', 'Rejected')),
    public_comment TEXT,
    internal_note  TEXT,
    created_at     TEXT NOT NULL DEFAULT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
);
CREATE INDEX IF NOT EXISTS idx_history_request ON request_history (request_id);

-- Attachments are stored right inside the database (the "content" column), because Vercel
-- has no permanent disk. Files are small (see MAX_FILE_BYTES in portal/__init__.py).
CREATE TABLE IF NOT EXISTS attachments (
    id                SERIAL PRIMARY KEY,
    request_id        INTEGER NOT NULL REFERENCES service_requests (id) ON DELETE CASCADE,
    original_filename TEXT NOT NULL,
    content_type      TEXT NOT NULL,
    content           BYTEA NOT NULL,
    uploaded_by       INTEGER NOT NULL REFERENCES users (id),
    created_at        TEXT NOT NULL DEFAULT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
);
CREATE INDEX IF NOT EXISTS idx_attachments_request ON attachments (request_id);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER REFERENCES users (id),
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   INTEGER,
    details     TEXT,
    created_at  TEXT NOT NULL DEFAULT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs (created_at);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs (action);

-- Failed-login / lookup counters for rate limiting. They live in the database (not in
-- memory) because Vercel may run each request on a different server instance.
CREATE TABLE IF NOT EXISTS rate_limits (
    id           SERIAL PRIMARY KEY,
    key          TEXT NOT NULL,
    attempted_at DOUBLE PRECISION NOT NULL   -- Unix time in seconds
);
CREATE INDEX IF NOT EXISTS idx_rate_limits_key ON rate_limits (key);
