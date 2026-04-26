"""FastAPI editor for paperless-rules. Endpoints under /api/, SPA at /."""

from __future__ import annotations

import re as _re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from paperless_rules import bootstrap as bootstrap_module
from paperless_rules.config import Config
from paperless_rules.engine import coerce_value, extract_with_rule
from paperless_rules.paperless_client import PaperlessClient, PaperlessError
from paperless_rules.rules_io import (
    RulesIOError,
    delete_rule,
    list_rules,
    read_rule,
    write_rule,
)

__APP_VERSION__ = "0.1.0"


class RuleSaveRequest(BaseModel):
    filename: str
    yaml: str


class TestRequest(BaseModel):
    yaml: str
    doc_ids: list[int]


class RegexTestRequest(BaseModel):
    pattern: str
    flags: str = ""
    doc_ids: list[int] | None = None
    text: str | None = None
    type: str | None = None
    date_formats: list[str] | None = None


class BootstrapRequest(BaseModel):
    doc_id: int


class DiscoverRequest(BaseModel):
    """Find paperless docs whose content matches a regex.

    Used by the editor to auto-populate the test corpus from the rule's
    `match:` pattern (and optional `exclude:`). Caller can pre-filter via
    paperless full-text search by setting `search`; otherwise we derive
    a literal-token prefilter from the regex so paperless does the heavy
    narrowing before we run the regex.
    """
    match: str
    exclude: str | None = None
    search: str | None = None
    scan_limit: int = 1000      # max docs to fetch+regex-test
    max_matches: int = 100      # max matching docs to return


def _derive_prefilter(pattern: str) -> str:
    """Extract literal alphanumeric tokens (≥3 chars) from a regex so they
    can be used as a paperless full-text query. Conservative: returns ""
    when the regex contains alternation (|) since AND-joining tokens from
    different branches would over-narrow.
    """
    if "|" in pattern:
        return ""
    # Drop escapes (\d, \s, \., …) and char classes ([abc])
    simplified = _re.sub(r"\\.", " ", pattern)
    simplified = _re.sub(r"\[[^\]]*\]", " ", simplified)
    # Drop group prefixes like (?:, (?=, (?!, (?P<name>
    simplified = _re.sub(r"\(\?[a-zA-Z!=:<][^)]*\)", " ", simplified)
    simplified = _re.sub(r"[(){}*+?$^]", " ", simplified)
    seen: set[str] = set()
    out: list[str] = []
    for m in _re.finditer(r"[A-Za-zÀ-ÿ0-9]{3,}", simplified):
        t = m.group(0)
        tl = t.lower()
        if tl in seen:
            continue
        seen.add(tl)
        out.append(t)
        if len(out) >= 5:
            break
    return " ".join(out)


def _build_re_flags(flags: str) -> int:
    out = _re.MULTILINE  # rule semantics require MULTILINE
    if "i" in flags:
        out |= _re.IGNORECASE
    if "s" in flags:
        out |= _re.DOTALL
    if "x" in flags:
        out |= _re.VERBOSE
    return out


def _run_pattern(
    compiled: _re.Pattern[str],
    text: str,
    type_: str | None,
    date_formats: list[str] | None,
    doc_id: int | None,
    source: str,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for m in compiled.finditer(text):
        entry: dict[str, Any] = {
            "start": m.start(), "end": m.end(),
            "match": m.group(0), "groups": list(m.groups()),
        }
        if type_:
            raw = m.group(1) if m.groups() else m.group(0)
            entry["coerced"] = coerce_value(raw, type_, date_formats)
        matches.append(entry)
    out: dict[str, Any] = {"source": source, "match_count": len(matches), "matches": matches}
    if doc_id is not None:
        out["doc_id"] = doc_id
    return out


class _State:
    paperless: PaperlessClient | None = None
    owns_client: bool = False


def create_app(
    config: Config | None = None,
    *,
    paperless_client: PaperlessClient | None = None,
) -> FastAPI:
    cfg = config or Config.from_env()
    state = _State()
    if paperless_client is not None:
        state.paperless = paperless_client

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ARG001
        if state.paperless is None and cfg.paperless_url and cfg.paperless_token:
            state.paperless = PaperlessClient(cfg.paperless_url, cfg.paperless_token, verify=cfg.httpx_verify)
            state.owns_client = True
        try:
            yield
        finally:
            if state.owns_client and state.paperless is not None:
                await state.paperless.aclose()
                state.paperless = None

    app = FastAPI(title="paperless-rules", version=__APP_VERSION__, lifespan=lifespan)

    def require_paperless() -> PaperlessClient:
        if state.paperless is None:
            raise HTTPException(503, "paperless not configured")
        return state.paperless

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        if state.paperless is None:
            ps: dict[str, Any] = {"ok": False, "error": "not configured"}
        else:
            ps = await state.paperless.health()
        return {
            "app": {"name": "paperless-rules", "version": __APP_VERSION__},
            "rules_dir": str(cfg.rules_dir),
            "paperless": ps,
        }

    @app.get("/api/documents")
    async def list_documents_endpoint(
        query: str = Query(""),
        page: int = Query(1, ge=1),
        page_size: int = Query(25, ge=1, le=100),
    ) -> dict[str, Any]:
        try:
            return await require_paperless().list_documents(
                query=query, page=page, page_size=page_size
            )
        except PaperlessError as e:
            raise HTTPException(502, str(e)) from e

    @app.get("/api/documents/{doc_id}/text")
    async def get_document_text(doc_id: int) -> dict[str, Any]:
        try:
            doc = await require_paperless().get_document(doc_id)
        except PaperlessError as e:
            raise HTTPException(404, str(e)) from e
        return {
            "id": doc.get("id", doc_id),
            "title": doc.get("title", ""),
            "created": doc.get("created", ""),
            "content": doc.get("content", "") or "",
        }

    @app.get("/api/documents/{doc_id}/preview")
    async def get_document_preview(doc_id: int) -> Response:
        """Proxy paperless's PDF preview so the editor can embed it without
        exposing the API token to the browser."""
        try:
            data, content_type = await require_paperless().get_preview(doc_id)
        except PaperlessError as e:
            raise HTTPException(502, str(e)) from e
        return Response(content=data, media_type=content_type)

    @app.get("/api/custom_fields")
    async def list_custom_fields_endpoint() -> dict[str, Any]:
        """Return paperless custom fields so the editor can validate that
        rule field names + types align with the live schema.
        """
        try:
            fields = await require_paperless().list_custom_fields()
        except PaperlessError as e:
            raise HTTPException(502, str(e)) from e
        return {
            "fields": [
                {"id": f.get("id"), "name": f.get("name"), "data_type": f.get("data_type")}
                for f in fields
            ]
        }

    @app.get("/api/rules")
    def list_rules_endpoint() -> dict[str, Any]:
        return {"rules": list_rules(cfg.rules_dir)}

    @app.get("/api/rules/{filename}")
    def get_rule_endpoint(filename: str) -> dict[str, Any]:
        try:
            return {"filename": filename, "yaml": read_rule(cfg.rules_dir, filename)}
        except RulesIOError as e:
            raise HTTPException(404, str(e)) from e

    @app.post("/api/rules")
    def save_rule_endpoint(req: RuleSaveRequest) -> dict[str, Any]:
        try:
            write_rule(cfg.rules_dir, req.filename, req.yaml)
        except RulesIOError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "filename": req.filename}

    @app.delete("/api/rules/{filename}")
    def delete_rule_endpoint(filename: str) -> dict[str, Any]:
        try:
            removed = delete_rule(cfg.rules_dir, filename)
        except RulesIOError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "removed": removed}

    @app.post("/api/test")
    async def test_rule(req: TestRequest) -> dict[str, Any]:
        try:
            rule = yaml.safe_load(req.yaml)
        except yaml.YAMLError as e:
            raise HTTPException(400, f"invalid YAML: {e}") from e
        if not isinstance(rule, dict):
            raise HTTPException(400, "rule must be a YAML mapping")

        client = require_paperless()
        results: list[dict[str, Any]] = []
        for doc_id in req.doc_ids:
            try:
                doc = await client.get_document(doc_id)
            except PaperlessError as e:
                results.append({"doc_id": doc_id, "error": str(e)})
                continue
            results.append({
                "doc_id": doc_id,
                "title": doc.get("title", ""),
                "extraction": extract_with_rule(doc.get("content", "") or "", rule),
            })
        return {"results": results}

    @app.post("/api/regex/test")
    async def test_regex(req: RegexTestRequest) -> dict[str, Any]:
        if not req.doc_ids and req.text is None:
            raise HTTPException(400, "either doc_ids or text must be provided")
        try:
            compiled = _re.compile(req.pattern, _build_re_flags(req.flags or ""))
        except _re.error as e:
            # editor calls this on every keystroke — return 200 with ok=False
            # so a half-typed pattern shows an inline error, not a crash
            return {"ok": False, "error": str(e), "results": []}

        results: list[dict[str, Any]] = []
        if req.doc_ids:
            client = require_paperless()
            for doc_id in req.doc_ids:
                try:
                    doc = await client.get_document(doc_id)
                except PaperlessError as e:
                    results.append({
                        "doc_id": doc_id, "source": "doc", "error": str(e),
                        "match_count": 0, "matches": [],
                    })
                    continue
                results.append(_run_pattern(
                    compiled, doc.get("content", "") or "",
                    req.type, req.date_formats, doc_id, "doc",
                ))
        if req.text is not None:
            results.append(_run_pattern(
                compiled, req.text, req.type, req.date_formats, None, "text",
            ))
        return {"ok": True, "error": None, "results": results}

    @app.post("/api/discover")
    async def discover_endpoint(req: DiscoverRequest) -> dict[str, Any]:
        if not req.match:
            return {"scanned": 0, "matching": [], "truncated_scan": False}
        try:
            match_re = _re.compile(req.match, _build_re_flags(""))
        except _re.error as e:
            raise HTTPException(400, f"invalid match regex: {e}") from e
        exclude_re = None
        if req.exclude:
            try:
                exclude_re = _re.compile(req.exclude, _build_re_flags(""))
            except _re.error as e:
                raise HTTPException(400, f"invalid exclude regex: {e}") from e

        prefilter = req.search if req.search else _derive_prefilter(req.match)
        client = require_paperless()
        matching: list[dict[str, Any]] = []
        scanned = 0
        try:
            async for doc in client.iter_documents(query=prefilter, page_size=50):
                if scanned >= req.scan_limit:
                    break
                scanned += 1
                text = doc.get("content", "") or ""
                m = match_re.search(text)
                if not m:
                    continue
                if exclude_re is not None and exclude_re.search(text):
                    continue
                start = max(0, m.start() - 30)
                end = min(len(text), m.end() + 30)
                snippet = text[start:end].replace("\n", " · ")
                matching.append({
                    "id": doc.get("id"),
                    "title": doc.get("title", ""),
                    "snippet": snippet,
                    "match_start": m.start() - start,
                    "match_end": m.end() - start,
                    "leading_ellipsis": start > 0,
                    "trailing_ellipsis": end < len(text),
                })
                if len(matching) >= req.max_matches:
                    break
        except PaperlessError as e:
            raise HTTPException(502, str(e)) from e
        return {
            "scanned": scanned,
            "matching": matching,
            "truncated_scan": scanned >= req.scan_limit,
            "prefilter": prefilter,
            "prefilter_auto": not req.search and bool(prefilter),
        }

    @app.post("/api/bootstrap")
    async def bootstrap_endpoint(req: BootstrapRequest) -> dict[str, Any]:
        try:
            doc = await require_paperless().get_document(req.doc_id)
        except PaperlessError as e:
            raise HTTPException(404, str(e)) from e
        return bootstrap_module.bootstrap_from_text(doc.get("content", "") or "")

    # SPA — mount LAST so /api/* routes match first.
    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app
