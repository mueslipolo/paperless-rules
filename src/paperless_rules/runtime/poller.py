"""Long-running poller: scan paperless, apply rules to changed docs.

State (`<state_dir>/poller.json`) maps `doc_id → modified_iso`. Unchanged
docs short-circuit. Survives transient errors; SIGTERM/SIGINT for clean exit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from paperless_rules.config import Config
from paperless_rules.engine import load_rules, rules_dir_signature
from paperless_rules.paperless_client import PaperlessClient
from paperless_rules.runtime.apply import (
    ResolutionCache,
    apply_rules_to_document,
)

log = logging.getLogger("paperless_rules.poller")

# Defensive cap on the state dict — paperless installs of any plausible size
# stay well under this, but the bound prevents pathological growth from
# upstream bugs or misconfigured retention.
_STATE_MAX = 100_000


def _load_state(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("poller state file %s unreadable; starting fresh", path)
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _save_state(path: Path, state: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(path)


async def _poll_once(
    client: PaperlessClient,
    rules: list[tuple[str, dict[str, Any]]],
    state: dict[str, str],
    state_path: Path,
    cache: ResolutionCache,
    poll_filter: str,
) -> int:
    processed = 0
    async for doc in client.iter_documents(query=poll_filter):
        doc_id = doc["id"]
        modified = str(doc.get("modified") or "")
        if state.get(str(doc_id)) == modified:
            continue
        result = await apply_rules_to_document(client, doc_id, rules, cache=cache)
        state[str(doc_id)] = modified
        if len(state) > _STATE_MAX:
            # Evict an arbitrary entry — dict insertion order makes this
            # roughly oldest-first, which is the right call for a state
            # bound that should almost never trigger.
            state.pop(next(iter(state)))
        processed += 1
        if result.error:
            log.error("doc %d: %s", doc_id, result.error)
        elif result.matched and result.payload:
            log.info("doc %d: applied %s", doc_id, result.rule_filename)
    _save_state(state_path, state)
    return processed


async def run(config: Config | None = None, *, max_iterations: int | None = None) -> int:
    cfg = config or Config.from_env()
    if not cfg.paperless_url or not cfg.paperless_token:
        log.error("PAPERLESS_URL / PAPERLESS_TOKEN not configured")
        return 1

    state_path = cfg.state_dir / "poller.json"
    state = _load_state(state_path)
    rules = load_rules(cfg.rules_dir)
    rules_sig = rules_dir_signature(cfg.rules_dir)
    if not rules:
        log.warning("no rules in %s; poller will idle", cfg.rules_dir)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    iterations = 0
    async with PaperlessClient(
        cfg.paperless_url, cfg.paperless_token, verify=cfg.httpx_verify
    ) as client:
        cache = ResolutionCache()
        while not stop.is_set():
            # Hot-reload rules when any *.yml mtime changes.
            current_sig = rules_dir_signature(cfg.rules_dir)
            if current_sig != rules_sig:
                rules = load_rules(cfg.rules_dir)
                rules_sig = current_sig
                log.info("poller: reloaded %d rule(s) from %s", len(rules), cfg.rules_dir)
            try:
                n = await _poll_once(client, rules, state, state_path, cache, cfg.poll_filter)
                if n:
                    log.info("poller: processed %d doc(s)", n)
            except Exception:
                log.exception("poller iteration failed; will retry")
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=cfg.poll_interval_seconds)
            except TimeoutError:
                pass
    return 0


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return asyncio.run(run())
