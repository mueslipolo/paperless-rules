"""Runtime: apply rules to paperless documents.

Three entry points:
  - post_consume.run() — invoked by paperless via PAPERLESS_POST_CONSUME_SCRIPT
  - poller.run()       — long-running periodic scan
  - apply.apply_rules_to_document() — pure-async core, used by both above
                                      and by the `apply` / `backfill` CLIs

The split separates the *invocation* (env vars, signal handling) from the
*work* (matching, resolving, PATCHing). The work is testable without Docker
or paperless via the FakePaperless used in test_apply.py.
"""

from paperless_rules.runtime.apply import (
    ApplyResult,
    ResolutionCache,
    apply_rules_to_document,
)

__all__ = ["ApplyResult", "ResolutionCache", "apply_rules_to_document"]
