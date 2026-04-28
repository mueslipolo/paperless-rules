"""extract_with_rule's trace: returns per-rule diagnostics + emits via the
paperless_rules.trace logger when opted in.

The logger output is a behavioural concern (people grep their container
logs); we assert via caplog so a refactor that drops the log path will fail.
"""

from __future__ import annotations

import logging

from paperless_rules.engine import extract_with_rule

RULE = {
    "match": "Acme",
    "fields": {
        "amount": {"regex": r"EUR\s+([\d ,]+)", "type": "float"},
        "ref": {"regex": r"Ref\s+(\d+)", "type": "str"},
    },
}

DOC = "Acme Corp\nRef 4521\nTotal EUR 1234,50\n"


def test_no_trace_by_default():
    r = extract_with_rule(DOC, RULE)
    assert "trace" not in r


def test_explicit_trace_true_attaches_lines():
    r = extract_with_rule(DOC, RULE, trace=True)
    assert isinstance(r.get("trace"), list)
    assert any("match" in ln.lower() for ln in r["trace"])
    assert any("amount" in ln for ln in r["trace"])
    assert any("MATCHED" in ln for ln in r["trace"])


def test_rule_trace_true_opts_in():
    rule = dict(RULE, trace=True)
    r = extract_with_rule(DOC, rule)
    assert "trace" in r
    assert any("ref" in ln.lower() for ln in r["trace"])


def test_trace_emits_to_dedicated_logger(caplog):
    caplog.set_level(logging.INFO, logger="paperless_rules.trace")
    extract_with_rule(DOC, RULE, trace=True)
    msgs = [rec.message for rec in caplog.records if rec.name == "paperless_rules.trace"]
    assert any("MATCHED" in m for m in msgs)
    assert any("amount" in m for m in msgs)


def test_no_trace_means_silent_logger(caplog):
    caplog.set_level(logging.INFO, logger="paperless_rules.trace")
    extract_with_rule(DOC, RULE)  # default trace=False
    msgs = [rec.message for rec in caplog.records if rec.name == "paperless_rules.trace"]
    assert msgs == []


def test_trace_records_exclude_fire():
    rule = dict(RULE, exclude="Acme", trace=True)
    r = extract_with_rule(DOC, rule)
    joined = "\n".join(r["trace"])
    assert "FIRED" in joined
    assert r["matched"] is False


def test_trace_records_field_failure():
    rule = {
        "match": "Acme",
        "fields": {"amount": {"regex": r"never_matches_(\d+)", "type": "float"}},
        "trace": True,
    }
    r = extract_with_rule(DOC, rule)
    fail_lines = [ln for ln in r["trace"] if "amount" in ln and "FAIL" in ln]
    assert fail_lines, r["trace"]
