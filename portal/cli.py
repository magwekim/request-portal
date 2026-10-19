"""Command-line helpers:
  flask --app portal init-db | create-admin | create-department-admin"""
import click

from . import accounts, database, workflow


def register(app):
    @app.cli.command("init-db")
    def init_db_command():
        """Create the tables and default departments in the database (safe to repeat).

        Run this ONCE after pointing DATABASE_URL at your hosted PostgreSQL database.
        """
        database.init_db(app)
        click.echo("Database is ready.")

    @app.cli.command("create-admin")
    @click.option("--name", prompt="Full name")
    @click.option("--email", prompt="Email")
    @click.password_option("--password")
    def create_admin(name, email, password):
        """Create an administrator account (the only way to create the first admin)."""
        db = database.get_db()
        values, errors = accounts.validate_new_user(
            {"full_name": name, "email": email, "password": password}, db)
        if errors:
            raise click.ClickException("; ".join(errors.values()))
        user_id = accounts.create_user(db, values, "admin")
        workflow.audit(db, user_id, "admin_created_cli", "user", user_id, values["email"])
        db.commit()
        click.echo(f"Administrator {values['email']} created.")

    @app.cli.command("create-department-admin")
    @click.option("--admin-email", prompt="Your (administrator) email")
    @click.option("--department", "department_id", type=int, prompt="Department id")
    @click.option("--name", prompt="Full name")
    @click.option("--email", prompt="Email")
    @click.password_option("--password")
    def create_department_admin(admin_email, department_id, name, email, password):
        """Create a department admin; must be run on behalf of an existing administrator."""
        db = database.get_db()
        admin = db.execute("SELECT id FROM users WHERE email = ? AND role = 'admin'",
                           (admin_email.strip().lower(),)).fetchone()
        if admin is None:
            raise click.ClickException("No administrator with that email.")
        values, errors = accounts.validate_new_user(
            {"full_name": name, "email": email, "password": password}, db)
        if errors:
            raise click.ClickException("; ".join(errors.values()))
        try:
            database.create_department_admin(db, admin["id"], values["full_name"], values["email"],
                                             password, department_id)
        except ValueError as exc:
            raise click.ClickException(str(exc))
        db.commit()
        click.echo(f"Department admin {values['email']} created.")
