# Public Service Request Portal

Residents report problems such as a broken streetlight, a water fault or missed garbage
collection, get a reference number, and follow the request until it is resolved. Department
officers work through their queue and administrators oversee everything.

Flask + PostgreSQL, plain HTML/CSS/JS, no build step. Runs locally and on Vercel.

## How it meets the brief

| Requirement | Where |
|-------------|-------|
| Citizen registration and login | `/register`, `/login` (hashed passwords, CSRF protection, rate limiting) |
| Submit a request: category, location, description, optional image | **New request**. Category, location and description are required; a photo is optional. The department is chosen from the category. |
| Admin dashboard | **Dashboard**, plus Requests (search, filter, CSV export), Assignments, Users, Departments, Categories, Audit log |
| Status tracking: Pending → In Progress → Resolved | Three-step tracker on every request and on the public **Track** page |
| Basic analytics: open vs resolved | Dashboard totals, resolution rate and an open vs resolved chart, plus average time to resolve, top categories and workload per department |

## Run locally

Requires Python 3.10+ and PostgreSQL (`docker compose up -d` starts one that matches `.env.example`).

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # set DATABASE_URL, ADMIN_EMAIL, ADMIN_PASSWORD
python run.py                 # http://127.0.0.1:5000
```

The first start creates the tables, starter departments and categories, and the first
administrator from `.env`. The database user must be allowed to run `CREATE EXTENSION citext`
(hosted providers allow it).

Public sign-up only creates residents. Sign in as the administrator and create officers from
the **Users** page.

## Roles

- **Resident**: submits requests, attaches up to 3 files, tracks their own requests, answers when more information is requested.
- **Officer**: handles their department's requests (claim, change status, reply publicly, add private notes).
- **Administrator**: sees everything; assigns requests; manages users, departments and categories; reads the audit log.

The server enforces every rule; hiding a button is never the only protection.

## Request statuses

Every request follows **Pending → In Progress → Resolved**, with two side branches:
*Awaiting Information* (officer needs something from the resident) and *Rejected* (closed with a reason).

| From | Can go to |
|------|-----------|
| Pending | In Progress, Awaiting Information, Rejected |
| In Progress | Resolved, Awaiting Information, Rejected |
| Awaiting Information | Pending (when the resident replies), In Progress, Rejected |
| Resolved, Rejected | final |

Resolving needs resolution details; rejecting or asking for information needs a message the
resident can read. Assigning an officer does not change the status. Every change is stored in
the request history.

## Settings

Real environment variables win over `.env`.

| Variable | Purpose | Default |
|----------|---------|---------|
| `DATABASE_URL` | PostgreSQL connection string (**required**) | none |
| `SECRET_KEY` | Signs sessions (**required in production**) | random per start |
| `APP_ENV` | `development` or `production` (Vercel counts as production) | development |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `ADMIN_NAME` | First administrator, created only if none exists | none |
| `REF_PREFIX`, `APP_NAME`, `COUNTY_NAME` | Reference prefix and display names | `PSR`, Public Service Request Portal, County Government |
| `PORT` | Port for `python run.py` | 5000 |

Generate a key with `python -c "import secrets; print(secrets.token_hex(32))"`.

## Deploy on Vercel

1. Create a PostgreSQL database (Neon, Supabase or Vercel Storage) and copy its connection string (the pooled one if offered).
2. From your computer, with `DATABASE_URL`, `ADMIN_EMAIL` and `ADMIN_PASSWORD` in `.env`, run once:
   `flask --app portal init-db`. In production the app never creates tables itself.
3. Push to GitHub and import the project in Vercel.
4. Add environment variables `SECRET_KEY` and `DATABASE_URL`, then deploy.

Notes: Vercel rejects requests over 4.5 MB, so attachments are limited to 1.3 MB each (large
photos are shrunk in the browser; see `MAX_FILE_BYTES` in `portal/__init__.py`). Uploaded files
are stored in PostgreSQL, and rate limits are kept there too, so back the database up.

## Troubleshooting

- **"Set the DATABASE_URL environment variable"**: `.env` is missing, misnamed or empty.
- **`citext` permission error**: run `CREATE EXTENSION citext;` once as a database owner.
- **500 on every Vercel page**: check the function logs; usually a missing `SECRET_KEY` or `DATABASE_URL`, or `init-db` was never run.
- **Signed out on every restart locally**: set `SECRET_KEY` in `.env`.
- **See the real error locally**: `FLASK_DEBUG=1 python run.py`.

## Security

Hashed passwords, HttpOnly/SameSite cookies, CSRF tokens on every form, parameterised SQL,
escaped templates, a strict Content-Security-Policy, uploads limited to PDF/PNG/JPG and checked
by extension and file signature, permission-checked downloads, rate-limited sign-in/tracking/
registration, and an audit log of administrative actions.

## Project layout

```
run.py, api/index.py     local server and Vercel entry point
vercel.json              routes every URL to api/index.py
portal/
  __init__.py            app factory: settings, security headers, error pages
  database.py            PostgreSQL access, first-time setup, staff accounts
  schema.sql             tables, constraints, indexes (and upgrade for older databases)
  workflow.py            statuses, allowed transitions, permissions, audit trail
  security.py            passwords, CSRF, rate limiting, login/role guards
  queries.py             shared SQL for listing, filtering and analytics
  accounts.py, uploads.py, utils.py, cli.py
  routes/                public, resident, officer, admin
  templates/             Jinja pages (_macros.html holds shared pieces)
public/static/           css, js, img (served by Vercel's CDN)
```
