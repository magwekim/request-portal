"""Validating and creating user accounts."""
import re

from .database import insert_user
from .security import password_error
from .utils import clean


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


PHONE_RE = re.compile(r"^\+?[0-9][0-9 \-]{7,18}$")


def validate_new_user(form, db):
    errors = {}
    full_name, email = clean(form.get("full_name")), clean(form.get("email")).lower()
    phone, password = clean(form.get("phone_number")), form.get("password", "")
    confirm = form.get("confirm_password", password)
    if not 2 <= len(full_name) <= 100:
        errors["full_name"] = "Enter your full name (2 to 100 characters)."
    if not EMAIL_RE.match(email) or len(email) > 254:
        errors["email"] = "Enter a valid email address."
    elif db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        errors["email"] = "An account with this email already exists."
    if phone and not PHONE_RE.match(phone):
        errors["phone_number"] = "Enter a valid phone number, e.g. +254 712 345 678."
    problem = password_error(password)
    if problem:
        errors["password"] = problem
    elif password != confirm:
        errors["confirm_password"] = "Passwords do not match."
    values = {"full_name": full_name, "email": email, "phone_number": phone or None,
              "password": password}
    return values, errors


def create_user(db, values, role, department_id=None):
    """Insert a user from validate_new_user() output and return the new id."""
    return insert_user(db, values["full_name"], values["email"], values["password"], role,
                       department_id, values["phone_number"])
