"""Apply rules to paperless documents: match → resolve metadata → PATCH.

Reads built-in metadata (`correspondent`, `document_type`, `tags`, `title`)
from reserved field names in the rule's `fields:` block. Anything else
(non-internal) becomes a paperless custom field of the same name.

Behaviour pinned by tests:
- Tags are additive (manual tags survive a rule run).
- Correspondent / document_type / title are not overwritten unless
  overwrite_existing=True.
- Custom-field create failures are non-fatal.
- `internal: true` fields are skipped from the PATCH entirely.
- Idempotent: a second run on an already-processed doc emits zero PATCHes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from paperless_rules.engine import RESERVED_FIELDS, Rule, find_matching_rule
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
    """Paperless 2.x monetary format: '<CCY><amount>' with no space."""
    if field_result["type"] == "float":
        ccy = (rule.get("options") or {}).get("currency") or "EUR"
        return f"{ccy}{field_result['value']:.2f}"
    return str(field_result["value"])


def _ok_field(extraction: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Returns the field result dict if the field is present, ok, and not internal."""
    f = (extraction.get("fields") or {}).get(name)
    if f and f.get("ok") and not f.get("internal"):
        return f
    return None


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

    # ── built-in metadata from reserved field names ───────────────────

    corr_field = _ok_field(extraction, "correspondent")
    if corr_field and (overwrite_existing or not doc.get("correspondent")):
        cid = await _resolve(client, "correspondents", str(corr_field["value"]), cache.correspondents)
        if cid is not None:
            payload["correspondent"] = cid

    dt_field = _ok_field(extraction, "document_type")
    if dt_field and (overwrite_existing or not doc.get("document_type")):
        dtid = await _resolve(client, "document_types", str(dt_field["value"]), cache.document_types)
        if dtid is not None:
            payload["document_type"] = dtid

    tags_field = _ok_field(extraction, "tags")
    if tags_field:
        rule_tag_names = (
            tags_field["value"] if isinstance(tags_field["value"], list)
            else [str(tags_field["value"])]
        )
        existing_tag_ids = list(doc.get("tags") or [])
        new_ids: list[int] = []
        for t in rule_tag_names:
            tid = await _resolve(client, "tags", str(t), cache.tags)
            if tid is not None:
                new_ids.append(tid)
        merged = sorted(set(existing_tag_ids) | set(new_ids))
        if merged != sorted(existing_tag_ids):
            payload["tags"] = merged

    title_field = _ok_field(extraction, "title")
    if title_field and (overwrite_existing or not doc.get("title")):
        payload["title"] = str(title_field["value"])

    # `created` is paperless's document date (the field labelled "Date" in
    # the UI). Engine writes it as ISO YYYY-MM-DD via _coerce_date; paperless
    # accepts that for the `created` API key.
    created_field = _ok_field(extraction, "created")
    if created_field and (overwrite_existing or not doc.get("created")):
        payload["created"] = str(created_field["value"])

    # ── custom fields: every other non-reserved, non-internal, ok field ──

    cf_writes: list[dict[str, Any]] = []
    for fname, fres in (extraction.get("fields") or {}).items():
        if fname in RESERVED_FIELDS:
            continue
        if not fres.get("ok") or fres.get("internal"):
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
