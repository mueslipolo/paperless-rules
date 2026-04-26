# paperless-rules

Rule-based document classification and extraction for [paperless-ngx](https://github.com/paperless-ngx/paperless-ngx).

A web editor + runtime that learns the patterns of your recurring senders and writes back paperless metadata: correspondent, document type, tags, and custom fields. Regex-first, deterministic, no LLM, no database.

## Why

paperless-ngx has built-in matching rules but each one only assigns a single piece of metadata. paperless-rules adds **two-stage matching**: one regex (`match:`) decides whether a document is the right kind, then *because* it matched, a bundle of per-field regexes (`fields:`) extract amounts / dates / reference numbers into custom fields. For predictable templates (around 70 % of typical household admin), this beats an LLM-based approach on accuracy, speed, cost, and auditability.

Currency, date formats, language, and matching tags are all configurable per rule — see [Writing rules](#writing-rules).

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
7. **Author a rule**: open one of your recurring documents, click `bootstrap`, accept the suggested keywords + fields, fill in the regex for `amount` using the live tester, click `save`.

The poller picks up new documents within `POLL_INTERVAL_SECONDS` (default 60s) and writes back metadata. Existing documents can be back-filled with `paperless-rules backfill`.

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
      TZ: UTC                                          # or your locale, e.g. Europe/Berlin
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

The image is a multi-stage Python 3.12 build, runs as a non-root user (`paperless`, uid 1000), and exposes `/api/health` for healthchecks.

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

A rule has two phases:

1. **MATCH** — a single regex (`match:`) that decides whether the rule applies to a document. Evaluated with `re.MULTILINE | re.DOTALL` so `.` spans newlines.
2. **EXTRACT** — a set of per-field regexes (`fields:`) that pull metadata once the rule has fired.

Files in `rules/` load alphabetically — prefix with `NN_` to control specificity (`01_acme_invoice.yml` runs before `99_generic_invoice.yml`). The first rule whose `match` succeeds and `required_fields` all extract wins.

### Example 1 — Telecom mobile invoice (French)

The canonical case: stable issuer name, French invoice template, EUR with comma decimal.

```yaml
issuer: Acme Télécom (Europe) SARL
document_type: Invoice
tags: [telecom, mobile, monthly]

# MATCH — single regex; rule fires when this matches the doc
match: 'Acme.*?Facture'         # issuer + doc-type, in any order, any distance
exclude: 'Rappel'               # optional: disqualifies reminders / dunning notices

# FIELDS — per-field regexes that extract paperless metadata
fields:
  amount:
    regex: 'Total à payer\s+EUR\s+([\d ,]+)'
    type: float                 # writes "EUR1234.50" as a monetary custom_field
  date:
    regex: 'Date d''émission\s+(\d{2}\.\d{2}\.\d{4})'
    type: date                  # writes "2024-03-15" as a date custom_field
  invoice_number:
    regex:
      - 'Numéro de facture\s+(\d+)'
      - 'No\.?\s*facture\s+(\d+)' # alternate template
    type: str

required_fields: [amount, date]

options:
  currency: EUR
  date_formats: ['%d.%m.%Y']
  languages: [fr]
```

### Example 2 — Insurance premium statement (German)

Different language, due_date, and a locale-dependent month name (`%B`).

```yaml
issuer: Globex Versicherung GmbH
document_type: Insurance premium
tags: [insurance, health, monthly]

match: 'Globex.*?Prämienrechnung'

fields:
  amount:
    regex: 'Rechnungsbetrag\s+EUR\s+([\d.,]+)'
    type: float
  date:
    regex: 'Rechnungsdatum\s+(\d{1,2}\.\s?\w+\s+\d{4})'
    type: date
  due_date:
    regex: 'Fällig am\s+(\d{2}\.\d{2}\.\d{4})'
    type: date
  policy_number:
    regex: 'Police[\s.\-]+(\d{6,})'
    type: str

required_fields: [amount, due_date, policy_number]

options:
  currency: EUR
  # Engine tries user formats first, then a built-in fallback list
  date_formats:
    - '%d.%m.%Y'
    - '%d. %B %Y'             # "15. März 2024" — locale-dependent
  languages: [de]
```

### Example 3 — Bank statement (with disambiguating exclusion)

Demonstrates `exclude_keywords` to differentiate a regular account statement from a credit-card statement that uses similar wording.

```yaml
issuer: Initech Bank
document_type: Bank statement
tags: [bank, statement, initech]

match: 'Initech Bank.*?Account statement'
exclude: 'Credit card statement'  # different rule applies for those

fields:
  iban:
    regex: '\b([A-Z]{2}\d{2}\s?(?:[A-Z0-9]{4}\s?){3,7}[A-Z0-9]{0,4})\b'
    type: str
  period_end:
    regex: 'Balance as of\s+(\d{4}-\d{2}-\d{2})'
    type: date
  closing_balance:
    regex: 'Closing balance[^\n]+([+\-]?[\d,.]+)'
    type: float

required_fields: [iban, period_end]

options:
  currency: USD
  date_formats: ['%Y-%m-%d']
```

### Example 4 — Generic invoice fallback (multi-currency catch-all)

Place this at `99_generic_invoice.yml` so it runs after issuer-specific rules. The runtime stops at the first match, so this fires only when nothing more specific matched.

```yaml
issuer: ''                    # no specific correspondent
document_type: Invoice
tags: [unmatched-invoice]

match: '\bInvoice\b'

fields:
  amount:
    regex:
      - 'Total\s+(?:CHF|EUR|USD|GBP)\s+([\d''.,]+)'
      - 'Amount due\s+(?:CHF|EUR|USD|GBP)\s+([\d''.,]+)'
      - 'Grand total\s+(?:CHF|EUR|USD|GBP)\s+([\d''.,]+)'
    type: float
  date:
    regex: '\b(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\b'
    type: date

required_fields: [amount]

options:
  currency: EUR
```

### Field-spec syntax

Three equivalent forms for declaring `fields.<name>`:

```yaml
fields:
  amount: 'Total\s+EUR\s+([\d.]+)'         # bare string, type inferred from name
  invoice_number:                          # list of patterns, first match wins
    - 'Invoice\s+number\s*(\d+)'
    - 'Numéro de facture\s*(\d+)'
  date:                                    # full dict form
    regex: 'on\s+(\d{4}-\d{2}-\d{2})'
    type: date
```

Type inference from the field name: substrings `amount`, `total`, `price`, `tva`, `vat`, `tax`, `montant` → `float`; `date`, `due`, `echeance`, `period`, `fällig` → `date`; otherwise `str`.

### Field transforms

Beyond simple capture-group extraction, the dict form supports a set of transforms. **Modes are mutually exclusive** — precedence `match > aggregate > combine > value > default-extract` — and `default`, `pick`, `map` compose with the relevant modes as noted below.

The editor's YAML drawer has a **`+ transform ▼`** dropdown that inserts a starter snippet for any of these.

#### `default` — fallback when nothing matches

Universal modifier. If the field's pattern (or whole transform chain) doesn't produce a value, the constant in `default:` is used instead, and the field counts as successfully extracted. Without it, a missing pattern fails the field and disqualifies the rule when listed in `required_fields`.

```yaml
fields:
  is_paid:
    regex: '\bPAID\b'
    value: yes
    default: no                # nothing matched → "no" (no error)
    type: str
```

#### `match` — multi-arm enumeration

A list of `{regex, value}` alternatives. The first arm whose regex matches wins; its `value` becomes the field. Subsumes the `value` form for the multi-arm case. Composes with `default`.

```yaml
fields:
  status:
    match:
      - { regex: '\bPAID\b',     value: paid }
      - { regex: '\bOVERDUE\b',  value: overdue }
      - { regex: '\bPENDING\b',  value: pending }
    default: unknown
    type: str
```

Real use: invoice status, document classification (refund/invoice/credit-note), payment-method detection.

#### `pick` — choose first / last / Nth match

Modifier on the default-extract mode. Collects all matches of every pattern across the document, sorts by position, and picks one. Composes with `default` and `map`. Default behaviour without `pick` is `first` (cheapest path — short-circuits at the first matching pattern).

```yaml
fields:
  last_payment_date:
    regex: '\b(\d{2}\.\d{2}\.\d{4})\b'
    pick: last                 # latest date in the doc
    type: date

  closing_balance:
    regex: 'EUR\s+([\d.,]+)'
    pick: -1                   # last EUR amount on the page
    type: float
```

`pick` accepts `first`, `last`, or any integer (`0` = first, `-1` = last, `1` = second, …).

#### `map` — lookup table

Applied to the captured value. If the captured string is a key in `map`, the mapped value is used; otherwise the original capture is kept. Composes with default-extract, `pick`, `combine`. Useful for normalising codes to canonical names.

```yaml
fields:
  country:
    regex: 'Origin:\s*(\w+)'
    map:
      DE: Germany
      FR: France
      NL: Netherlands
    type: str
```

If the document says `Origin: ZZ` and `ZZ` isn't in the map, the field is set to `"ZZ"` (no error).

#### `aggregate` — sum / count / min / max across all matches

Runs every pattern, collects every match, applies the operation. Useful for line-item documents where the printed total is OCR-garbled but the items are clean.

```yaml
fields:
  line_item_total:
    regex: 'Item\s+\$([\d.,]+)'
    aggregate: sum             # sum all line-item amounts
    type: float

  num_charges:
    regex: '^Charge'
    aggregate: count           # number of "Charge" lines
    type: float
```

`count` always succeeds (returns 0 with no matches). `sum` / `min` / `max` fail without numeric matches unless `default` is set.

#### `value` — constant on any match (one-arm shorthand for `match`)

A simpler form when you only need a single arm: regex is a trigger, the constant in `value:` becomes the field. Composes with `default`.

```yaml
fields:
  has_warranty:
    regex: '(?i)\bwarranty\b'  # case-insensitive trigger
    value: yes
    default: no
    type: str
```

#### `combine` — concatenate captures from multiple patterns

Runs every pattern and joins the captures with a separator. Partial matches are kept (a missing pattern doesn't fail the field, just contributes nothing). Composes with `default`, `map`.

```yaml
fields:
  full_name:
    regex:
      - 'First name:\s*(\w+)'
      - 'Last name:\s*(\w+)'
    combine: ' '
    type: str
```

#### Composition example

A realistic field that uses several transforms together:

```yaml
fields:
  payment_status:
    match:
      - { regex: '\bPAID\b',     value: paid }
      - { regex: '\bOVERDUE\b',  value: overdue }
      - { regex: '(?i)pending|in process', value: pending }
    default: unknown
    type: str

  total:
    regex: 'Total\s+EUR\s+([\d.,]+)'
    pick: last                  # if the total appears multiple times, take the last one
    default: '0.00'             # don't break the rule if total is missing
    type: float

  origin_country:
    regex: 'Country:\s*(\w{2})'
    map:
      DE: Germany
      FR: France
    type: str
```

All transforms compose with `type` coercion — e.g. `value: '1.0'` with `type: float` writes `1.0` as a numeric custom field.

### Number coercion

`float` fields handle the common thousand-separator and decimal-point variations real OCR emits:

```
89.50       → 89.5      (plain dot decimal)
89,50       → 89.5      (comma decimal)
1'234.50    → 1234.5    (apostrophe thousand sep, dot decimal)
1’234.50    → 1234.5    (typographic apostrophe)
1ʼ234.50    → 1234.5    (modifier letter apostrophe — OCR artefact)
1 234.50    → 1234.5    (NBSP thousand sep)
1.234,50    → 1234.5    (dot-thousand, comma-decimal)
1,234.50    → 1234.5    (comma-thousand, dot-decimal)
```

When both `,` and `.` appear, the rightmost is the decimal separator and the other is stripped as a thousand separator.

`date` fields try the user's `options.date_formats` first, then a built-in fallback list (`%d.%m.%Y`, `%d-%m-%Y`, `%d/%m/%Y`, `%Y-%m-%d`, `%d %B %Y`, …) and emit ISO `YYYY-MM-DD`.

`options.currency` only affects the **monetary** custom-field value sent to paperless (e.g. `EUR1234.50`). The `regex` itself can match any currency token you want — `currency` is just the prefix added when writing back.

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
paperless-rules backfill --filter "correspondent:Acme"
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

## License

Apache-2.0. Copyright Yves Räber.
