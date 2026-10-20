"""Command-line helper:  flask --app portal init-db"""
import click

from . import database


def register(app):
    @app.cli.command("init-db")
    def init_db_command():
        """Create the tables, starter departments and first administrator (safe to repeat).

        Run this ONCE after pointing DATABASE_URL at your PostgreSQL database.
        """
        database.init_db(app)
        click.echo("Database is ready.")
