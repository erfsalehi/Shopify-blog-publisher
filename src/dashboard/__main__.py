"""`python -m dashboard` — the one way to start the app.

Binds to loopback. The host is a setting only so it can be pointed at ::1 or a
different loopback alias; putting a LAN address there would expose an app with
no authentication, which is not a supported configuration.
"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from dashboard.config import get_settings


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="python -m dashboard",
        description="D&R Flooring Control Center — local dashboard.",
    )
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--host", default=settings.host)
    parser.add_argument(
        "--no-scheduler",
        action="store_true",
        help="Serve whatever is already in the database and run no sync jobs.",
    )
    parser.add_argument("--reload", action="store_true", help="Auto-reload on edits.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.no_scheduler:
        # create_app() reads this at startup, so set it before uvicorn imports
        # the app module.
        import os

        os.environ["DASHBOARD_ENABLE_SCHEDULER"] = "false"
        get_settings.cache_clear()

    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            f"Refusing to bind {args.host}: this app has no authentication and "
            "is meant to be reachable only from this machine.",
            file=sys.stderr,
        )
        return 2

    # ASCII only. The default Windows console codepage is cp1252, and a stray
    # arrow or box-drawing character here kills the process before uvicorn has
    # even started — an unhelpful way to fail at the very first thing the
    # owner types.
    print(f"\n  Control Center: http://{args.host}:{args.port}\n")
    uvicorn.run(
        "dashboard.web:app",
        host=args.host,
        port=args.port,
        reload=args.reload or settings.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
