"""Vercel's Python entrypoint. Auto-detected: Vercel looks for `app.py`,
`index.py`, `server.py`, `main.py`, `wsgi.py` or `asgi.py` at the repo root or
under `src/`, `app/`, `api/`, and loads whichever top-level ASGI/WSGI
variable is named `app`.

This file's only job is making `src/` importable before touching anything
under it — the package lives at `src/dashboard`, and nothing installs it as
a site-package here. Vercel's Python build installs `requirements.txt` (the
minimal, hand-picked set at the repo root — see its own comment for why
that's a *different* list from `pyproject.toml`'s `[dashboard]` extra) but
never runs this repo's own `setup.py`/`pyproject.toml`, so without this the
import below fails with `ModuleNotFoundError` before the function ever
handles a request.
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dashboard.web import app  # noqa: E402  (path must be set first)

__all__ = ["app"]
