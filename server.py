"""Meridian: start the loopback server (or build datasets).

    python server.py                      start on http://127.0.0.1:8790/
    python server.py --open               ...and open the browser
    python server.py --port 8791 --only flights   one module (plus sanctions) on another port
    python server.py --build all          build/refresh every dataset, then exit
    python server.py --build infra --force

Exit codes: 0 stopped normally, 1 another problem, 2 a refused host or port.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

from meridian import APP_NAME, __version__
from meridian.config import DEFAULT_HOST, DEFAULT_PORT, Config
from meridian.core_api import register_core
from meridian.datasets import DatasetManager
from meridian.guards import BindRefused, resolve_bind
from meridian.httpserver import make_server
from meridian.log import setup as setup_logging
from meridian.modules import load_all
from meridian.net import Net
from meridian.registry import App
from meridian.scheduler import Scheduler
from meridian.store import Store


def build_app(config: Config, *, only: set[str] | None = None, console: bool = True,
              level: int = logging.INFO) -> App:
    data_dir = config.data_dir
    name = "meridian.log" if not only else "meridian-" + "-".join(sorted(only)) + ".log"
    log = setup_logging(config, data_dir / "logs", level=level, console=console, filename=name)
    store = Store(data_dir / "meridian.db")
    net = Net(config, logging.getLogger("meridian.net"))
    scheduler = Scheduler(logging.getLogger("meridian.scheduler"), config.redact)
    datasets = DatasetManager(logging.getLogger("meridian.datasets"), config.redact)
    app = App(config, store, net, log, scheduler, datasets)
    app.only = set(only) if only else None
    register_core(app)
    load_all(app, only)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} {__version__}")
    parser.add_argument("--host", default=None, help="loopback address (127.0.0.1, localhost or ::1)")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--only", default=None, help="comma-separated modules to load (sanctions always loads)")
    parser.add_argument("--open", action="store_true", help="open the browser once the server is up")
    parser.add_argument("--no-tasks", action="store_true", help="don't start background polling")
    parser.add_argument("--no-datasets", action="store_true", help="don't build datasets in the background")
    parser.add_argument("--build", default=None, metavar="WHAT",
                        help="build datasets and exit: all, a module name or module.dataset")
    parser.add_argument("--force", action="store_true", help="with --build: rebuild even if fresh")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    # MERIDIAN_ENV_FILE=none runs without the .env file (development copies run keyless); a path picks another file.
    env_choice = os.environ.get("MERIDIAN_ENV_FILE")
    if env_choice is not None and env_choice.strip().lower() in ("", "none", "off"):
        config = Config(env_file=None)
    elif env_choice:
        config = Config(env_file=Path(env_choice))
    else:
        config = Config()
    only = set(s.strip() for s in args.only.split(",") if s.strip()) if args.only else None
    level = logging.DEBUG if args.debug else logging.INFO

    if args.build:
        app = build_app(config, only=only, level=level)
        target = None if args.build == "all" else set(s.strip() for s in args.build.split(","))
        results = app.datasets.build_all(force=args.force, only=target)
        for key, ok in sorted(results.items()):
            print(f"{'ok    ' if ok else 'FAILED'} {key}")
        return 0 if all(results.values()) else 1

    host = args.host or config.get("MERIDIAN_HOST", DEFAULT_HOST)
    port = args.port if args.port is not None else config.get_int("MERIDIAN_PORT", DEFAULT_PORT)
    try:
        resolve_bind(host)
    except BindRefused as err:
        print(err.message, file=sys.stderr)
        return 2
    if not (0 <= port <= 65535):
        print("The port must be between 0 and 65535.", file=sys.stderr)
        return 2

    app = build_app(config, only=only, level=level)
    try:
        server = make_server(app, host, port)
    except OSError as err:
        print(f"Can't listen on {host}:{port} ({err}). Is Meridian already running?", file=sys.stderr)
        return 2
    bind_ip, actual = app.bind
    shown = f"[{bind_ip}]" if ":" in bind_ip else bind_ip
    url = f"http://{shown}:{actual}/"
    app.log.info("%s %s listening on %s", APP_NAME, __version__, url)
    if not args.no_datasets:
        app.datasets.start(app.scheduler.stop_event)
    if not args.no_tasks:
        app.history.schedule(app.scheduler)     # event history snapshots and pruning (CONTRACT 15.3)
        app.scheduler.start()
    if args.open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.log.info("stopping")
        app.scheduler.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
