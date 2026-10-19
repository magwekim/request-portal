"""Vercel entry point.

Vercel looks for a Flask object called `app` in this file and runs it as a serverless function.
vercel.json sends every URL of the site to this function.
"""
import os
import sys

# Make the project root importable so that "import portal" works from inside api/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from portal import create_app  # noqa: E402

app = create_app()
