"""FastAPI editor for paperless-rules. Endpoints under /api/, SPA at /."""

from __future__ import annotations

import re as _re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from paperless_rules.config import Config
from paperless_rules.editor.auth import make_auth_dep
from paperless_rules.engine import coerce_value, extract_with_rule, load_rules
from paperless_rules.paperless_client import PaperlessClient, PaperlessError
from paperless_rules.rules_io import (
    RulesIOError,
    auto_filename,
    delete_rule,
    list_rules,
    read_rule,
    rename_rule,
    reorder_rules,
    write_rule,
)
from paperless_rules.runtime.apply import ResolutionCache, apply_rules_to_document

__APP_VERSION__ = "0.1.0"

_AUTO_PREFIX_RE = _re.compile(r"^(\d{2})_")


def _extract_prefix(filename: str) -> int | None:
    m = _AUTO_PREFIX_RE.match(filename)
    return int(m.group(1)) if m else None


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


class RenameRequest(BaseModel):
    name: str


class ReorderRequest(BaseModel):
    filenames: list[str]


class NewRuleRequest(BaseModel):
    name: str


class PostConsumeRequest(BaseModel):
    doc_id: int


class ApplyRequest(BaseModel):
    doc_ids: list[int] | None = None
    filter: str | None = None
    dry_run: bool = True
    max_docs: int = 500
    overwrite_existing: bool = False


class DiscoverRequest(BaseModel):
    match: str
    exclude: str | None = None
    search: str | None = None
    scan_limit: int = 1000
    max_matches: int = 100


def _derive_prefilter(pattern: str) -> str:
    """Literal alphanumeric tokens from a regex, joined as a paperless
    full-text query. Returns "" when the regex contains alternation since
    AND-joining tokens from different branches would over-narrow.
    """
    if "|" in pattern:
        return ""
    simplified = _re.sub(r"\\.", " ", pattern)
    simplified = _re.sub(r"\[[^\]]*\]", " ", simplified)
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


# ReDoS guards. Python's stdlib `re` has no timeout, so we cap inputs
# instead: pattern length kills the majority of pathological compiles,
# and a 1 MB text cap caps the per-call linear-factor blowup.
_PATTERN_MAX = 4096
_TEXT_MAX = 1_000_000


def _bound_pattern(pattern: str) -> None:
    if len(pattern) > _PATTERN_MAX:
        raise HTTPException(400, f"pattern exceeds {_PATTERN_MAX} chars")


def _bound_text(text: str) -> str:
    return text if len(text) <= _TEXT_MAX else text[:_TEXT_MAX]


def _build_re_flags(flags: str) -> int:
    out = _re.MULTILINE
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
            "start": m.start(),
            "end": m.end(),
            "match": m.group(0),
            "groups": list(m.groups()),
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
    async def lifespan(app: FastAPI):
        if state.paperless is None and cfg.paperless_url and cfg.paperless_token:
            state.paperless = PaperlessClient(
                cfg.paperless_url, cfg.paperless_token, verify=cfg.httpx_verify
            )
            state.owns_client = True
        try:
            yield
        finally:
            if state.owns_client and state.paperless is not None:
                await state.paperless.aclose()
                state.paperless = None

    app = FastAPI(title="paperless-rules", version=__APP_VERSION__, lifespan=lifespan)

    auth_dep = make_auth_dep(state, required=cfg.editor_auth_required)
    auth = [Depends(auth_dep)]

    def require_paperless() -> PaperlessClient:
        if state.paperless is None:
            raise HTTPException(503, "paperless not configured")
        return state.paperless

    def require_writable() -> None:
        if cfg.editor_readonly:
            raise HTTPException(405, "editor is in read-only mode (EDITOR_READONLY=true)")

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
            "auth_required": cfg.editor_auth_required,
            "readonly": cfg.editor_readonly,
        }

    @app.get("/api/documents", dependencies=auth)
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

    @app.get("/api/documents/{doc_id}/text", dependencies=auth)
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

    @app.get("/api/documents/{doc_id}/preview", dependencies=auth)
    async def get_document_preview(doc_id: int) -> Response:
        try:
            data, content_type = await require_paperless().get_preview(doc_id)
        except PaperlessError as e:
            raise HTTPException(502, str(e)) from e
        return Response(content=data, media_type=content_type)

    @app.get("/api/custom_fields", dependencies=auth)
    async def list_custom_fields_endpoint() -> dict[str, Any]:
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

    @app.get("/api/rules", dependencies=auth)
    def list_rules_endpoint() -> dict[str, Any]:
        return {"rules": list_rules(cfg.rules_dir)}

    @app.get("/api/rules/{filename}", dependencies=auth)
    def get_rule_endpoint(filename: str) -> dict[str, Any]:
        try:
            return {"filename": filename, "yaml": read_rule(cfg.rules_dir, filename)}
        except RulesIOError as e:
            raise HTTPException(404, str(e)) from e

    @app.post("/api/rules", dependencies=auth)
    def save_rule_endpoint(req: RuleSaveRequest) -> dict[str, Any]:
        require_writable()
        try:
            write_rule(cfg.rules_dir, req.filename, req.yaml)
        except RulesIOError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "filename": req.filename}

    @app.post("/api/rules/{filename}/rename", dependencies=auth)
    def rename_rule_endpoint(filename: str, req: RenameRequest) -> dict[str, Any]:
        """Rename a rule via display name; preserves the NN_ prefix."""
        require_writable()
        try:
            new_filename = auto_filename(req.name, cfg.rules_dir, prefix=_extract_prefix(filename))
            new_filename = rename_rule(cfg.rules_dir, filename, new_filename)
        except RulesIOError as e:
            raise HTTPException(400, str(e)) from e
        # Best-effort rewrite of the in-file `name:` so slug and label stay aligned.
        try:
            data = yaml.safe_load(read_rule(cfg.rules_dir, new_filename)) or {}
            if isinstance(data, dict):
                data = {"name": req.name, **{k: v for k, v in data.items() if k != "name"}}
                write_rule(
                    cfg.rules_dir,
                    new_filename,
                    yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                )
        except (RulesIOError, yaml.YAMLError):
            pass
        return {"ok": True, "filename": new_filename}

    @app.post("/api/rules/reorder", dependencies=auth)
    def reorder_rules_endpoint(req: ReorderRequest) -> dict[str, Any]:
        require_writable()
        try:
            renamed = reorder_rules(cfg.rules_dir, req.filenames)
        except RulesIOError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "renamed": renamed}

    @app.post("/api/rules/new", dependencies=auth)
    def new_rule_endpoint(req: NewRuleRequest) -> dict[str, Any]:
        """Create a blank rule from a display name; server picks the filename."""
        require_writable()
        filename = auto_filename(req.name, cfg.rules_dir)
        body = yaml.safe_dump(
            {"name": req.name, "match": "", "exclude": "", "fields": {}},
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )
        try:
            write_rule(cfg.rules_dir, filename, body)
        except RulesIOError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "filename": filename, "name": req.name}

    @app.delete("/api/rules/{filename}", dependencies=auth)
    def delete_rule_endpoint(filename: str) -> dict[str, Any]:
        require_writable()
        try:
            removed = delete_rule(cfg.rules_dir, filename)
        except RulesIOError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "removed": removed}

    @app.post("/api/test", dependencies=auth)
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
            results.append(
                {
                    "doc_id": doc_id,
                    "title": doc.get("title", ""),
                    "extraction": extract_with_rule(
                        doc.get("content", "") or "",
                        rule,
                        trace=True,
                    ),
                }
            )
        return {"results": results}

    @app.post("/api/regex/test", dependencies=auth)
    async def test_regex(req: RegexTestRequest) -> dict[str, Any]:
        if not req.doc_ids and req.text is None:
            raise HTTPException(400, "either doc_ids or text must be provided")
        _bound_pattern(req.pattern)
        try:
            compiled = _re.compile(req.pattern, _build_re_flags(req.flags or ""))
        except _re.error as e:
            # Called on every keystroke — return 200 with ok=False so a
            # half-typed pattern shows an inline error, not a crash.
            return {"ok": False, "error": str(e), "results": []}

        results: list[dict[str, Any]] = []
        if req.doc_ids:
            client = require_paperless()
            for doc_id in req.doc_ids:
                try:
                    doc = await client.get_document(doc_id)
                except PaperlessError as e:
                    results.append(
                        {
                            "doc_id": doc_id,
                            "source": "doc",
                            "error": str(e),
                            "match_count": 0,
                            "matches": [],
                        }
                    )
                    continue
                results.append(
                    _run_pattern(
                        compiled,
                        _bound_text(doc.get("content", "") or ""),
                        req.type,
                        req.date_formats,
                        doc_id,
                        "doc",
                    )
                )
        if req.text is not None:
            results.append(
                _run_pattern(
                    compiled,
                    _bound_text(req.text),
                    req.type,
                    req.date_formats,
                    None,
                    "text",
                )
            )
        return {"ok": True, "error": None, "results": results}

    @app.post("/api/discover", dependencies=auth)
    async def discover_endpoint(req: DiscoverRequest) -> dict[str, Any]:
        if not req.match:
            return {"scanned": 0, "matching": [], "truncated_scan": False}
        _bound_pattern(req.match)
        try:
            match_re = _re.compile(req.match, _build_re_flags(""))
        except _re.error as e:
            raise HTTPException(400, f"invalid match regex: {e}") from e
        exclude_re = None
        if req.exclude:
            _bound_pattern(req.exclude)
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
                text = _bound_text(doc.get("content", "") or "")
                m = match_re.search(text)
                if not m:
                    continue
                if exclude_re is not None and exclude_re.search(text):
                    continue
                start = max(0, m.start() - 30)
                end = min(len(text), m.end() + 30)
                snippet = text[start:end].replace("\n", " · ")
                matching.append(
                    {
                        "id": doc.get("id"),
                        "title": doc.get("title", ""),
                        "snippet": snippet,
                        "match_start": m.start() - start,
                        "match_end": m.end() - start,
                        "leading_ellipsis": start > 0,
                        "trailing_ellipsis": end < len(text),
                    }
                )
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

    @app.post("/api/post-consume", dependencies=auth)
    async def post_consume_endpoint(req: PostConsumeRequest) -> dict[str, Any]:
        """Apply rules to one doc; called by paperless's post-consume hook."""
        require_writable()
        rules = load_rules(cfg.rules_dir)
        if not rules:
            return {"doc_id": req.doc_id, "matched": False, "skipped": "no rules loaded"}
        result = await apply_rules_to_document(require_paperless(), req.doc_id, rules)
        return {
            "doc_id": result.doc_id,
            "matched": result.matched,
            "rule_filename": result.rule_filename,
            "payload": result.payload,
            "error": result.error,
            "skipped_fields": result.skipped_fields,
        }

    @app.post("/api/rules/{filename}/apply", dependencies=auth)
    async def apply_rule_endpoint(filename: str, req: ApplyRequest) -> dict[str, Any]:
        """Apply one rule to a doc set (corpus or paperless filter)."""
        # Dry-run is always allowed; only commits respect EDITOR_READONLY.
        if cfg.editor_readonly and not req.dry_run:
            raise HTTPException(405, "editor is in read-only mode (EDITOR_READONLY=true)")
        try:
            rule = yaml.safe_load(read_rule(cfg.rules_dir, filename))
        except RulesIOError as e:
            raise HTTPException(404, str(e)) from e
        except yaml.YAMLError as e:
            raise HTTPException(400, f"invalid YAML: {e}") from e
        if not isinstance(rule, dict):
            raise HTTPException(400, "rule must be a YAML mapping")
        rules = [(filename, rule)]

        client = require_paperless()
        cache = ResolutionCache()
        results: list[dict[str, Any]] = []
        scanned = 0
        truncated = False

        if req.doc_ids:
            doc_ids = list(req.doc_ids)[: req.max_docs]
            truncated = len(req.doc_ids) > req.max_docs
        else:
            doc_ids = []
            try:
                async for doc in client.iter_documents(query=req.filter or "", page_size=50):
                    if len(doc_ids) >= req.max_docs:
                        truncated = True
                        break
                    doc_ids.append(int(doc["id"]))
            except PaperlessError as e:
                raise HTTPException(502, str(e)) from e

        # Per-doc failures are non-fatal — one bad regex shouldn't strand the sweep.
        for doc_id in doc_ids:
            scanned += 1
            try:
                r = await apply_rules_to_document(
                    client,
                    doc_id,
                    rules,
                    overwrite_existing=req.overwrite_existing,
                    dry_run=req.dry_run,
                    cache=cache,
                )
            except Exception as e:
                results.append({"doc_id": doc_id, "error": f"apply failed: {e}"})
                continue
            results.append(
                {
                    "doc_id": r.doc_id,
                    "matched": r.matched,
                    "payload": r.payload,
                    "error": r.error,
                    "dry_run": r.dry_run,
                    "skipped_fields": r.skipped_fields,
                }
            )

        matched = sum(1 for r in results if r.get("matched"))
        written = (
            0 if req.dry_run else sum(1 for r in results if r.get("matched") and not r.get("error"))
        )
        errors = sum(1 for r in results if r.get("error"))
        return {
            "filename": filename,
            "scanned": scanned,
            "matched": matched,
            "written": written,
            "errors": errors,
            "dry_run": req.dry_run,
            "truncated": truncated,
            "results": results,
        }

    # SPA — mount LAST so /api/* routes match first.
    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app
