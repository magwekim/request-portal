"""Automated tests (standard library unittest; pytest can also run them).

Run:  python -m unittest discover -s tests -v

These tests need a PostgreSQL database that they are allowed to ERASE. Create an empty one and
point TEST_DATABASE_URL at it (never use your real database), for example:

    TEST_DATABASE_URL=postgresql://user:pass@localhost/psr_test python -m unittest discover -s tests -v

Every test starts from a freshly emptied database and uses synthetic data only.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from portal import accounts, create_app, queries, security, workflow  # noqa: E402
from portal.database import IntegrityError, connect, get_db  # noqa: E402

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n" + b"0" * 64
PASSWORD = "TestPass123"


@unittest.skipUnless(TEST_DATABASE_URL, "set TEST_DATABASE_URL to run the tests")
class BaseCase(unittest.TestCase):
    def reset_database(self):
        """Erase everything in the test database (tables are re-created by create_app)."""
        db = connect(self.db_url)
        db.run_script("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        db.commit()
        db.close()

    def setUp(self):
        self.db_url = TEST_DATABASE_URL
        self.reset_database()
        self.make_app()
        self.client = self.app.test_client()
        with self.app.app_context():
            db = get_db()
            self.depts = {r["name"]: r["id"] for r in db.execute("SELECT id, name FROM departments")}
            self.water = self.depts["Water and Sanitation"]
            self.roads = self.depts["Roads, Transport and Public Works"]
            self.water_cat = db.execute("SELECT id FROM request_categories WHERE department_id = ?",
                                        (self.water,)).fetchone()["id"]
            self.admin_id = self.add_user("Test Admin", "admin@test.local", "admin")
            self.officer_id = self.add_user("Water Officer", "officer@test.local", "officer", self.water)
            self.officer2_id = self.add_user("Roads Officer", "officer2@test.local", "officer", self.roads)
            self.res_a = self.add_user("Resident A", "a@test.local", "resident")
            self.res_b = self.add_user("Resident B", "b@test.local", "resident")
            db.commit()

    def make_app(self, **extra):
        cfg = {"TESTING": True, "SECRET_KEY": "test-secret", "DATABASE_URL": self.db_url}
        cfg.update(extra)
        self.app = create_app(cfg)

    def add_user(self, name, email, role, dept=None):
        return accounts.create_user(get_db(), {"full_name": name, "email": email, "phone_number": None,
                                              "password": PASSWORD}, role, dept)

    def token(self, client, url="/login"):
        page = client.get(url).get_data(as_text=True)
        match = re.search(r'name="csrf_token" value="([^"]+)"', page)
        self.assertIsNotNone(match, f"no CSRF token on {url}")
        return match.group(1)

    def login(self, email, password=PASSWORD, client=None):
        client = client or self.client
        return client.post("/login", data={"email": email, "password": password,
                                           "csrf_token": self.token(client)})

    def client_for(self, email):
        c = self.app.test_client()
        r = self.login(email, client=c)
        self.assertEqual(r.status_code, 302, f"login failed for {email}")
        return c

    def post(self, client, url, data=None, token_from="/", **kw):
        data = dict(data or {})
        data["csrf_token"] = self.token(client, token_from)
        return client.post(url, data=data, **kw)

    def q(self, sql, params=()):
        db = connect(self.db_url)
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()

    def submit(self, client, title="Burst pipe on Market Road", files=None, **over):
        data = {"department_id": str(self.water), "category_id": str(self.water_cat), "title": title,
                "description": "Water has been flowing onto the road for two days now.",
                "location": "Market Road"}
        data.update(over)
        if files:
            data["evidence"] = files
        return self.post(client, "/resident/requests/new", data, token_from="/resident/requests/new",
                         content_type="multipart/form-data")

    def make_request(self, email="a@test.local", **kw):
        c = self.client_for(email)
        r = self.submit(c, **kw)
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:500])
        return int(r.headers["Location"].rstrip("/").split("/")[-1]), c

    def assign_to_officer(self, rid, officer_id=None):
        admin = self.client_for("admin@test.local")
        r = self.post(admin, f"/admin/requests/{rid}/assign", {
            "department_id": str(self.water), "category_id": str(self.water_cat),
            "officer_id": str(officer_id or self.officer_id)}, token_from=f"/admin/requests/{rid}")
        self.assertEqual(r.status_code, 302)
        return admin

    def status_of(self, rid):
        return self.q("SELECT status FROM service_requests WHERE id = ?", (rid,))[0]["status"]


class TestDatabaseAndStartup(BaseCase):
    def test_tables_created_and_foreign_keys_enforced(self):
        tables = {r["name"] for r in self.q(
            "SELECT table_name AS name FROM information_schema.tables WHERE table_schema = 'public'")}
        for t in ("users", "departments", "request_categories", "service_requests",
                  "request_history", "attachments", "audit_logs"):
            self.assertIn(t, tables)
        with self.app.app_context():
            with self.assertRaises(IntegrityError):
                get_db().execute("INSERT INTO service_requests (reference_number, resident_id, "
                                 "department_id, category_id, title, description) "
                                 "VALUES ('X', 9999, 1, 1, 't', 'd')")

    def test_data_persists_across_restart(self):
        rid, _ = self.make_request()
        self.make_app()  # "restart": new app object on the same database file
        self.assertEqual(self.status_of(rid), "Submitted")
        users = self.q("SELECT COUNT(*) AS n FROM users")[0]["n"]
        self.assertEqual(users, 5)
        self.assertEqual(self.q("SELECT COUNT(*) AS n FROM departments")[0]["n"], 5)  # not re-seeded


class TestAuth(BaseCase):
    def register(self, **over):
        data = {"full_name": "New Resident", "email": "new@test.local", "phone_number": "+254712345678",
                "password": "Secret123", "confirm_password": "Secret123"}
        data.update(over)
        return self.post(self.client, "/register", data, token_from="/register")

    def test_registration_creates_resident_and_hashes_password(self):
        r = self.register(role="admin", is_active="1")  # extra fields must be ignored
        self.assertEqual(r.status_code, 302)
        row = self.q("SELECT * FROM users WHERE email='new@test.local'")[0]
        self.assertEqual(row["role"], "resident")
        self.assertNotEqual(row["password_hash"], "Secret123")
        self.assertNotIn("Secret123", row["password_hash"])
        self.assertTrue(security.verify_password(row["password_hash"], "Secret123"))
        self.assertFalse(security.verify_password(row["password_hash"], "secret123"))

    def test_registration_validation(self):
        self.assertEqual(self.register(email="a@test.local").status_code, 400)       # duplicate
        self.assertEqual(self.register(password="short", confirm_password="short").status_code, 400)
        self.assertEqual(self.register(password="onlyletters", confirm_password="onlyletters").status_code, 400)
        self.assertEqual(self.register(confirm_password="Different1").status_code, 400)
        self.assertEqual(self.register(email="not-an-email").status_code, 400)
        self.assertEqual(self.register(phone_number="abc").status_code, 400)

    def test_login_logout(self):
        self.assertEqual(self.login("a@test.local", "wrong").status_code, 401)
        self.assertEqual(self.login("nobody@test.local").status_code, 401)
        r = self.login("a@test.local")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/resident/dashboard").status_code, 200)
        self.post(self.client, "/logout")
        self.assertEqual(self.client.get("/resident/dashboard").status_code, 302)

    def test_login_redirects_each_role(self):
        for email, expected in (("a@test.local", "/resident/dashboard"),
                                ("officer@test.local", "/officer/dashboard"),
                                ("admin@test.local", "/admin/dashboard")):
            c = self.app.test_client()
            self.login(email, client=c)
            self.assertTrue(c.get("/dashboard").headers["Location"].endswith(expected))

    def test_open_redirect_is_blocked(self):
        c = self.app.test_client()
        r = c.post("/login", data={"email": "a@test.local", "password": PASSWORD, "next": "//evil.example",
                                   "csrf_token": self.token(c)})
        self.assertEqual(r.headers["Location"], "/dashboard")

    def test_login_rate_limit(self):
        for _ in range(5):
            self.assertEqual(self.login("a@test.local", "wrong").status_code, 401)
        self.assertEqual(self.login("a@test.local", "wrong").status_code, 429)
        self.assertEqual(self.login("a@test.local").status_code, 429)  # even the right password waits

    def test_deactivated_user_cannot_login_or_keep_session(self):
        c = self.client_for("a@test.local")
        admin = self.client_for("admin@test.local")
        r = self.post(admin, f"/admin/users/{self.res_a}/toggle", token_from="/admin/users")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(c.get("/resident/dashboard").status_code, 302)  # existing session invalid
        self.assertEqual(self.login("a@test.local").status_code, 403)

    def test_csrf_required(self):
        r = self.client.post("/login", data={"email": "a@test.local", "password": PASSWORD})
        self.assertEqual(r.status_code, 400)
        c = self.client_for("a@test.local")
        self.assertEqual(c.post("/logout", data={"csrf_token": "forged"}).status_code, 400)

    def test_password_change(self):
        c = self.client_for("a@test.local")
        self.post(c, "/profile/password", {"current_password": "bad", "new_password": "Another123",
                                           "confirm_password": "Another123"}, token_from="/profile")
        self.assertTrue(security.verify_password(self.q("SELECT password_hash FROM users WHERE id=?",
                                                       (self.res_a,))[0][0], PASSWORD))
        self.post(c, "/profile/password", {"current_password": PASSWORD, "new_password": "Another123",
                                           "confirm_password": "Another123"}, token_from="/profile")
        self.assertTrue(security.verify_password(self.q("SELECT password_hash FROM users WHERE id=?",
                                                       (self.res_a,))[0][0], "Another123"))


class TestRequests(BaseCase):
    def test_submission_and_unique_reference(self):
        rid1, _ = self.make_request("a@test.local")
        rid2, _ = self.make_request("b@test.local", title="Second request title")
        rows = self.q("SELECT reference_number, status FROM service_requests ORDER BY id")
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertRegex(row["reference_number"], r"^PSR-\d{4}-\d{6}$")
            self.assertEqual(row["status"], "Submitted")
        self.assertNotEqual(rows[0]["reference_number"], rows[1]["reference_number"])
        self.assertTrue(rows[0]["reference_number"].endswith(f"{rid1:06d}"))
        hist = self.q("SELECT * FROM request_history WHERE request_id = ?", (rid1,))
        self.assertEqual((hist[0]["old_status"], hist[0]["new_status"]), (None, "Submitted"))

    def test_invalid_submissions_rejected(self):
        c = self.client_for("a@test.local")
        for over in ({"title": "abc"}, {"description": "too short"}, {"department_id": "9999"},
                     {"category_id": ""}, {"department_id": str(self.roads)},  # category of other dept
                     {"location": "x" * 300}):
            r = self.submit(c, **over)
            self.assertEqual(r.status_code, 400, over)
        self.assertEqual(self.q("SELECT COUNT(*) FROM service_requests")[0][0], 0)

    def test_resident_cannot_set_status_or_owner_via_form(self):
        c = self.client_for("a@test.local")
        r = self.submit(c, status="Resolved", resident_id=str(self.res_b), assigned_officer_id=str(self.officer_id))
        rid = int(r.headers["Location"].rstrip("/").split("/")[-1])
        row = self.q("SELECT * FROM service_requests WHERE id = ?", (rid,))[0]
        self.assertEqual((row["status"], row["resident_id"], row["assigned_officer_id"]),
                         ("Submitted", self.res_a, None))

    def test_visibility_restricted_to_owner_and_authorised_staff(self):
        rid, owner = self.make_request("a@test.local")
        url = f"/resident/requests/{rid}"
        self.assertEqual(owner.get(url).status_code, 200)
        self.assertEqual(self.client_for("b@test.local").get(url).status_code, 404)
        self.assertEqual(self.app.test_client().get(url).status_code, 302)
        self.assertEqual(self.client_for("officer@test.local").get("/officer/requests").status_code, 200)
        self.assertEqual(self.client_for("officer@test.local").get(f"/officer/requests/{rid}").status_code, 200)
        self.assertEqual(self.client_for("officer2@test.local").get(f"/officer/requests/{rid}").status_code, 404)
        self.assertEqual(self.client_for("admin@test.local").get(f"/admin/requests/{rid}").status_code, 200)
        # a resident listing never contains someone else's request
        page = self.client_for("b@test.local").get("/resident/requests").get_data(as_text=True)
        self.assertNotIn("Burst pipe", page)

    def test_search_filters_and_sql_injection(self):
        self.make_request("a@test.local")
        c = self.client_for("admin@test.local")
        for evil in ("' OR '1'='1", "'; DROP TABLE users; --", "%", "_"):
            r = c.get("/admin/requests", query_string={"q": evil, "status": evil, "department": evil,
                                                       "date_from": evil, "page": evil})
            self.assertEqual(r.status_code, 200)
        self.assertIn("PSR-", c.get("/admin/requests?q=Burst").get_data(as_text=True))
        self.assertNotIn("PSR-", c.get("/admin/requests?status=Resolved").get_data(as_text=True))
        self.assertEqual(self.q("SELECT COUNT(*) FROM users")[0][0], 5)

    def test_sql_injection_in_login_and_tracking(self):
        self.assertEqual(self.login("' OR '1'='1' --", "' OR '1'='1").status_code, 401)
        c = self.app.test_client()
        r = self.post(c, "/track", {"reference": "' OR 1=1 --", "email": "' OR 1=1 --"}, token_from="/track")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.q("SELECT COUNT(*) FROM users")[0][0], 5)

    def test_xss_is_escaped(self):
        c = self.client_for("a@test.local")
        r = self.submit(c, title="<script>alert(1)</script> title")
        page = c.get(r.headers["Location"]).get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)


class TestWorkflow(BaseCase):
    def test_full_lifecycle_submission_to_resolution(self):
        rid, resident = self.make_request()
        self.assertEqual(self.status_of(rid), "Submitted")
        self.assign_to_officer(rid)
        self.assertEqual(self.status_of(rid), "Assigned")
        officer = self.client_for("officer@test.local")
        detail = f"/officer/requests/{rid}"
        upd = lambda data: self.post(officer, f"/staff/requests/{rid}/status", data, token_from=detail)
        upd({"new_status": "In Progress", "public_comment": "Crew dispatched."})
        self.assertEqual(self.status_of(rid), "In Progress")
        # resolving requires details
        upd({"new_status": "Resolved"})
        self.assertEqual(self.status_of(rid), "In Progress")
        upd({"new_status": "Resolved", "resolution_details": "Pipe replaced on site."})
        row = self.q("SELECT * FROM service_requests WHERE id=?", (rid,))[0]
        self.assertEqual(row["status"], "Resolved")
        self.assertEqual(row["resolution_details"], "Pipe replaced on site.")
        self.assertIsNotNone(row["resolved_at"])
        page = resident.get(f"/resident/requests/{rid}").get_data(as_text=True)
        self.assertIn("Pipe replaced on site.", page)
        self.assertIn("Crew dispatched.", page)
        hist = self.q("SELECT old_status, new_status, changed_by FROM request_history "
                      "WHERE request_id=? AND old_status IS DISTINCT FROM new_status ORDER BY id", (rid,))
        self.assertEqual([(h["old_status"], h["new_status"]) for h in hist],
                         [(None, "Submitted"), ("Submitted", "Assigned"), ("Assigned", "In Progress"),
                          ("In Progress", "Resolved")])
        self.assertEqual(hist[-1]["changed_by"], self.officer_id)

    def test_invalid_transitions_rejected(self):
        rid, _ = self.make_request()
        self.assign_to_officer(rid)
        officer = self.client_for("officer@test.local")
        for bad in ("Resolved", "Submitted", "Bogus", ""):
            self.post(officer, f"/staff/requests/{rid}/status", {"new_status": bad, "resolution_details": "x",
                      "public_comment": "x"}, token_from=f"/officer/requests/{rid}")
            self.assertEqual(self.status_of(rid), "Assigned", bad)
        # closed requests are final
        self.post(officer, f"/staff/requests/{rid}/status", {"new_status": "Rejected", "public_comment": "Out of scope."},
                  token_from=f"/officer/requests/{rid}")
        self.assertEqual(self.status_of(rid), "Rejected")
        self.post(officer, f"/staff/requests/{rid}/status", {"new_status": "In Progress"},
                  token_from=f"/officer/requests/{rid}")
        self.assertEqual(self.status_of(rid), "Rejected")

    def test_rejection_needs_public_justification(self):
        rid, _ = self.make_request()
        self.assign_to_officer(rid)
        officer = self.client_for("officer@test.local")
        self.post(officer, f"/staff/requests/{rid}/status", {"new_status": "Rejected"}, token_from=f"/officer/requests/{rid}")
        self.assertEqual(self.status_of(rid), "Assigned")

    def test_resident_cannot_change_status(self):
        rid, resident = self.make_request()
        r = self.post(resident, f"/staff/requests/{rid}/status", {"new_status": "Resolved", "resolution_details": "x"},
                      token_from="/resident/dashboard")
        self.assertEqual(r.status_code, 403)
        r = self.post(resident, f"/resident/requests/{rid}/reply", {"message": "hello there", "new_status": "Resolved"},
                      token_from=f"/resident/requests/{rid}")
        self.assertEqual(self.status_of(rid), "Submitted")

    def test_officer_cannot_modify_other_departments_or_other_officers_requests(self):
        rid, _ = self.make_request()
        other = self.client_for("officer2@test.local")
        r = self.post(other, f"/staff/requests/{rid}/status", {"new_status": "Under Review"}, token_from="/officer/dashboard")
        self.assertEqual(r.status_code, 404)
        # same department, but assigned to someone else -> view only
        with self.app.app_context():
            self.add_user("Water Officer 2", "officer3@test.local", "officer", self.water)
            get_db().commit()
        self.assign_to_officer(rid)
        c3 = self.client_for("officer3@test.local")
        self.assertEqual(c3.get(f"/officer/requests/{rid}").status_code, 200)
        r = self.post(c3, f"/staff/requests/{rid}/status", {"new_status": "In Progress"}, token_from="/officer/dashboard")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.status_of(rid), "Assigned")

    def test_officer_can_claim_unassigned_department_request(self):
        rid, _ = self.make_request()
        officer = self.client_for("officer@test.local")
        self.post(officer, f"/staff/requests/{rid}/claim", token_from=f"/officer/requests/{rid}")
        row = self.q("SELECT status, assigned_officer_id FROM service_requests WHERE id=?", (rid,))[0]
        self.assertEqual((row["status"], row["assigned_officer_id"]), ("Assigned", self.officer_id))

    def test_public_response_vs_internal_note(self):
        rid, resident = self.make_request()
        self.assign_to_officer(rid)
        officer = self.client_for("officer@test.local")
        detail = f"/officer/requests/{rid}"
        self.post(officer, f"/staff/requests/{rid}/note", {"internal_note": "SECRET-INTERNAL-NOTE"}, token_from=detail)
        self.post(officer, f"/staff/requests/{rid}/respond", {"public_comment": "PUBLIC-REPLY-TEXT"}, token_from=detail)
        self.post(officer, f"/staff/requests/{rid}/status", {"new_status": "In Progress", "public_comment": "Started.",
                  "internal_note": "SECRET-STATUS-NOTE"}, token_from=detail)
        staff_page = officer.get(detail).get_data(as_text=True)
        self.assertIn("SECRET-INTERNAL-NOTE", staff_page)
        self.assertIn("SECRET-STATUS-NOTE", staff_page)
        res_page = resident.get(f"/resident/requests/{rid}").get_data(as_text=True)
        self.assertIn("PUBLIC-REPLY-TEXT", res_page)
        self.assertNotIn("SECRET-INTERNAL-NOTE", res_page)
        self.assertNotIn("SECRET-STATUS-NOTE", res_page)
        track = self.app.test_client()
        page = self.post(track, "/track", {"reference": self.q("SELECT reference_number FROM service_requests")[0][0],
                         "email": "a@test.local"}, token_from="/track").get_data(as_text=True)
        self.assertIn("PUBLIC-REPLY-TEXT", page)
        self.assertNotIn("SECRET", page)

    def test_request_info_and_resident_reply(self):
        rid, resident = self.make_request()
        self.assign_to_officer(rid)
        officer = self.client_for("officer@test.local")
        # reply not allowed unless info requested
        self.post(resident, f"/resident/requests/{rid}/reply", {"message": "unsolicited message"},
                  token_from=f"/resident/requests/{rid}")
        self.assertEqual(self.status_of(rid), "Assigned")
        self.post(officer, f"/staff/requests/{rid}/request-info", {"public_comment": "Please send the meter number."},
                  token_from=f"/officer/requests/{rid}")
        self.assertEqual(self.status_of(rid), "Awaiting Information")
        page = resident.get(f"/resident/requests/{rid}").get_data(as_text=True)
        self.assertIn("Please send the meter number.", page)
        self.assertIn('name="message"', page)
        self.post(resident, f"/resident/requests/{rid}/reply", {"message": "Meter number 12345"},
                  token_from=f"/resident/requests/{rid}")
        self.assertEqual(self.status_of(rid), "Under Review")
        self.assertIn("Meter number 12345", officer.get(f"/officer/requests/{rid}").get_data(as_text=True))

    def test_assignment_page_quick_assign(self):
        rid, _ = self.make_request()
        admin = self.client_for("admin@test.local")
        page = admin.get("/admin/assignments").get_data(as_text=True)
        self.assertIn("Water Officer", page)                      # only this department's officer
        self.assertNotIn("Roads Officer", page)
        self.assertIn(f'name="department_id" value="{self.water}"', page)
        r = self.post(admin, f"/admin/requests/{rid}/assign", {"department_id": str(self.water),
                      "category_id": str(self.water_cat), "officer_id": str(self.officer_id), "back": "list"},
                      token_from="/admin/assignments")
        self.assertTrue(r.headers["Location"].endswith("/admin/assignments"))
        self.assertEqual(self.status_of(rid), "Assigned")
        self.assertIn("Every open request has an officer assigned", admin.get("/admin/assignments").get_data(as_text=True))

    def test_admin_assignment_validation(self):
        rid, _ = self.make_request()
        admin = self.client_for("admin@test.local")
        post = lambda d: self.post(admin, f"/admin/requests/{rid}/assign", d, token_from=f"/admin/requests/{rid}")
        # officer from the wrong department
        post({"department_id": str(self.water), "category_id": str(self.water_cat), "officer_id": str(self.officer2_id)})
        self.assertIsNone(self.q("SELECT assigned_officer_id FROM service_requests WHERE id=?", (rid,))[0][0])
        # category not in department
        post({"department_id": str(self.roads), "category_id": str(self.water_cat), "officer_id": ""})
        self.assertEqual(self.q("SELECT department_id FROM service_requests WHERE id=?", (rid,))[0][0], self.water)
        # valid, then un-assign returns to Under Review
        post({"department_id": str(self.water), "category_id": str(self.water_cat), "officer_id": str(self.officer_id)})
        self.assertEqual(self.status_of(rid), "Assigned")
        post({"department_id": str(self.water), "category_id": str(self.water_cat), "officer_id": ""})
        self.assertEqual(self.status_of(rid), "Under Review")


class TestAccessControl(BaseCase):
    ADMIN_URLS = ["/admin/dashboard", "/admin/requests", "/admin/users", "/admin/departments",
                  "/admin/categories", "/admin/assignments", "/admin/audit"]

    def test_anonymous_redirected_to_login(self):
        c = self.app.test_client()
        for url in self.ADMIN_URLS + ["/resident/dashboard", "/officer/dashboard", "/profile", "/attachments/1",
                                          "/attachments/1/view"]:
            r = c.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn("/login", r.headers["Location"])

    def test_wrong_role_forbidden(self):
        resident, officer = self.client_for("a@test.local"), self.client_for("officer@test.local")
        for url in self.ADMIN_URLS + ["/officer/dashboard", "/officer/requests"]:
            self.assertEqual(resident.get(url).status_code, 403, url)
        for url in self.ADMIN_URLS + ["/resident/dashboard", "/resident/requests/new"]:
            self.assertEqual(officer.get(url).status_code, 403, url)
        self.assertEqual(self.client_for("admin@test.local").get("/resident/dashboard").status_code, 403)

    def test_non_admin_cannot_use_admin_actions(self):
        officer = self.client_for("officer@test.local")
        r = self.post(officer, "/admin/users/create-officer", {"full_name": "Evil", "email": "evil@test.local",
                      "password": "Secret123", "department_id": str(self.water)}, token_from="/officer/dashboard")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.q("SELECT COUNT(*) FROM users WHERE email='evil@test.local'")[0][0], 0)

    def test_custom_404_and_no_leak_on_errors(self):
        r = self.app.test_client().get("/does-not-exist")
        self.assertEqual(r.status_code, 404)
        self.assertIn("Page not found", r.get_data(as_text=True))

    def test_security_headers(self):
        r = self.app.test_client().get("/")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        c = self.app.test_client()
        c.get("/login")
        cookie = self.login("a@test.local", client=c).headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)


class TestProgressTracker(BaseCase):
    def test_simple_progress_tracker_follows_status(self):
        self.assertEqual([workflow.progress_step(s) for s in workflow.STATUSES],
                         [0, 0, 0, 0, 1, 2, None])
        rid, resident = self.make_request()
        url = f"/resident/requests/{rid}"
        page = resident.get(url).get_data(as_text=True)
        self.assertIn('aria-label="Request progress"', page)
        self.assertRegex(page, r'<li class="current" aria-current="step">\s*<span[^>]*>1</span>\s*<span class="progress-label">Pending')
        self.assign_to_officer(rid)
        officer = self.client_for("officer@test.local")
        self.post(officer, f"/staff/requests/{rid}/status", {"new_status": "In Progress"}, token_from=f"/officer/requests/{rid}")
        self.assertRegex(resident.get(url).get_data(as_text=True), r'class="current" aria-current="step">\s*<span[^>]*>2</span>\s*<span class="progress-label">In Progress')
        self.post(officer, f"/staff/requests/{rid}/status", {"new_status": "Resolved", "resolution_details": "Done"}, token_from=f"/officer/requests/{rid}")
        self.assertRegex(resident.get(url).get_data(as_text=True), r'class="current" aria-current="step">\s*<span[^>]*>&#10003;</span>\s*<span class="progress-label">Resolved')


class TestTracking(BaseCase):
    def track(self, ref, email):
        return self.post(self.app.test_client(), "/track", {"reference": ref, "email": email}, token_from="/track")

    def test_requires_reference_and_matching_email(self):
        self.make_request()
        ref = self.q("SELECT reference_number FROM service_requests")[0][0]
        ok = self.track(ref, "a@test.local")
        self.assertEqual(ok.status_code, 200)
        body = ok.get_data(as_text=True)
        self.assertIn(ref, body)
        self.assertNotIn("Burst pipe", body)            # title is not revealed
        self.assertNotIn("Water has been flowing", body)  # description is not revealed
        self.assertEqual(self.track(ref, "b@test.local").status_code, 404)
        self.assertEqual(self.track("PSR-2026-999999", "a@test.local").status_code, 404)

    def test_tracking_is_rate_limited(self):
        for _ in range(10):
            self.track("PSR-2026-000001", "x@test.local")
        self.assertEqual(self.track("PSR-2026-000001", "x@test.local").status_code, 429)


class TestUploads(BaseCase):
    def upload(self, client, name, content, ctype="application/octet-stream"):
        import io
        return self.submit(client, files=(io.BytesIO(content), name))

    def test_valid_upload_stored_in_database_and_downloadable_by_owner_only(self):
        c = self.client_for("a@test.local")
        r = self.upload(c, "../../evidence photo.png", PNG)
        self.assertEqual(r.status_code, 302)
        rid = int(r.headers["Location"].rstrip("/").split("/")[-1])
        att = self.q("SELECT * FROM attachments WHERE request_id=?", (rid,))[0]
        self.assertNotIn("/", att["original_filename"])
        self.assertNotIn("..", att["original_filename"])
        self.assertEqual(att["content_type"], "image/png")
        self.assertEqual(bytes(att["content"]), PNG)      # the file itself is stored in the database
        url = f"/attachments/{att['id']}"
        got = c.get(url)
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.data, PNG)
        got.close()
        self.assertIn("attachment", got.headers["Content-Disposition"])
        self.assertEqual(self.client_for("b@test.local").get(url).status_code, 404)
        self.assertEqual(self.client_for("officer2@test.local").get(url).status_code, 404)
        staff_dl = self.client_for("officer@test.local").get(url)
        self.assertEqual(staff_dl.status_code, 200)
        staff_dl.close()
        self.assertEqual(self.app.test_client().get(url).status_code, 302)

    def _request_with(self, client, name, content):
        r = self.upload(client, name, content)
        self.assertEqual(r.status_code, 302)
        rid = int(r.headers["Location"].rstrip("/").split("/")[-1])
        att = self.q("SELECT id FROM attachments WHERE request_id=?", (rid,))[0]
        return rid, att["id"]

    def test_photo_is_shown_inline_to_those_who_may_see_the_request(self):
        c = self.client_for("a@test.local")
        _, att_id = self._request_with(c, "road.png", PNG)
        url = f"/attachments/{att_id}/view"
        got = c.get(url)
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.mimetype, "image/png")
        self.assertEqual(got.data, PNG)
        self.assertNotIn("attachment", got.headers.get("Content-Disposition", ""))
        self.assertEqual(got.headers["X-Content-Type-Options"], "nosniff")
        got.close()
        staff = self.client_for("officer@test.local").get(url)
        self.assertEqual(staff.status_code, 200)
        staff.close()
        # Same rules as downloads: other residents and other departments get a 404.
        self.assertEqual(self.client_for("b@test.local").get(url).status_code, 404)
        self.assertEqual(self.client_for("officer2@test.local").get(url).status_code, 404)
        self.assertEqual(self.app.test_client().get(url).status_code, 302)

    def test_pdf_is_never_shown_inline(self):
        c = self.client_for("a@test.local")
        _, att_id = self._request_with(c, "letter.pdf", PDF)
        self.assertEqual(c.get(f"/attachments/{att_id}/view").status_code, 404)
        dl = c.get(f"/attachments/{att_id}")
        self.assertEqual(dl.status_code, 200)       # the download still works
        self.assertIn("attachment", dl.headers["Content-Disposition"])
        dl.close()

    def test_request_pages_show_photo_thumbnails_and_pdf_cards(self):
        import io
        c = self.client_for("a@test.local")
        r = self.submit(c, files=[(io.BytesIO(PNG), "road.png"), (io.BytesIO(PDF), "letter.pdf")])
        rid = int(r.headers["Location"].rstrip("/").split("/")[-1])
        png_id, pdf_id = [row["id"] for row in
                          self.q("SELECT id FROM attachments WHERE request_id=? ORDER BY id", (rid,))]
        for client, url in ((c, f"/resident/requests/{rid}"),
                            (self.client_for("officer@test.local"), f"/officer/requests/{rid}")):
            body = client.get(url).get_data(as_text=True)
            self.assertIn(f'<img src="/attachments/{png_id}/view"', body)
            self.assertIn("data-lightbox", body)
            self.assertNotIn(f"/attachments/{pdf_id}/view", body)   # a PDF has no thumbnail ...
            self.assertIn(f"/attachments/{pdf_id}", body)           # ... but can be downloaded
            self.assertIn("evidence-doc", body)
        staff_body = self.client_for("officer@test.local").get(f"/officer/requests/{rid}").get_data(as_text=True)
        self.assertIn("Resident A", staff_body)                      # uploader shown to staff only
        self.assertNotIn("Resident A,", c.get(f"/resident/requests/{rid}").get_data(as_text=True))

    def test_upload_form_limit_matches_the_server_limit(self):
        body = self.client_for("a@test.local").get("/resident/requests/new").get_data(as_text=True)
        self.assertIn(f'data-max-bytes="{self.app.config["MAX_FILE_BYTES"]}"', body)

    def test_rejected_files(self):
        c = self.client_for("a@test.local")
        self.assertEqual(self.upload(c, "virus.exe", b"MZ" + b"0" * 50).status_code, 400)
        self.assertEqual(self.upload(c, "page.html", b"<script>alert(1)</script>").status_code, 400)
        self.assertEqual(self.upload(c, "fake.png", b"<script>alert(1)</script>").status_code, 400)  # bad signature
        self.assertEqual(self.upload(c, "empty.pdf", b"").status_code, 400)
        self.assertEqual(self.upload(c, "noext", PNG).status_code, 400)
        self.assertEqual(self.q("SELECT COUNT(*) FROM service_requests")[0][0], 0)
        self.assertEqual(self.q("SELECT COUNT(*) FROM attachments")[0][0], 0)

    def test_size_limit(self):
        self.make_app(MAX_FILE_BYTES=1024)
        c = self.client_for("a@test.local")
        self.assertEqual(self.upload(c, "big.pdf", PDF + b"0" * 2000).status_code, 400)
        self.assertEqual(self.upload(c, "small.pdf", PDF).status_code, 302)

    def test_too_many_files(self):
        import io
        c = self.client_for("a@test.local")
        files = [(io.BytesIO(PNG), f"f{i}.png") for i in range(4)]
        self.assertEqual(self.submit(c, files=files).status_code, 400)


class TestAdminBootstrap(BaseCase):
    def fresh_app(self, **cfg):
        """An app on a completely empty database (so no admin exists yet)."""
        self.reset_database()
        return create_app({"TESTING": True, "SECRET_KEY": "x", "DATABASE_URL": self.db_url, **cfg})

    def admins(self):
        return [(r[0], r[1]) for r in self.q("SELECT email, role FROM users WHERE role = 'admin'")]

    def test_first_admin_created_from_config_once(self):
        cfg = {"ADMIN_EMAIL": "Boss@Test.Local", "ADMIN_PASSWORD": "BossPass123", "ADMIN_NAME": "Chief"}
        app = self.fresh_app(**cfg)
        self.assertEqual(self.admins(), [("boss@test.local", "admin")])
        create_app({"TESTING": True, "SECRET_KEY": "x", "DATABASE_URL": self.db_url, **cfg})  # restart: no duplicate
        self.assertEqual(len(self.admins()), 1)
        client = app.test_client()
        self.assertEqual(self.login("boss@test.local", "BossPass123", client=client).status_code, 302)
        self.assertEqual(client.get("/admin/dashboard").status_code, 200)

    def test_no_admin_without_config_or_with_weak_password(self):
        self.fresh_app()
        self.assertEqual(self.admins(), [])
        self.fresh_app(ADMIN_EMAIL="a@test.local", ADMIN_PASSWORD="short")
        self.assertEqual(self.admins(), [])

    def test_existing_admin_prevents_bootstrap(self):
        # the base fixture already has an admin, so a configured one must not be added
        create_app({"TESTING": True, "SECRET_KEY": "x", "DATABASE_URL": self.db_url,
                    "ADMIN_EMAIL": "extra@test.local", "ADMIN_PASSWORD": "ExtraPass123"})
        self.assertEqual([r[0] for r in self.admins()], ["admin@test.local"])

    def test_admin_creates_department_officer_and_another_admin(self):
        admin = self.client_for("admin@test.local")
        base = {"full_name": "Staff Member", "password": "Staff12345"}
        r = self.post(admin, "/admin/users/create-officer", dict(base, email="dept@test.local", role="officer",
                      department_id=str(self.roads)), token_from="/admin/users")
        self.assertEqual(r.status_code, 302)
        r = self.post(admin, "/admin/users/create-officer", dict(base, email="admin2@test.local", role="admin"),
                      token_from="/admin/users")
        self.assertEqual(r.status_code, 302)
        rows = {r["email"]: (r["role"], r["department_id"]) for r in self.q("SELECT * FROM users")}
        self.assertEqual(rows["dept@test.local"], ("officer", self.roads))
        self.assertEqual(rows["admin2@test.local"], ("admin", None))
        bad = self.post(admin, "/admin/users/create-officer", dict(base, email="x@test.local", role="superuser"),
                        token_from="/admin/users")
        self.assertEqual(bad.status_code, 400)
        new_admin = self.app.test_client()
        self.assertEqual(self.login("admin2@test.local", "Staff12345", client=new_admin).status_code, 302)
        self.assertEqual(new_admin.get("/admin/dashboard").status_code, 200)

    def test_officer_cannot_create_admin(self):
        officer = self.client_for("officer@test.local")
        r = self.post(officer, "/admin/users/create-officer", {"full_name": "Sneaky", "email": "s@test.local",
                      "password": "Sneaky1234", "role": "admin"}, token_from="/officer/dashboard")
        self.assertEqual(r.status_code, 403)


class TestAdminManagement(BaseCase):
    def test_create_officer_and_deactivate(self):
        admin = self.client_for("admin@test.local")
        data = {"full_name": "New Officer", "email": "newofficer@test.local", "password": "Officer123",
                "department_id": str(self.roads)}
        self.assertEqual(self.post(admin, "/admin/users/create-officer", data, token_from="/admin/users").status_code, 302)
        row = self.q("SELECT * FROM users WHERE email='newofficer@test.local'")[0]
        self.assertEqual((row["role"], row["department_id"]), ("officer", self.roads))
        self.assertEqual(self.login("newofficer@test.local", "Officer123").status_code, 302)
        self.post(admin, f"/admin/users/{row['id']}/toggle", token_from="/admin/users")
        self.assertEqual(self.q("SELECT is_active FROM users WHERE id=?", (row["id"],))[0][0], 0)
        # invalid: officer without department, duplicate email
        bad = dict(data, email="x@test.local", department_id="")
        self.assertEqual(self.post(admin, "/admin/users/create-officer", bad, token_from="/admin/users").status_code, 400)
        dup = dict(data, email="a@test.local")
        self.assertEqual(self.post(admin, "/admin/users/create-officer", dup, token_from="/admin/users").status_code, 400)

    def test_admin_cannot_deactivate_self(self):
        admin = self.client_for("admin@test.local")
        self.post(admin, f"/admin/users/{self.admin_id}/toggle", token_from="/admin/users")
        self.assertEqual(self.q("SELECT is_active FROM users WHERE id=?", (self.admin_id,))[0][0], 1)

    def test_departments_and_categories(self):
        admin = self.client_for("admin@test.local")
        p = lambda url, d, t="/admin/departments": self.post(admin, url, d, token_from=t)
        self.assertEqual(p("/admin/departments/create", {"name": "Education", "description": "Schools"}).status_code, 302)
        self.assertEqual(p("/admin/departments/create", {"name": "education"}).status_code, 400)  # duplicate (NOCASE)
        dept = self.q("SELECT id FROM departments WHERE name='Education'")[0][0]
        self.assertEqual(p("/admin/categories/create", {"department_id": str(dept), "name": "School fees"},
                           "/admin/categories").status_code, 302)
        self.assertEqual(p("/admin/categories/create", {"department_id": str(dept), "name": "School fees"},
                           "/admin/categories").status_code, 400)
        # deactivated departments disappear from the resident form
        p(f"/admin/departments/{dept}/toggle", {})
        page = self.client_for("a@test.local").get("/resident/requests/new").get_data(as_text=True)
        self.assertNotIn("Education", page)

    def test_category_update_and_toggle(self):
        admin = self.client_for("admin@test.local")
        cat = self.water_cat
        url = f"/admin/categories/{cat}"
        # a bad name is refused and the old name stays
        r = self.post(admin, f"{url}/update", {"name": "x"}, token_from="/admin/categories")
        self.assertEqual(r.status_code, 302)
        self.assertNotEqual(self.q("SELECT name FROM request_categories WHERE id=?", (cat,))[0][0], "x")
        # a good name is saved
        self.post(admin, f"{url}/update", {"name": "Renamed category", "description": "New"},
                  token_from="/admin/categories")
        self.assertEqual(self.q("SELECT name FROM request_categories WHERE id=?", (cat,))[0][0],
                         "Renamed category")
        # toggling switches it off, then on again, and both are audited
        for expected in (0, 1):
            self.post(admin, f"{url}/toggle", token_from="/admin/categories")
            self.assertEqual(self.q("SELECT is_active FROM request_categories WHERE id=?", (cat,))[0][0],
                             expected)
        actions = [row[0] for row in self.q("SELECT action FROM audit_logs ORDER BY id")]
        self.assertIn("category_deactivated", actions)
        self.assertIn("category_activated", actions)
        # an id that does not exist is a 404, not a crash
        self.assertEqual(self.post(admin, "/admin/categories/99999/toggle",
                                   token_from="/admin/categories").status_code, 404)

    def test_audit_log_records_important_actions(self):
        rid, _ = self.make_request()
        self.assign_to_officer(rid)
        admin = self.client_for("admin@test.local")
        self.post(admin, "/admin/departments/create", {"name": "Audit Dept"}, token_from="/admin/departments")
        actions = {r["action"] for r in self.q("SELECT action FROM audit_logs")}
        for expected in ("request_submitted", "request_assigned", "status_changed", "department_created"):
            self.assertIn(expected, actions)
        page = admin.get("/admin/audit").get_data(as_text=True)
        self.assertIn("department_created", page)


class TestDashboardStatistics(BaseCase):
    def stat(self, page, key):
        return int(re.search(rf'data-stat="{key}">(\d+)<', page).group(1))

    def test_admin_dashboard_counts_match_database(self):
        admin = self.client_for("admin@test.local")
        page = admin.get("/admin/dashboard").get_data(as_text=True)
        self.assertEqual([self.stat(page, k) for k in ("total", "pending", "in_progress", "resolved", "rejected")],
                         [0, 0, 0, 0, 0])
        ids = [self.make_request("a@test.local", title=f"Request number {i}")[0] for i in range(4)]
        self.assign_to_officer(ids[0])
        self.assign_to_officer(ids[1])
        officer = self.client_for("officer@test.local")
        st = lambda rid, data: self.post(officer, f"/staff/requests/{rid}/status", data, token_from=f"/officer/requests/{rid}")
        st(ids[0], {"new_status": "In Progress"})
        st(ids[1], {"new_status": "In Progress"})
        st(ids[1], {"new_status": "Resolved", "resolution_details": "Done"})
        self.assign_to_officer(ids[3])
        st(ids[3], {"new_status": "Rejected", "public_comment": "Not a county matter"})
        page = admin.get("/admin/dashboard").get_data(as_text=True)
        self.assertEqual(self.stat(page, "total"), 4)
        self.assertEqual(self.stat(page, "in_progress"), 1)
        self.assertEqual(self.stat(page, "resolved"), 1)
        self.assertEqual(self.stat(page, "rejected"), 1)
        self.assertEqual(self.stat(page, "pending"), 1)   # ids[2] still Submitted
        self.assertEqual(self.stat(page, "open_cases"), 2)  # total - resolved - rejected
        self.assertIn("Open vs resolved cases", page)
        with self.app.app_context():
            stats = queries.request_stats(get_db())
            self.assertEqual(stats["total"], 4)
            water = [d for d in queries.stats_by_department(get_db()) if d["name"] == "Water and Sanitation"][0]
            self.assertEqual(water["total"], 4)
            self.assertEqual(water["counts"]["Resolved"], 1)

    def test_resident_and_officer_dashboards(self):
        self.make_request("a@test.local")
        a = self.client_for("a@test.local").get("/resident/dashboard").get_data(as_text=True)
        b = self.client_for("b@test.local").get("/resident/dashboard").get_data(as_text=True)
        self.assertEqual(self.stat(a, "total"), 1)
        self.assertEqual(self.stat(b, "total"), 0)
        self.assertIn("not submitted any requests", b)
        o1 = self.client_for("officer@test.local").get("/officer/dashboard").get_data(as_text=True)
        o2 = self.client_for("officer2@test.local").get("/officer/dashboard").get_data(as_text=True)
        self.assertEqual((self.stat(o1, "total"), self.stat(o2, "total")), (1, 0))


class TestPagination(BaseCase):
    def test_pagination_and_page_param_tolerance(self):
        c = self.client_for("a@test.local")
        for i in range(12):
            self.assertEqual(self.submit(c, title=f"Paged request {i:02d}").status_code, 302)
        page1 = c.get("/resident/requests").get_data(as_text=True)
        page2 = c.get("/resident/requests?page=2").get_data(as_text=True)
        link = re.compile(r'href="/resident/requests/\d+"')
        self.assertEqual(len(link.findall(page1)), 10)
        self.assertEqual(len(link.findall(page2)), 2)
        self.assertEqual(c.get("/resident/requests?page=999").status_code, 200)
        self.assertEqual(c.get("/resident/requests?page=-3").status_code, 200)


if __name__ == "__main__":
    unittest.main()
