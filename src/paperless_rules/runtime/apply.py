"""Apply rules to paperless documents: match → resolve metadata → PATCH.

Behaviour pinned by tests:
- Tags are additive (manually-applied tags survive a rule run).
- Correspondent / document_type aren't overwritten unless overwrite_existing=True.
- Custom-field create failures are non-fatal (logged + skipped, rest proceeds).
- Idempotent: a second run on an already-processed doc emits zero PATCHes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from paperless_rules.engine import Rule, find_matching_rule
from paperless_rules.paperless_client import PaperlessClient, PaperlessError

log = logging.getLogger(__name__)

_TYPE_MAP = {"float": "monetary", "date": "date", "str": "string"}


@dataclass
class ResolutionCache:
    correspondents: dict[str, int] = field(default_factory=dict)
    document_types: dict[str, int] = field(default_factory=dict)
    tags: dict[str, int] = field(default_factory=dict)
    custom_fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    custom_fields_loaded: bool = False


@dataclass
class ApplyResult:
    doc_id: int
    matched: bool = False
    rule_filename: str | None = None
    payload: dict[str, Any] | None = None
    error: str | None = None
    dry_run: bool = False
    skipped_fields: list[str] = field(default_factory=list)


async def _resolve(
    client: PaperlessClient, kind: str, name: str, cache: dict[str, int]
) -> int | None:
    if name in cache:
        return cache[name]
    try:
        existing = await client.find_one_by_name(kind, name)
        if existing is not None:
            cache[name] = existing["id"]
            return existing["id"]
        created = await client.create(kind, {"name": name})
    except PaperlessError as e:
        log.warning("paperless %s/%s: %s", kind, name, e)
        return None
    cache[name] = created["id"]
    return created["id"]


async def _resolve_cf(
    client: PaperlessClient, name: str, data_type: str, cache: ResolutionCache
) -> dict[str, Any] | None:
    if not cache.custom_fields_loaded:
        try:
            for r in await client.list_custom_fields():
                cache.custom_fields[r["name"]] = r
        except PaperlessError as e:
            log.warning("list custom fields: %s", e)
        cache.custom_fields_loaded = True
    if name in cache.custom_fields:
        return cache.custom_fields[name]
    try:
        created = await client.create("custom_fields", {"name": name, "data_type": data_type})
    except PaperlessError as e:
        log.warning("create custom_field %r (%s): %s", name, data_type, e)
        return None
    cache.custom_fields[name] = created
    return created


def _format_cf_value(field_result: dict[str, Any], rule: Rule) -> Any:
    # Paperless 2.x monetary format: "<CCY><amount>" with no space.
    if field_result["type"] == "float":
        ccy = (rule.get("options") or {}).get("currency") or "CHF"
        return f"{ccy}{field_result['value']:.2f}"
    return str(field_result["value"])


async def apply_rules_to_document(
    client: PaperlessClient,
    doc_id: int,
    rules: list[tuple[str, Rule]],
    *,
    overwrite_existing: bool = False,
    dry_run: bool = False,
    cache: ResolutionCache | None = None,
) -> ApplyResult:
    cache = cache or ResolutionCache()

    try:
        doc = await client.get_document(doc_id)
    except PaperlessError as e:
        return ApplyResult(doc_id=doc_id, error=str(e))

    match = find_matching_rule(doc.get("content", "") or "", rules)
    if match is None:
        return ApplyResult(doc_id=doc_id, matched=False)

    rule_filename, extraction = match
    rule = next(r for fn, r in rules if fn == rule_filename)
    payload: dict[str, Any] = {}
    skipped: list[str] = []

    issuer = rule.get("issuer")
    if issuer and (overwrite_existing or not doc.get("correspondent")):
        cid = await _resolve(client, "correspondents", issuer, cache.correspondents)
        if cid is not None:
            payload["correspondent"] = cid

    doctype = rule.get("document_type")
    if doctype and (overwrite_existing or not doc.get("document_type")):
        dtid = await _resolve(client, "document_types", doctype, cache.document_types)
        if dtid is not None:
            payload["document_type"] = dtid

    rule_tags = rule.get("tags") or []
    if rule_tags:
        existing = list(doc.get("tags") or [])
        new_ids: list[int] = []
        for t in rule_tags:
            tid = await _resolve(client, "tags", t, cache.tags)
            if tid is not None:
                new_ids.append(tid)
        merged = sorted(set(existing) | set(new_ids))
        if merged != sorted(existing):
            payload["tags"] = merged

    cf_writes: list[dict[str, Any]] = []
    for fname, fres in (extraction.get("fields") or {}).items():
        if not fres.get("ok"):
            continue
        cf = await _resolve_cf(client, fname, _TYPE_MAP.get(fres["type"], "string"), cache)
        if cf is None:
            skipped.append(fname)
            continue
        cf_writes.append({"field": cf["id"], "value": _format_cf_value(fres, rule)})

    if cf_writes:
        existing_cf = list(doc.get("custom_fields") or [])
        by_id = {c["field"]: c for c in existing_cf if c.get("field") is not None}
        for w in cf_writes:
            if w["field"] not in by_id or overwrite_existing:
                by_id[w["field"]] = w
        merged_cf = list(by_id.values())
        if merged_cf != existing_cf:
            payload["custom_fields"] = merged_cf

    if not payload:
        return ApplyResult(
            doc_id=doc_id, matched=True, rule_filename=rule_filename,
            skipped_fields=skipped,
        )

    if dry_run:
        return ApplyResult(
            doc_id=doc_id, matched=True, rule_filename=rule_filename,
            payload=payload, dry_run=True, skipped_fields=skipped,
        )

    try:
        await client.patch_document(doc_id, payload)
    except PaperlessError as e:
        return ApplyResult(
            doc_id=doc_id, matched=True, rule_filename=rule_filename,
            payload=payload, error=str(e), skipped_fields=skipped,
        )
    return ApplyResult(
        doc_id=doc_id, matched=True, rule_filename=rule_filename,
        payload=payload, skipped_fields=skipped,
    )
