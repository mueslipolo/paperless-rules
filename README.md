# paperless-rules

Rule-based document classification and metadata extraction for [paperless-ngx](https://github.com/paperless-ngx/paperless-ngx). YAML rules + a browser editor + a small runtime that PATCHes paperless. Regex-only, deterministic, no LLM, no database.

Image: `ghcr.io/mueslipolo/paperless-rules:latest` (multi-arch: `amd64` + `arm64`, ~75 MB Alpine).

---

## What it does

- Reads paperless documents (OCR text), runs each user-written rule's `match:` regex against them, and on a hit extracts `fields:` (each one a regex/value/template) into paperless metadata: `correspondent`, `document_type`, `tags`, `title`, `created` (the document date), and any number of custom fields.
- Surfaces every step in a browser editor (`http://<host>:8765`):
  - **Discovery**: type a `match:` regex → all matching paperless docs become the test corpus, with snippets.
  - **Test / dry-run**: extraction outcome per doc with the would-be PATCH payload, before anything is written.
  - **Backfill**: one button per rule applies it to the current corpus or to every paperless doc the rule matches (capped, dry-run by default).
  - **OCR or PDF view**: toggle to see layout when the OCR text alone isn't telling you enough.
  - **Live validation against your paperless schema**: per-field badge shows whether the field exists, type-matches, or would be created.
- Three orthogonal triggers for the runtime — pick the one that suits you:
  - **post-consume webhook** — paperless calls a curl wrapper after each consumed doc; instant.
  - **backfill** — manual sweep from the UI or CLI; on demand.
  - **poller** — periodic background scan; safety net for deployments that can't wire post-consume.

---

## Install

### Synology NAS (DSM 7 / Container Manager)

The image is published to GHCR for `linux/amd64` and `linux/arm64`. No on-NAS build.

1. **Make the public image accessible** to your NAS once: visit https://github.com/users/mueslipolo/packages/container/paperless-rules/settings → Visibility → **Public**. (Or `docker login ghcr.io` with a personal access token if you prefer to keep it private.)
2. **Mint a paperless API token**: Settings → API auth tokens → Create. Save it as `PAPERLESS_RULES_TOKEN` in the same `.env` your paperless compose reads.
3. **Pre-create the volumes** with the same UID:GID paperless writes as (typically `1035:100` on Synology):
   ```bash
   sudo mkdir -p /volume1/docker/paperless-rules/{rules,state}
   sudo chown -R 1035:100 /volume1/docker/paperless-rules
   ```
4. **Add the service** to your existing paperless `docker-compose.yml` — see [`docker-compose.example.yml`](./docker-compose.example.yml). For Synology with a DSM reverse proxy:
   ```yaml
   paperless-rules:
     container_name: paperless-rules
     image: ghcr.io/mueslipolo/paperless-rules:latest
     restart: unless-stopped
     user: "1035:100"
     networks:
       - paperless_net                                 # same network as paperless
     depends_on:
       - paperless
     ports:
       - "127.0.0.1:8765:8765"                         # DSM reverse proxy fronts it
     volumes:
       - /volume1/docker/paperless-rules/rules:/data/rules
       - /volume1/docker/paperless-rules/state:/data/state
     environment:
       PAPERLESS_URL: http://paperless:8000            # docker-network DNS
       PAPERLESS_TOKEN: ${PAPERLESS_RULES_TOKEN}
       RUNTIME_MODE: disabled                          # see "Modes" below
       EDITOR_AUTH_REQUIRED: "true"
       TZ: Europe/Berlin
   ```
5. **Start it**: Container Manager → Project → Update — or via SSH:
   ```bash
   sudo docker compose up -d paperless-rules
   ```

### Updating

```bash
sudo docker compose pull paperless-rules && sudo docker compose up -d paperless-rules
```
Or DSM Container Manager → Project → **Update**. Releases are published on `vX.Y.Z` git tags; you can pin to a specific tag (`ghcr.io/mueslipolo/paperless-rules:0.1.0`) instead of `latest`.

### Generic Docker Compose

See [`docker-compose.example.yml`](./docker-compose.example.yml) — the same shape, with localhost-only port binding and standard `./` paths.

### Building from source

```bash
git clone https://github.com/mueslipolo/paperless-rules.git
cd paperless-rules
docker build -t paperless-rules:local .
```

The image is single-stage Alpine, runs as `paperless` (uid 1000), exposes `/api/health` for healthchecks.

---

## Behind DSM reverse proxy with HTTPS (recommended for any non-LAN access)

1. Bind the container to localhost only (`127.0.0.1:8765:8765` as in the snippet above).
2. *Control Panel → Login Portal → Advanced → Reverse Proxy → Create*:
   - Source: HTTPS · `rules.your-syno.lan` · 443
   - Destination: HTTP · `localhost` · 8765
   - Custom header → WebSocket: enabled
   - HSTS, HTTP/2: enabled
3. Issue a TLS cert for `rules.your-syno.lan` (*Control Panel → Security → Certificate*).
4. Visit `https://rules.your-syno.lan` — paste your paperless API token in the login modal, you're in.

---

## Authentication

The editor uses **paperless's own API token as the login credential** — no separate password. On first load it shows a login modal; the token is verified against paperless and stored in your browser's `localStorage` only. Revoking the token in paperless logs you out within ~60 s.

| env | default | meaning |
|---|---|---|
| `EDITOR_AUTH_REQUIRED` | `true` | Off only on a strictly trusted LAN. |
| `EDITOR_READONLY` | `false` | When true, every mutation (rule writes, deletes, post-consume, non-dry-run apply) returns 405. Useful for laptop dev mode. |

Two presets:

- **`.env.home.example`** — Synology / production, auth on, write-enabled.
- **`.env.dev.example`** — laptop, auth off, read-only, server-side `PAPERLESS_TOKEN` from env.

---

## Modes

```
RUNTIME_MODE = disabled | poller | post_consume
```

| mode | catches new docs | catches re-OCR / late edits | requires paperless config | continuous load |
|---|---|---|---|---|
| `disabled` (recommended) | post-consume webhook (instant) | editor *Backfill* button | yes — `PAPERLESS_POST_CONSUME_SCRIPT` | none |
| `poller` | next poll (≤ `POLL_INTERVAL_SECONDS`) | next poll | no | one paperless scan / minute, 24/7 |
| `post_consume` | post-consume only | nothing | yes | none |

The poller hot-reloads rules via mtime — toggle `enabled: false` on a rule in the editor and the runtime picks it up on the next iteration without a container restart.

### Wiring post-consume

paperless invokes `PAPERLESS_POST_CONSUME_SCRIPT` per consumed doc. Use [`scripts/post_consume_via_rules.sh`](./scripts/post_consume_via_rules.sh) — a curl wrapper that POSTs to paperless-rules' webhook. Mount it into the paperless container and:

```diff
   webserver:
     environment:
-      PAPERLESS_POST_CONSUME_SCRIPT: /usr/src/paperless/scripts/your_old_script.py
+      PAPERLESS_POST_CONSUME_SCRIPT: /usr/src/paperless/scripts/post_consume_via_rules.sh
+      PAPERLESS_RULES_URL: http://paperless-rules:8765
+      PAPERLESS_RULES_TOKEN: ${PAPERLESS_RULES_TOKEN}
```

The wrapper exits 0 on call failure so a paperless-rules outage never breaks paperless's consume pipeline.

---

## Writing rules

A rule is a YAML file in `RULES_DIR`. The editor names files behind a display label (e.g. `Acme Telecom invoice` → `01_acme_telecom_invoice.yml`), but the file format is plain — you can drop hand-edited YAML into the directory and the editor picks it up. Files are loaded in filename order; `NN_` prefix governs evaluation priority (drag-to-reorder in the UI auto-renumbers).

### Top-level keys

```yaml
name: 'Acme Telecom invoice'           # optional; display label, derived from filename if absent
enabled: true                          # default; set false to park without renaming
match: 'Acme Télécom.*?Facture'        # required; rule fires when this matches the OCR text
exclude: 'Rappel'                      # optional; disqualifies the rule when this matches
fields:                                # one entry per metadata to extract
  …
required: [amount, date]               # field names whose `ok` gates the rule firing
trace: false                           # optional; set true for per-rule diagnostic logs
options:
  currency: EUR                        # prefix used when writing monetary custom fields
  date_formats: ['%d.%m.%Y']           # extra strptime patterns the engine tries
  languages: [fr]                      # locale hints (extension point)
```

`match`/`exclude` regexes run with `re.MULTILINE | re.DOTALL`. Make them specific — `'Invoice'` is too generic; `'Acme Corp.*?Invoice'` anchors to a particular template.

### Reserved field names

These map to paperless built-ins instead of custom fields:

| name | paperless field |
|---|---|
| `correspondent` | `correspondent` (FK; created if missing) |
| `document_type` | `document_type` (FK; created if missing) |
| `tags` | `tags` (M2M; merged additively) |
| `title` | `title` (string) |
| `created` | `created` (the document date — paperless's UI calls this "Date") |

Anything else becomes a custom field of the same name. The editor's name input has an autocomplete combo of every existing paperless custom field, with a per-field badge showing **✓ exists & types match** / **⚠ type mismatch** / **✗ not in paperless · will be created**.

### Field shapes

Each entry under `fields:` is one of three shapes; pick `re` / `val` / `tpl` (or `adv` for the YAML escape hatch) in the editor's kind toggle.

**`regex:`** — capture from the document. Composes with every transform.
```yaml
amount: { regex: 'Total\s+EUR\s+([\d.,]+)', type: float }
```

**`value:`** — fixed assignment. Lists pass through (used for `tags`). Paired with `regex:`, becomes "constant on match" — the regex is the trigger.
```yaml
document_type: { value: Invoice }
tags:          { value: [invoice, monthly] }
is_paid:       { regex: '\bPAID\b', value: yes, default: no, type: str }
```

**`template:`** — string with `{name}` placeholders that resolve against other fields. Templates can reference templates; cycles are detected and surfaced in the editor.
```yaml
title:    { template: '{date} Acme #{invoice_number} EUR{amount}' }
filename: { template: '{date}_acme_{invoice_number}' }
```

A field can be marked **`internal: true`** — extracted/computed but not written to paperless. Useful for fragments that only feed into a template.

Types are explicit: `str` (default), `float`, `date`, `int`, `bool`. The editor's type select drives this; the engine doesn't infer from the field name.

### Transforms

Modes are mutually exclusive — precedence `match > aggregate > combine > value > default-extract`. `default`, `pick`, `map` compose with the relevant modes as noted. The editor's `+ transform ▼` dropdown inserts starter snippets for any of these into an `adv` field.

#### `default` — fallback when nothing matches

If the field's pattern doesn't produce a value, `default:` is used and the field counts as ok.
```yaml
is_paid: { regex: '\bPAID\b', value: yes, default: no, type: str }
```

#### `match` — multi-arm enumeration

A list of `{regex, value}` alternatives. First arm whose regex matches wins.
```yaml
status:
  match:
    - { regex: '\bPAID\b',     value: paid }
    - { regex: '\bOVERDUE\b',  value: overdue }
    - { regex: '\bPENDING\b',  value: pending }
  default: unknown
```

#### `pick` — first / last / Nth match

Sorts every match by position and picks one. `first` (default), `last`, or any int (`0`, `-1`, `1`, …).
```yaml
last_payment_date:
  regex: '\b(\d{2}\.\d{2}\.\d{4})\b'
  pick: last
  type: date
```

#### `map` — lookup table on the captured value

Composes with default-extract, `pick`, `combine`. Captures not in the map pass through unchanged.
```yaml
country:
  regex: 'Origin:\s*(\w+)'
  map:
    DE: Germany
    FR: France
```

#### `aggregate` — sum / count / min / max

Useful for line-item docs.
```yaml
line_item_total: { regex: 'Item\s+\$([\d.,]+)', aggregate: sum, type: float }
num_charges:     { regex: '^Charge',             aggregate: count, type: int }
```

#### `combine` — concat captures from multiple patterns

```yaml
full_name:
  regex:
    - 'First name:\s*(\w+)'
    - 'Last name:\s*(\w+)'
  combine: ' '
```

### Type coercion

`float` handles thousand-separator and decimal-point variations real OCR emits:

```
89.50      → 89.5      1'234.50   → 1234.5    1.234,50  → 1234.5
89,50      → 89.5      1’234.50   → 1234.5    1,234.50  → 1234.5
1 234.50   → 1234.5    (NBSP)
```
When both `,` and `.` appear, the rightmost is the decimal separator; the other is stripped.

`date` tries `options.date_formats` first, then a built-in fallback list (`%d.%m.%Y`, `%d-%m-%Y`, `%d/%m/%Y`, `%Y-%m-%d`, `%d %b %Y`, `%d %B %Y`, `%d-%b-%Y`, `%d-%B-%Y`, …). Always emits ISO `YYYY-MM-DD`.

`options.currency` prefixes the **monetary** custom-field value sent to paperless (`EUR1234.50`). The regex itself can match any token — `currency` is just the prefix on writeback.

### Per-rule diagnostic trace

Set `trace: true` at the top of a rule and the engine emits per-step lines (match hit/miss, exclude fire, per-field outcome) via the `paperless_rules.trace` logger, AND attaches them to `/api/test`'s response so the editor's Test button shows them inline. Off by default; opt in when you need to figure out why a rule isn't firing.

---

## Examples

### Telecom invoice from a specific sender

```yaml
name: 'Acme Telecom invoice'
match: 'Acme Télécom.*?Facture mensuelle'
exclude: 'Rappel'

fields:
  correspondent: { value: 'Acme Télécom (Europe) SARL' }
  document_type: { value: Invoice }
  tags:          { value: [invoice, telecom, monthly] }

  amount:         { regex: 'Total à payer\s+EUR\s+([\d ,]+)', type: float }
  created:        { regex: 'Date d''émission\s+(\d{2}\.\d{2}\.\d{4})', type: date }
  invoice_number: { regex: 'Numéro de facture\s+(\d+)' }

  title: { template: '{created} Acme #{invoice_number} — €{amount}' }

required: [amount, created]
options:
  currency: EUR
  date_formats: ['%d.%m.%Y']
```

### Generic insurance premium (multiple senders)

Correspondent extracted from the doc, not hard-coded.

```yaml
name: 'Insurance premium'
match: 'Premium statement|Prämienrechnung'
exclude: 'Reminder|Mahnung'

fields:
  document_type: { value: 'Insurance premium' }
  tags:          { value: [insurance, premium] }

  correspondent: { regex: '^(.+?(?:GmbH|AG|Inc|Ltd))\s*$' }
  amount:        { regex: 'Premium\s+EUR\s+([\d.,]+)', type: float }
  due_date:      { regex: 'Due\s+(\d{2}\.\d{2}\.\d{4})', type: date }
  policy_number: { regex: 'Policy\s+([A-Z0-9-]+)' }

  title: { template: '{due_date} {correspondent} premium €{amount}' }

required: [correspondent, amount, due_date]
```

### Composing transforms

```yaml
fields:
  payment_status:
    match:
      - { regex: '\bPAID\b',                value: paid }
      - { regex: '\bOVERDUE\b',             value: overdue }
      - { regex: '(?i)pending|in process',  value: pending }
    default: unknown

  total:
    regex: 'Total\s+EUR\s+([\d.,]+)'
    pick: last
    default: '0.00'
    type: float

  origin_country:
    regex: 'Country:\s*(\w{2})'
    map: { DE: Germany, FR: France }
```

---

## Backfilling

Apply a rule to existing documents (paperless-rules only writes new metadata going forward; backfill is how you catch up).

### From the editor

Step 2 (Extraction) → `↻ backfill` button on the SELECTION card. Modal lets you pick:
- **scope**: the current corpus (the docs the discovery returned), or every paperless doc matching the rule (capped at 500/click, paginated).
- **dry run**: on by default; shows would-be PATCH payloads per doc without writing.

After a clean dry-run with matches and no errors, the modal swaps `Apply` for a red `Apply for real (N docs)` button with a 2.5 s safety delay. Per-doc rows show verdict (`dry` / `patched` / `no match` / `error`) and the payload.

### From the CLI

```bash
docker exec paperless-rules paperless-rules backfill                    # everything
docker exec paperless-rules paperless-rules backfill --filter 'tag:invoice'
docker exec paperless-rules paperless-rules backfill --dry-run          # preview
docker exec paperless-rules paperless-rules apply <doc_id>              # one doc
```

---

## Configuration

Full list in [`.env.example`](./.env.example). Most-used:

| Variable | Default | Description |
|---|---|---|
| `PAPERLESS_URL` | (required) | Base URL of paperless |
| `PAPERLESS_TOKEN` | (required) | API token |
| `PAPERLESS_VERIFY_SSL` | `true` | Set false for self-signed LAN paperless |
| `PAPERLESS_CA_BUNDLE` | (empty) | CA bundle path (overrides `VERIFY_SSL`) |
| `RULES_DIR` | `/data/rules` | Where rule YAMLs live |
| `STATE_DIR` | `/data/state` | Poller state file |
| `EDITOR_ENABLED` | `true` | Toggle the web editor |
| `EDITOR_HOST` | `0.0.0.0` | Bind address |
| `EDITOR_PORT` | `8765` | HTTP port |
| `EDITOR_AUTH_REQUIRED` | `true` | Gate `/api/*` behind paperless token |
| `EDITOR_READONLY` | `false` | Block every mutation (laptop dev mode) |
| `RUNTIME_MODE` | `disabled` | `disabled` / `poller` / `post_consume` |
| `POLL_INTERVAL_SECONDS` | `60` | Poll cadence (poller mode only) |
| `POLL_FILTER` | (empty) | Optional paperless query, e.g. `tag:needs-rules` |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

---

## CLI

The image's default `CMD` is `supervisor` (editor + the configured `RUNTIME_MODE`). Other subcommands:

```
paperless-rules editor              # editor only
paperless-rules poller              # long-running poller
paperless-rules post-consume        # apply to $DOCUMENT_ID (paperless hook)
paperless-rules apply <doc_id>      # one-off
paperless-rules apply <id> --dry-run
paperless-rules backfill --filter 'tag:invoice'
paperless-rules backfill --dry-run
```

`apply` and `backfill` respect `--dry-run`: the engine returns the would-be PATCH payload without writing.

---

## API

Auth-gated `/api/*` routes (token via `Authorization: Token …`). `/api/health` is open.

| route | what it does |
|---|---|
| `GET /api/health` | server + paperless connectivity + auth/readonly flags |
| `GET /api/documents?query=` | paperless search proxy |
| `GET /api/documents/{id}/text` | OCR text |
| `GET /api/documents/{id}/preview` | PDF (proxied; token never reaches the browser) |
| `GET /api/custom_fields` | paperless custom-field schema for editor validation |
| `GET /api/rules` | rule list (filename, name, match, field_count, enabled) |
| `GET /api/rules/{f}` | raw YAML |
| `POST /api/rules` | save |
| `DELETE /api/rules/{f}` | delete |
| `POST /api/rules/new` | create from a display name (server picks the filename) |
| `POST /api/rules/{f}/rename` | rename via display name (preserves NN_ prefix) |
| `POST /api/rules/reorder` | drag-reorder; renumbers NN_ prefixes |
| `POST /api/test` | test rule against doc_ids; returns full extraction with trace |
| `POST /api/regex/test` | test a single regex |
| `POST /api/discover` | find paperless docs whose content matches a regex |
| `POST /api/rules/{f}/apply` | backfill (dry-run by default) |
| `POST /api/post-consume` | apply rules to a single doc — called by `scripts/post_consume_via_rules.sh` |

---

## Architecture

```
┌──────────────────┐              ┌──────────────────┐
│   editor (8765)  │ ◄─── read ── │   paperless      │
│   regex tester   │              │                  │
│   discover/test  │              │                  │
│   backfill (UI)  │              │                  │
├──────────────────┤              │                  │
│   runtime        │ ── PATCH ──▶ │                  │
│   poller / hook  │              │                  │
└────────┬─────────┘              └──────────────────┘
         │
         ▼
   /data/rules/*.yml
```

- **Editor** — FastAPI + a single-file SPA; serves `/api/*` and the editor at `/`.
- **Runtime** — applies rules and PATCHes paperless. Tags are additive; manually-set fields aren't overwritten unless `--overwrite-existing` is passed; runs are idempotent.

---

## Development

Requires Python 3.11+, [uv](https://github.com/astral-sh/uv), and (optionally) podman/docker for the e2e tier.

```bash
uv venv
uv pip install -e ".[test]"
uv run pytest tests/ --ignore=tests/e2e -q     # unit + API tests
uv run pytest tests/e2e                         # tier-3 e2e (brings up paperless-ngx)
uv run paperless-rules editor                   # local editor against remote paperless
```

A laptop dev preset (read-only, no auth, env-var token) lives in [`.env.dev.example`](./.env.dev.example).

---

## Repo layout

```
src/paperless_rules/
  engine.py            rule engine (pure function)
  rules_io.py          YAML load/save with path-traversal protection
  paperless_client.py  async paperless API wrapper
  config.py            env-driven config
  cli.py               argparse dispatcher
  editor/
    app.py             FastAPI app
    auth.py            paperless-token verification dep
    static/index.html  single-file SPA
  runtime/
    apply.py           apply_rules_to_document — used by all three triggers
    poller.py          long-running scan loop with mtime-cached reload
    post_consume.py    one-shot via paperless's PAPERLESS_POST_CONSUME_SCRIPT
tests/
  test_*.py            unit + API tests
  e2e/                 tier-3 (docker/podman compose)
scripts/
  post_consume_via_rules.sh   curl wrapper for paperless's post-consume hook
.github/workflows/publish.yml CI: GHCR multi-arch image on every v* tag
```

---

## License

Apache-2.0. Copyright Yves Räber.
