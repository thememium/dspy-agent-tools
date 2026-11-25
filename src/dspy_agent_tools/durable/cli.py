"""Run durable workers for an application module.

Usage:
    dspy-durable worker app:durable
    dspy-durable worker app:durable --queue research --processes 4
"""

from __future__ import annotations

import argparse
import importlib
import multiprocessing
import threading
from collections.abc import Sequence
from typing import Any

from .agent import Durable


def _load(spec: str) -> Any:
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise SystemExit(f"expected module:attribute, got {spec!r}")
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attribute)
    except AttributeError as exc:
        raise SystemExit(f"{module_name!r} has no attribute {attribute!r}") from exc


def _run(spec: str, queues: Sequence[str]) -> None:
    _load(spec)
    from .runtime import runtime

    Durable.launch()
    if queues:
        missing = [queue for queue in queues if queue not in runtime().queues]
        if missing:
            Durable.shutdown()
            raise SystemExit(f"unknown queue(s): {', '.join(missing)}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        Durable.shutdown()


def _child(spec: str, queues: Sequence[str]) -> None:
    try:
        _run(spec, queues)
    except KeyboardInterrupt:
        pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dspy-durable")
    subparsers = parser.add_subparsers(dest="command", required=True)
    worker = subparsers.add_parser("worker", help="start durable workers")
    worker.add_argument("app", help="module:attribute that configures Durable")
    worker.add_argument("--queue", action="append", default=[], dest="queues")
    worker.add_argument("--processes", type=int, default=1)
    args = parser.parse_args(argv)

    if args.processes < 1:
        parser.error("--processes must be at least 1")
    if args.processes == 1:
        _run(args.app, args.queues)
        return 0

    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_child, args=(args.app, args.queues))
        for _ in range(args.processes)
    ]
    for process in processes:
        process.start()
    try:
        for process in processes:
            process.join()
    except KeyboardInterrupt:
        for process in processes:
            process.terminate()
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
