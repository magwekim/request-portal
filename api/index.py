"""Vercel entry point.

Vercel looks for a Flask object called `app` in this file and runs it as a serverless function.
vercel.json sends every URL of the site to this function.
"""
import os
import sys
from urllib.parse import parse_qsl, urlencode
 
# Make the project root importable so that "import portal" works from inside api/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
 
from portal import create_app  # noqa: E402
 
PATH_PARAM = "__path"
 
 
class RestoreOriginalPath:
    """WSGI middleware: rebuild PATH_INFO from the ?__path= parameter added by the rewrite."""
 
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app
 
    def __call__(self, environ, start_response):
        pairs = parse_qsl(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        original = None
        rest = []
        for key, value in pairs:
            if key == PATH_PARAM and original is None:
                original = value
            else:
                rest.append((key, value))
        if original is not None:
            path = "/" + original.lstrip("/")
            environ["PATH_INFO"] = path
            environ["REQUEST_URI"] = path + ("?" + urlencode(rest) if rest else "")
            environ["RAW_URI"] = environ["REQUEST_URI"]
            environ["QUERY_STRING"] = urlencode(rest)
        return self.wsgi_app(environ, start_response)
 
 
flask_app = create_app()
app = RestoreOriginalPath(flask_app)
 
