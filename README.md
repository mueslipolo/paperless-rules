# paperless-rules

Rule-based document classification and extraction for [paperless-ngx](https://github.com/paperless-ngx/paperless-ngx).

A web editor + runtime that learns the patterns of your recurring senders (Swisscom, CSS, UBS, …) and writes back paperless metadata: correspondent, document type, tags, and custom fields. Regex-first, deterministic, no LLM, no database.

## Why

paperless-ngx has built-in matching rules but each only assigns one piece of metadata. paperless-rules adds **two-stage matching**: a rule identifies a document by keywords, then *because* it matched, a bundle of regexes extract amounts / dates / reference numbers into custom fields. For predictable templates (~70 % of typical household admin), this beats an LLM-based approach on accuracy, speed, cost, and auditability.

---

## Quick start

5-minute path, assuming paperless-ngx is already running.

1. **Mint an API token** in paperless: `Settings → API auth tokens → Create`. Copy it.
2. **Create the directories** that will hold your rules and the poller's state file:
   ```bash
   mkdir -p paperless-rules/{rules,state}
   ```
3. **Add the service** to your existing `docker-compose.yml` (or use [`docker-compose.example.yml`](./docker-compose.example.yml) as a starting point):
   ```yaml
   services:
     paperless-rules:
       image: paperless-rules:latest
       environment:
         PAPERLESS_URL: http://paperless:8000
         PAPERLESS_TOKEN: ${PAPERLESS_RULES_TOKEN}
         RUNTIME_MODE: poller
       volumes:
         - ./paperless-rules/rules:/data/rules
         - ./paperless-rules/state:/data/state
       ports:
         - "127.0.0.1:8765:8765"
       depends_on:
         - paperless
       restart: unless-stopped
   ```
4. **Set the token** in `.env`:
   ```
   PAPERLESS_RULES_TOKEN=<paste from step 1>
   ```
5. **Build + start**:
   ```bash
   docker compose build paperless-rules
   docker compose up -d paperless-rules
   ```
6. **Open the editor** at `http://localhost:8765`. The health pill should say `paperless: connected`.
7. **Author a rule**: click a Swisscom (or any recurring) document, click `bootstrap`, accept the suggested keywords + fields, fill in the regex for `amount` using the live tester, click `save`. Done.

The poller will pick up new documents within `POLL_INTERVAL_SECONDS` (default 60s) and write back metadata. Existing documents can be back-filled with `paperless-rules backfill`.

---

## Installation

### On a Synology NAS (DSM 7 / Container Manager)

paperless-rules is packaged as a single image and is designed to live alongside your existing paperless-ngx containers in the same compose stack.

**1. Place the source.** SSH into the NAS or use File Station. Paperless usually lives in `/volume1/docker/paperless/`. Clone the repo next to it:

```bash
cd /volume1/docker
git clone https://github.com/mueslipolo/paperless-rules.git
```

**2. Build the image.** Container Manager's UI doesn't build from source directly, so use SSH:

```bash
cd /volume1/docker/paperless-rules
sudo docker build -t paperless-rules:latest .
```

(Synology runs Docker as root; `sudo` is normal here.)

**3. Add the service to your paperless compose file.** Edit `/volume1/docker/paperless/docker-compose.yml` and add the snippet from `docker-compose.example.yml`. Adjust volume paths to your Synology layout:

```yaml
  paperless-rules:
    image: paperless-rules:latest
    environment:
      PAPERLESS_URL: http://paperless-webserver:8000   # match your paperless service name
      PAPERLESS_TOKEN: ${PAPERLESS_RULES_TOKEN}
      RUNTIME_MODE: poller
      TZ: Europe/Zurich
    volumes:
      - /volume1/docker/paperless-rules/rules:/data/rules
      - /volume1/docker/paperless-rules/state:/data/state
    ports:
      - "8765:8765"
    depends_on:
      - paperless-webserver
    restart: unless-stopped
```

**4. Mint the API token** in paperless (Settings → API auth tokens), add `PAPERLESS_RULES_TOKEN=<token>` to your paperless `.env`.

**5. Bring it up.** Either via Container Manager → Project → action `Build` and `Start`, or via SSH:

```bash
cd /volume1/docker/paperless
sudo docker compose up -d paperless-rules
```

**6. Open the editor** at `http://<nas-ip>:8765`. Reach it from outside the LAN by adding it to your existing Synology reverse proxy (Control Panel → Login Portal → Reverse Proxy) the same way you proxy paperless itself.

The default `127.0.0.1:8765:8765` binding in the compose example would restrict access to localhost only — on a Synology you usually want LAN access, so use `8765:8765` as shown above.

### Generic Docker Compose

See [`docker-compose.example.yml`](./docker-compose.example.yml) — same shape as the Synology section, but with localhost-only port binding and standard `./` paths.

### Building from source

```bash
git clone https://github.com/mueslipolo/paperless-rules.git
cd paperless-rules
docker build -t paperless-rules:latest .
```

The image is a multi-stage Python 3.12 build, runs as a non-root user (`paperless`, uid 1000), and exposes a `/api/health` endpoint for healthchecks.

---

## How it works

```
┌──────────────────┐              ┌──────────────────┐
│   editor (8765)  │ ◄─── read ── │   paperless      │
│   regex tester   │              │                  │
│   bootstrap      │              │                  │
├──────────────────┤              │                  │
│   runtime        │ ── PATCH ──▶ │                  │
│   poller / hook  │              │                  │
└────────┬─────────┘              └──────────────────┘
         │
         ▼
   ./rules/*.yml
```

- **Editor** — a regex playground with a corpus picker. Type a pattern → live multi-doc highlighting + match counts → coerced-value preview → save the YAML.
- **Runtime** — applies rules to documents and writes metadata back. Tags are additive, manually-set fields aren't overwritten, runs are idempotent.

Two runtime modes:

| `RUNTIME_MODE` | When to use |
|---|---|
| `poller` (default) | Periodically scans paperless for new/changed documents. No paperless config changes needed. Lag = `POLL_INTERVAL_SECONDS`. |
| `post_consume` | Synchronous. Wired to paperless's `PAPERLESS_POST_CONSUME_SCRIPT`. No lag, but requires changes on the paperless side. |
| `disabled` | Editor only — no automatic write-back. |

---

## Writing rules

A rule is a YAML file in `rules/`. Filenames load alphabetically — prefix with `NN_` to control specificity (`01_swisscom_invoice.yml` runs before `99_generic_invoice.yml`). The first rule whose `keywords` match and `required_fields` extract wins.

### Example 1 — Swisscom mobile invoice (FR)

The canonical case: stable issuer name, French invoice template, Swiss number formatting (`1'234.50`).

```yaml
issuer: Swisscom (Suisse) SA
document_type: Invoice
tags: [telecom, mobile, monthly]

keywords:
  - Swisscom
  - Facture

exclude_keywords:
  - Rappel                    # don't match reminders / dunning notices
  - Mahnung

fields:
  amount:
    regex: 'Total à payer\s+CHF\s+([\d''.,]+)'
    type: float               # writes "CHF1234.50" as a monetary custom_field
  date:
    regex: 'Date d''émission\s+(\d{2}\.\d{2}\.\d{4})'
    type: date                # writes "2024-03-15" as a date custom_field
  invoice_number:
    regex:
      - 'Numéro de facture\s+(\d+)'
      - 'No\.?\s*facture\s+(\d+)'        # alternate template
    type: str

required_fields: [amount, date]

options:
  currency: CHF
  date_formats: ['%d.%m.%Y']
  languages: [fr]
```

### Example 2 — CSS health-insurance premium statement (DE)

Different language, different number format, no apostrophe thousand separator.

```yaml
issuer: CSS Versicherung AG
document_type: Insurance premium
tags: [insurance, health, monthly]

keywords:
  - CSS
  - Prämienrechnung

fields:
  amount:
    regex: 'Rechnungsbetrag\s+CHF\s+([\d''.,]+)'
    type: float
  date:
    regex: 'Rechnungsdatum\s+(\d{1,2}\.\s?\w+\s+\d{4})'
    type: date
  due_date:
    regex: 'Fällig am\s+(\d{2}\.\d{2}\.\d{4})'
    type: date
  policy_number:
    regex: 'Polizze[\s.-]+(\d{6,})'
    type: str

required_fields: [amount, due_date, policy_number]

options:
  currency: CHF
  # Engine tries user formats first, then built-in fallbacks
  date_formats:
    - '%d.%m.%Y'
    - '%d. %B %Y'             # "15. März 2024" (locale-dependent)
  languages: [de]
```

### Example 3 — Tarif 590 medical bill (Swiss healthcare)

Demonstrates structural identifiers (GLN provider numbers, AHV patient numbers) — paperless-rules ships dedicated regex helpers for both.

```yaml
issuer: Praxis Dr. Meier
document_type: Medical bill
tags: [health, medical]

keywords:
  - Tarif 590
  - GLN

fields:
  amount:
    regex: 'Total\s+CHF\s+([\d''.,]+)'
    type: float
  date_of_service:
    regex: 'Behandlungsdatum\s+(\d{2}\.\d{2}\.\d{4})'
    type: date
  provider_gln:
    regex: '\b(7601\d{9})\b'           # Swiss healthcare provider ID
    type: str
  patient_ahv:
    regex: '\b(756\.\d{4}\.\d{4}\.\d{2})\b'
    type: str

required_fields: [amount, date_of_service, provider_gln]

options:
  currency: CHF
```

### Example 4 — UBS bank statement (excluding card statements)

Demonstrates `exclude_keywords` to differentiate a generic statement from a card statement that uses similar wording.

```yaml
issuer: UBS Switzerland AG
document_type: Bank statement
tags: [bank, statement, ubs]

keywords:
  - UBS
  - Kontoauszug

exclude_keywords:
  - Kreditkartenabrechnung    # card statement — different rule applies

fields:
  iban:
    regex: '\b(CH\d{2}\s?(?:\d{4}\s?){4}\d{1,2})\b'
    type: str
  period_end:
    regex: 'Saldo per\s+(\d{2}\.\d{2}\.\d{4})'
    type: date
  closing_balance:
    regex: 'Saldo per[^\n]+CHF\s+([\d''.,-]+)'
    type: float

required_fields: [iban, period_end]

options:
  currency: CHF
```

### Example 5 — Generic invoice fallback (low-priority catch-all)

Place this at `99_generic_invoice.yml` so it runs after all issuer-specific rules. It only fires when no specific rule matched, since the runtime stops at the first match.

```yaml
issuer: ''                    # leave empty — paperless won't get a correspondent set
document_type: Invoice
tags: [unmatched-invoice]

keywords:
  - Invoice                   # English variant
exclude_keywords: []

fields:
  amount:
    regex:
      - 'Total\s+(?:CHF|EUR|USD)\s+([\d''.,]+)'
      - 'Amount due\s+(?:CHF|EUR|USD)\s+([\d''.,]+)'
    type: float
  date:
    regex: '\b(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\b'
    type: date

required_fields: [amount]

options:
  currency: CHF
```

### Field-spec syntax

Three equivalent forms for declaring `fields.<name>`:

```yaml
fields:
  amount: 'Total\s+CHF\s+([\d.]+)'         # bare string, type inferred from name
  invoice_number:                          # list of patterns, first match wins
    - 'Facture Nr\.\s*(\d+)'
    - 'Invoice number\s*(\d+)'
  date:                                    # full dict form
    regex: 'du\s+(\d{2}\.\d{2}\.\d{4})'
    type: date
```

Type inference from the field name: substrings `amount`, `total`, `price`, `tva`, `vat`, `tax`, `montant` → `float`; `date`, `due`, `echeance`, `period`, `fällig` → `date`; otherwise `str`.

### Number coercion

`float` fields handle Swiss quirks the OCR engine emits:

```
1'234.50    → 1234.5     (ASCII apostrophe, Swiss canonical)
1’234.50    → 1234.5     (typographic apostrophe)
1ʼ234.50    → 1234.5     (modifier letter apostrophe)
1 234.50    → 1234.5     (NBSP)
89,50       → 89.5       (EU decimal comma)
1.234,50    → 1234.5     (DE thousand-dot, comma-decimal)
```

When both `,` and `.` appear, the rightmost is the decimal separator and the other is stripped as a thousand separator.

`date` fields try the user's `options.date_formats` first, then a built-in fallback list (`%d.%m.%Y`, `%d-%m-%Y`, `%d/%m/%Y`, `%Y-%m-%d`, `%d %B %Y`, …) and emit ISO `YYYY-MM-DD`.

---

## Configuration

Full list in [`.env.example`](./.env.example). The most-used vars:

| Variable | Default | Description |
|---|---|---|
| `PAPERLESS_URL` | (required) | Base URL of paperless (e.g. `http://paperless:8000`) |
| `PAPERLESS_TOKEN` | (required) | API token — Settings → API auth tokens |
| `RULES_DIR` | `/data/rules` | Where rule YAMLs live |
| `STATE_DIR` | `/data/state` | Poller state file |
| `EDITOR_ENABLED` | `true` | Toggle the web editor |
| `EDITOR_PORT` | `8765` | Editor HTTP port |
| `RUNTIME_MODE` | `disabled` | `poller` / `post_consume` / `disabled` |
| `POLL_INTERVAL_SECONDS` | `60` | How often to poll (poller mode only) |
| `POLL_FILTER` | (empty) | Optional paperless query, e.g. `tag:needs-rules` for explicit opt-in |
| `LOG_LEVEL` | `INFO` | Logger level (`DEBUG` / `INFO` / `WARNING`) |

---

## CLI

The image's default `CMD` is `supervisor`, which runs the editor + the configured `RUNTIME_MODE`. Other subcommands:

```
paperless-rules editor              # editor only
paperless-rules poller              # poller only
paperless-rules post-consume        # apply to $DOCUMENT_ID (paperless hook)
paperless-rules apply <doc_id>      # one-off, useful for testing a rule
paperless-rules apply <id> --dry-run
                                    # see the would-be PATCH, no write
paperless-rules backfill --filter "correspondent:Swisscom"
                                    # apply rules to existing matching docs
```

The `apply` and `backfill` commands respect `--dry-run` so you can validate a new rule against your library before letting it loose.

---

## Development

Requires Python 3.11+, [uv](https://github.com/astral-sh/uv), and (optionally) Docker or podman for the e2e tier.

```bash
uv venv
uv pip install -e ".[test]"
pytest                              # unit + API tests, ~0.6s
pytest -m e2e                       # tier-3 e2e — brings up paperless-ngx
```

Repo layout:

```
src/paperless_rules/
  engine.py            rule engine (pure function)
  bootstrap.py         heuristic rule generation
  rules_io.py          YAML load/save with path-traversal protection
  paperless_client.py  async paperless API wrapper
  config.py            env-driven config
  cli.py               argparse dispatcher
  editor/              FastAPI app + single-file SPA
  runtime/             apply / post_consume / poller
tests/
  test_*.py            unit tests
  e2e/                 e2e tests (docker/podman compose)
docker-compose.example.yml   drop-in for production
docker-compose.test.yml      isolated stack for e2e tests
```

---

## What's not in scope

- No LLM integration, fallback, or "smart" inference.
- No database. Flat YAML for rules, small JSON for poller state.
- No user accounts or multi-tenancy. Single-user local tool.
- No anonymization or PII detection.
- No build pipeline — vanilla HTML/CSS/JS in a single file.

---

## License

Apache-2.0. Copyright Yves Räber.
