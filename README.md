# Public Service Request Portal

Ever reported a burst pipe or a pothole and wondered whether anyone actually read it? This app is
built for exactly that. A resident tells the county what's broken, gets a reference number, and
can watch the request move along until someone resolves it. On the other side, officers work
through their department's queue and administrators keep everything in order.

It's a small Flask app on PostgreSQL, with plain HTML, CSS and a little JavaScript. There's no
front-end framework and no build step. It runs on your own computer and on Vercel.

> New to all this? `docs/system_guide.pdf` walks through the whole thing step by step: how the
> code fits together, running it, debugging, Git and deploying.

---

## Run it on your computer

You need Python 3.10 or newer and a PostgreSQL database. A free hosted one from Neon or Supabase
is the easiest option, though a local install works just as well.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # Windows: copy .env.example .env
```

Open `.env` and set `DATABASE_URL` to your database, for example
`postgresql://user:password@localhost:5432/service_requests`. Then start the app:

```bash
python run.py
```

Visit http://127.0.0.1:5000. The first start creates the tables and five starter departments, so
there's nothing else to set up.

One catch: your database user must be allowed to run `CREATE EXTENSION citext` (it's what makes
emails and names case-insensitive). Hosted providers allow it. On a local install, run
`CREATE EXTENSION citext;` once if you hit a permission error.

## Signing in for the first time

Nobody can sign up as an officer or administrator from the website. The public sign-up page only
ever creates residents. Staff accounts come from one of these:

Put `ADMIN_EMAIL` and `ADMIN_PASSWORD` in `.env` before you set up the database.
If no administrator exists yet, one is created from those values. Sign in, then add your officers
from the **Users** page.

**Prefer the command line?**

```bash
flask --app portal create-admin              # a new administrator
flask --app portal create-department-admin   # a department officer, made on an admin's behalf
```

## Who can do what

There are three kinds of people, and the server checks every rule below. Hiding a button is never
the only protection.

**Residents** submit requests, attach up to three files, follow their own requests and answer
when an officer asks for more details. They can't see anyone else's requests, and they never see
internal notes.

**Department officers** work on one department's requests: the ones assigned to them, plus the
unassigned ones in their department. They can claim a request, change its status, reply to the
resident and leave private notes for colleagues. They can't reassign requests or manage users.

**Administrators** see everything. They create officers and other administrators, manage
departments and categories, assign requests, switch accounts on or off and read the audit log.
An administrator can't deactivate their own account, so nobody gets locked out by accident.

### How a request moves along

A status can only follow the paths below. Anything else is refused.

| From | Can go to |
|------|-----------|
| Submitted | Under Review, Assigned, Rejected |
| Under Review | Assigned, Awaiting Information, In Progress, Rejected |
| Assigned | Under Review, Awaiting Information, In Progress, Rejected |
| Awaiting Information | Under Review (when the resident replies), In Progress, Rejected |
| In Progress | Awaiting Information, Resolved, Rejected |
| Resolved, Rejected | nowhere, they're final |

Two small rules keep things honest. Resolving a request needs resolution details, and rejecting
one (or asking for more information) needs a message the resident can read. Every change is saved
in the request's history with who made it and when. Residents see a simpler three-step tracker
(Pending, In Progress, Resolved), so they aren't buried in internal statuses.

## Take it for a spin

1. Register a resident account and sign in, choose **New request**, fill it in and submit. Note the
   reference number, something like `PSR-2026-000003`.
2. Sign in as your administrator, open **Assignments** and give the request to the department's
   officer. Its status becomes *Assigned*.
3. Sign in as that officer. Mark it *In Progress*, post a public reply, add a private note, or ask
   the resident for more details.
4. Back as the resident, answer the question. The request returns to *Under Review*.
5. As the officer, finish with *Resolved* (say what was done) or *Rejected* (say why).
6. Anyone can check progress on the **Track** page with the reference number and the email address
   the request was submitted under.

## Settings

Only `DATABASE_URL` is needed on your own computer. The app reads `.env` if it's there, and real
environment variables always win over it.

| Variable | What it does | Default |
|----------|--------------|---------|
| `DATABASE_URL` | PostgreSQL connection string. **Required.** | none |
| `APP_ENV` | `development` or `production` | development |
| `SECRET_KEY` | Signs session cookies. **Required in production.** | a random key made at each start |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `ADMIN_NAME` | The first administrator, created only if none exists | not set |
| `REF_PREFIX` | Prefix for reference numbers | `PSR` |
| `APP_NAME`, `COUNTY_NAME` | Names shown in the header and footer | Public Service Request Portal, County Government |
| `PORT` | Port for `python run.py` | 5000 |
| `SESSION_COOKIE_SECURE` | `1` sends the cookie over HTTPS only (always on in production) | off in development |

To make a good `SECRET_KEY`:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

## Putting it on Vercel

Vercel runs the app as a serverless function with no permanent disk. That's why both the data and
the uploaded files live in PostgreSQL. `api/index.py` and `vercel.json` tell Vercel how to start
the app.

1. **Create a PostgreSQL database** (Neon, Supabase, or Vercel's Storage tab) and copy its
   connection string. If there's a "pooled" one, use that.
2. **Set up the tables once, from your own computer.** Put the connection string in `.env` as
   `DATABASE_URL`, add `ADMIN_EMAIL` and `ADMIN_PASSWORD`, then run:
   ```bash
   flask --app portal init-db
   ```
   This creates the tables, the starter departments and your first administrator. It's safe to
   run again. In production the app never does this by itself, so no visitor ever waits for it.
3. **Push the project to GitHub** and import it in Vercel (Add New, then Project).
4. **Add environment variables** under Project Settings, then Environment Variables:

   | Name | Value |
   |------|-------|
   | `APP_ENV` | `production` |
   | `SECRET_KEY` | a long random value |
   | `DATABASE_URL` | your PostgreSQL connection string |

   `ADMIN_EMAIL` and `ADMIN_PASSWORD` were only needed for step 2, so they don't have to go on
   Vercel.
5. **Deploy**, sign in as the administrator and add your officers.

Worth knowing:

- Vercel rejects requests bigger than 4.5 MB, so attachments are limited to 1.3 MB each and 4 MB
  per submission (`MAX_FILE_BYTES` in `portal/__init__.py`). The upload form shrinks large phone photos in
  the browser automatically (PDFs over the limit are refused). If
  you outgrow this, move files to object storage such as S3.
- Rate limiting is kept in the database, so it still works when Vercel uses a different server
  for every request.
- Back up the database. It holds everything, uploaded files included.

## When something breaks

- **"Set the DATABASE_URL environment variable"**: the app can't find your `.env`, or the value is
  empty. Check that the file is named `.env` (not `.env.example`) and sits next to `run.py`.
- **A `citext` permission error on start**: see the note under "Run it on your computer".
- **Every Vercel page shows a 500 error**: check the function logs in Vercel first. The usual
  causes are a missing `SECRET_KEY` or `DATABASE_URL`, or tables that were never created (run
  `flask --app portal init-db` against the production database).
- **You keep getting signed out locally**: with no `SECRET_KEY` set, a new random one is made on
  every restart. Put one in `.env`.
- **To see the real error behind a 500 page**: run `FLASK_DEBUG=1 python run.py` (on Windows
  PowerShell: `$env:FLASK_DEBUG=1; python run.py`). The terminal prints the full traceback. The
  error pages shown to visitors never include it, on purpose.

## Security, briefly

Passwords are hashed. Sessions use `HttpOnly` and `SameSite=Lax` cookies and are reset at
sign-in. Every form carries a CSRF token, all SQL is parameterised, and templates escape their
output while a strict Content-Security-Policy blocks inline scripts. Uploads must be PDF, PNG or
JPG, are checked by extension and by file signature, are capped at 1.3 MB each, and are only
handed out as downloads after a permission check. Sign-in, tracking, registration and password
changes are rate limited. Administrative and workflow actions go into an audit log, and error
pages never reveal SQL, file paths or secrets.

## Tests

The tests need an empty PostgreSQL database that they're allowed to **erase**. Never point them
at your real one.

```bash
TEST_DATABASE_URL=postgresql://user:pass@localhost/psr_test python -m unittest discover -s tests -v
```

There are 53 tests and they take about a minute (password hashing is slow on purpose). Each one
starts from an emptied database and uses made-up data. They cover registration and sign-in,
submitting requests, who can see what, assignments and status rules, public replies versus
private notes, CSRF, SQL injection and XSS attempts, upload safety, dashboard numbers,
pagination, rate limiting, tracking and audit logging.

## Where things live

```
run.py                  starts the development server
api/index.py            the entry point Vercel uses
vercel.json             sends every URL to api/index.py
requirements.txt        Flask and psycopg (the PostgreSQL driver)
.env.example            copy to .env and adjust
docs/system_guide.pdf   the step-by-step guide

portal/                 the application
  __init__.py           create_app(): settings, security headers, error pages
  database.py           the PostgreSQL connection, first-time setup, creating staff accounts
  schema.sql            tables, constraints and indexes
  security.py           password rules, CSRF, rate limiting, sign-in and role guards
  workflow.py           roles, statuses, allowed moves, permissions, audit trail
  queries.py            reusable SQL for listing, filtering and counting requests
  accounts.py           validating and creating users
  uploads.py            checking and saving attachments
  utils.py              small input-cleaning helpers
  cli.py                the flask commands (init-db, create-admin, ...)
  routes/
    public.py           home, sign-in, register, track, profile
    resident.py         resident dashboard, new request, request list and detail
    officer.py          officer pages, request processing, attachment downloads
    admin.py            dashboard, assignments, users, departments, categories, audit log
  templates/            Jinja2 pages: auth/, resident/, officer/, admin/, errors/
  static/               css/, js/ and img/

tests/test_portal.py    the automated tests
```

## Changing the pictures

The pictures that ship with the portal are SVG illustrations. Each image is an ordinary `<img>`
tag marked with an `<!-- IMAGE: ... -->` comment in the templates. To use your own, replace the
file in `portal/static/img/` (a photo works too, but then change the extension in the template,
for example `hero.svg` to `hero.jpg`).

| File | Used in | Suggested size |
|------|---------|----------------|
| `logo.svg` | header and browser tab (`base.html`) | square |
| `hero.svg`, `how-it-works.svg` | `home.html` | 1200 x 675 |
| `role-resident.svg`, `role-officer.svg`, `role-admin.svg` | `home.html` | 800 x 450 |
| `auth.svg` | sign-in and register pages, wide screens only | 800 x 1000 |
| `track.svg` | `track.html` | 1600 x 460 |

Photos residents attach to a request appear as thumbnails on the request page (`/attachments/<id>/view`)
and open in a viewer; PDFs stay download-only.

Keep each photo under about 500 KB and update the `alt` text to describe your picture. The colours are
at the top of `portal/static/css/style.css`.

## What it doesn't do (yet)

This is a first version. There are no payments, SMS or email notifications, links to outside
government systems, or advanced analytics.
