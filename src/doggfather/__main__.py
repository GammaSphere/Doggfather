"""Command line entry point: ``python -m doggfather <command>``."""

from __future__ import annotations

import argparse
import sys
from contextlib import closing

from . import db
from .config import load_settings


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app

    settings = load_settings(host=args.host, port=args.port)
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with closing(db.connect(settings.database_path)) as conn:
        applied = db.migrate(conn)
    print("applied: " + (", ".join(applied) or "nothing, schema is current"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="doggfather", description="Doggfather hackathon platform")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="migrate, seed if empty, and run the web server")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.set_defaults(func=cmd_serve)

    migrate = sub.add_parser("migrate", help="apply pending database migrations")
    migrate.set_defaults(func=cmd_migrate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args(["serve", *(argv or [])])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
