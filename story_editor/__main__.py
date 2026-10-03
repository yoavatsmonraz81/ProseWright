"""Entry point for ``python -m story_editor``.

``serve`` is routed directly to the HTTP API so it does not import the full CLI
(and the Phase 1 index stack) at startup.
"""
from __future__ import annotations

import sys


def _serve_main(argv: list[str]) -> int:
    import argparse

    from .server import serve

    p = argparse.ArgumentParser(prog="story_editor serve")
    p.add_argument("--host", default=None, help="bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=None, help="port (default: 8765)")
    args = p.parse_args(argv)
    serve(host=args.host, port=args.port)
    return 0


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "serve":
        return _serve_main(sys.argv[2:])
    from .cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
