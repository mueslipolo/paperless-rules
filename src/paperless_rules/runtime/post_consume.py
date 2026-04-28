"""Post-consume entry. Always exits 0 — never fail paperless's pipeline."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from paperless_rules.config import Config
from paperless_rules.engine import load_rules
from paperless_rules.paperless_client import PaperlessClient
from paperless_rules.runtime.apply import apply_rules_to_document

log = logging.getLogger("paperless_rules.post_consume")


async def run(config: Config | None = None, doc_id: int | None = None) -> int:
    cfg = config or Config.from_env()
    if doc_id is None:
        raw = os.environ.get("DOCUMENT_ID", "").strip()
        if not raw:
            log.error("DOCUMENT_ID not set")
            return 0
        try:
            doc_id = int(raw)
        except ValueError:
            log.error("DOCUMENT_ID=%r is not an integer", raw)
            return 0

    if not cfg.paperless_url or not cfg.paperless_token:
        log.error("PAPERLESS_URL / PAPERLESS_TOKEN not configured")
        return 0

    rules = load_rules(cfg.rules_dir)
    if not rules:
        log.info("no rules in %s", cfg.rules_dir)
        return 0

    async with PaperlessClient(
        cfg.paperless_url, cfg.paperless_token, verify=cfg.httpx_verify
    ) as client:
        result = await apply_rules_to_document(client, doc_id, rules)

    if result.error:
        log.error("doc %d: %s", doc_id, result.error)
    elif result.matched and result.payload:
        log.info("doc %d: applied %s", doc_id, result.rule_filename)
    elif result.matched:
        log.info("doc %d: matched %s, no changes", doc_id, result.rule_filename)
    else:
        log.debug("doc %d: no rule matched", doc_id)
    if result.skipped_fields:
        log.warning("doc %d: skipped %s", doc_id, result.skipped_fields)
    return 0


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return asyncio.run(run())
