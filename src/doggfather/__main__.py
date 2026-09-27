"""Command line entry point: ``python -m doggfather <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from contextlib import closing
from pathlib import Path

from . import db
from .config import load_settings


def _open(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(settings.database_path)
    db.migrate(conn)
    return conn


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app
    from .seed import banner, seed

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings(host=args.host, port=args.port)
    app = create_app(settings)
    with closing(_open(settings)) as conn:
        result = seed(conn, settings)
    print(banner(settings, result), flush=True)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info", proxy_headers=True)
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with closing(db.connect(settings.database_path)) as conn:
        applied = db.migrate(conn)
    print("applied: " + (", ".join(applied) or "nothing, schema is current"))
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    from .seed import banner, seed

    settings = load_settings(fixtures_path=Path(args.fixtures) if args.fixtures else None)
    with closing(_open(settings)) as conn:
        result = seed(conn, settings)
    print(banner(settings, result))
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    from .services.bundles import import_bundle

    settings = load_settings()
    data = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
    with closing(_open(settings)) as conn:
        report = import_bundle(conn, data)
    print(f"imported {report.event_id}: {report.summary()}")
    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)
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

    seed = sub.add_parser("seed", help="load fixtures.json into an empty database")
    seed.add_argument("--fixtures", default=None, help="path to fixtures.json")
    seed.set_defaults(func=cmd_seed)

    imp = sub.add_parser("import", help="import an event bundle (fixtures.json shape)")
    imp.add_argument("bundle")
    imp.set_defaults(func=cmd_import)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args(["serve", *(argv or [])])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
