"""`paperless-rules` CLI dispatcher.

Subcommands:
  editor        — start the FastAPI editor on $EDITOR_HOST:$EDITOR_PORT
  post-consume  — apply rules to $DOCUMENT_ID (paperless post-consume hook)
  poller        — long-running poller mode
  apply         — apply rules to one document on demand
  backfill      — apply rules to every document matching a paperless query
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any

from paperless_rules.config import Config
from paperless_rules.engine import load_rules
from paperless_rules.paperless_client import PaperlessClient
from paperless_rules.runtime.apply import (
    ResolutionCache,
    apply_rules_to_document,
)

log = logging.getLogger("paperless_rules.cli")


def _setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _run_editor(cfg: Config) -> int:
    import uvicorn

    from paperless_rules.editor.app import create_app

    uvicorn.run(
        create_app(cfg),
        host=cfg.editor_host,
        port=cfg.editor_port,
    )
    return 0


async def _run_apply(cfg: Config, doc_id: int, *, dry_run: bool) -> int:
    rules = load_rules(cfg.rules_dir)
    async with PaperlessClient(cfg.paperless_url, cfg.paperless_token) as client:
        result = await apply_rules_to_document(
            client, doc_id, rules, dry_run=dry_run
        )
    if result.error:
        log.error("doc %d: %s", doc_id, result.error)
        return 1
    if not result.matched:
        log.info("doc %d: no rule matched", doc_id)
        return 0
    if result.dry_run:
        log.info(
            "doc %d: would apply %s — payload=%s",
            doc_id, result.rule_filename, result.payload,
        )
    else:
        log.info(
            "doc %d: applied %s",
            doc_id, result.rule_filename,
        )
    return 0


async def _run_backfill(cfg: Config, *, query: str, dry_run: bool) -> int:
    rules = load_rules(cfg.rules_dir)
    if not rules:
        log.error("no rules loaded from %s", cfg.rules_dir)
        return 1
    cache = ResolutionCache()
    matched = 0
    unmatched = 0
    errors = 0
    async with PaperlessClient(cfg.paperless_url, cfg.paperless_token) as client:
        async for doc in client.iter_documents(query=query):
            result = await apply_rules_to_document(
                client, doc["id"], rules, dry_run=dry_run, cache=cache
            )
            if result.error:
                errors += 1
                log.warning("doc %d error: %s", doc["id"], result.error)
            elif result.matched:
                matched += 1
            else:
                unmatched += 1
    log.info(
        "backfill complete: matched=%d unmatched=%d errors=%d",
        matched, unmatched, errors,
    )
    return 0 if errors == 0 else 1


async def _run_supervisor(cfg: Config) -> int:
    """Concurrently run the editor + the configured runtime mode in one
    process. Default container CMD — single image, both services."""
    import uvicorn

    from paperless_rules.editor.app import create_app

    tasks: dict[str, asyncio.Task[Any]] = {}

    if cfg.editor_enabled:
        app = create_app(cfg)
        ucfg = uvicorn.Config(
            app, host=cfg.editor_host, port=cfg.editor_port,
            log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        )
        server = uvicorn.Server(ucfg)
        tasks["editor"] = asyncio.create_task(server.serve(), name="editor")
        log.info("supervisor: editor on %s:%d", cfg.editor_host, cfg.editor_port)

    if cfg.runtime_mode == "poller":
        from paperless_rules.runtime import poller as poller_mod
        tasks["poller"] = asyncio.create_task(poller_mod.run(cfg), name="poller")
        log.info("supervisor: poller every %ds", cfg.poll_interval_seconds)
    elif cfg.runtime_mode == "post_consume":
        # post_consume is invoked per-doc by paperless via PAPERLESS_POST_CONSUME_SCRIPT.
        # Nothing for the supervisor to do beyond the editor.
        log.info("supervisor: post_consume mode — invoked per-doc by paperless")
    elif cfg.runtime_mode == "disabled":
        log.info("supervisor: runtime disabled")

    if not tasks:
        log.warning("nothing to run; exiting")
        return 0

    # First task to exit is treated as the lifecycle anchor; cancel the rest.
    done, pending = await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    for t in pending:
        try:
            await t
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    for t in done:
        exc = t.exception()
        if exc is not None:
            log.error("task %s failed: %s", t.get_name(), exc)
            return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="paperless-rules")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("editor", help="start the FastAPI editor")
    sub.add_parser(
        "post-consume",
        help="apply rules to $DOCUMENT_ID (paperless post-consume hook)",
    )
    sub.add_parser("poller", help="long-running poller mode")
    sub.add_parser(
        "supervisor",
        help="run editor + configured runtime concurrently (default container CMD)",
    )
    apply_p = sub.add_parser("apply", help="apply rules to one document on demand")
    apply_p.add_argument("doc_id", type=int)
    apply_p.add_argument("--dry-run", action="store_true")
    bf_p = sub.add_parser(
        "backfill", help="apply rules to every document matching a query"
    )
    bf_p.add_argument(
        "--filter", default="", help="paperless query string (e.g. 'correspondent:Acme')"
    )
    bf_p.add_argument("--dry-run", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    args = _build_parser().parse_args(argv)
    cfg = Config.from_env()

    if args.cmd == "editor":
        return _run_editor(cfg)
    if args.cmd == "post-consume":
        from paperless_rules.runtime import post_consume

        return asyncio.run(post_consume.run(cfg))
    if args.cmd == "poller":
        from paperless_rules.runtime import poller

        return asyncio.run(poller.run(cfg))
    if args.cmd == "supervisor":
        return asyncio.run(_run_supervisor(cfg))
    if args.cmd == "apply":
        return asyncio.run(_run_apply(cfg, args.doc_id, dry_run=args.dry_run))
    if args.cmd == "backfill":
        return asyncio.run(
            _run_backfill(cfg, query=args.filter, dry_run=args.dry_run)
        )
    return 2


if __name__ == "__main__":
    sys.exit(main())
