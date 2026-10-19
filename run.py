"""Start the development server:  python run.py

On Vercel this file is not used (see api/index.py).
"""
import os

from portal import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)),
            debug=os.environ.get("FLASK_DEBUG") == "1")
