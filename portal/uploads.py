"""Attachment handling: checking type, size and file signature, then saving.

Files are stored inside PostgreSQL (the attachments.content column) because Vercel has no
permanent disk to write them to.
"""
import os

from flask import current_app
from werkzeug.utils import secure_filename

from .database import utcnow

# extension -> (content type sent on download, bytes every genuine file starts with)
FILE_TYPES = {
    "pdf": ("application/pdf", b"%PDF-"),
    "png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    "jpg": ("image/jpeg", b"\xff\xd8\xff"),
    "jpeg": ("image/jpeg", b"\xff\xd8\xff"),
}


def validate_uploads(files):
    """Check every file's extension, size and signature. Returns (valid_files, error_messages)."""
    max_files = current_app.config["MAX_FILES"]
    max_bytes = current_app.config["MAX_FILE_BYTES"]
    files = [f for f in files if f and f.filename]
    if len(files) > max_files:
        return [], [f"You can attach at most {max_files} files."]

    valid, errors = [], []
    for f in files:
        name = secure_filename(f.filename)
        label = name or "file"
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in FILE_TYPES:
            errors.append(f"'{label}': only PDF, PNG and JPG files are allowed.")
            continue

        start = f.stream.read(16)
        f.stream.seek(0, os.SEEK_END)
        size = f.stream.tell()
        f.stream.seek(0)

        if size == 0:
            errors.append(f"'{label}' is empty.")
        elif size > max_bytes:
            errors.append(f"'{label}' is larger than {max_bytes // (1024 * 1024)} MB.")
        elif not start.startswith(FILE_TYPES[ext][1]):
            errors.append(f"'{label}' does not look like a valid {ext.upper()} file.")
        else:
            valid.append((f, ext, name[-120:]))
    return valid, errors


def store_uploads(db, valid, request_id, user_id):
    """Save each checked file as a row in the attachments table.

    Nothing is final until the caller commits, so a rollback removes the files as well.
    """
    for f, ext, original_name in valid:
        db.execute(
            "INSERT INTO attachments (request_id, original_filename, content_type, content, "
            "uploaded_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (request_id, original_name, FILE_TYPES[ext][0], f.stream.read(), user_id, utcnow()))
