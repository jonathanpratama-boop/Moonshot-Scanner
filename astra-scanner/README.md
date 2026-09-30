# ASTRA scanner — local US-equities research prototype

A small local application (Python, SQLite, FastAPI, server-rendered HTML) that discovers
research candidates through two independent routes — **announcements** (retained local
JSON and a bounded free SEC EDGAR filings adapter) and **market anomalies** (experimental
detectors over imported 5-minute bars) — and keeps evidence, research, rechecks,
notifications and forward outcomes in an append-only local database.

> **Research only.** Nothing here places orders, confirms holdings or grants entry
> permission. Detectors are unvalidated experiments. Sample data is synthetic and every
> page, notification and export says so. No predictive accuracy, complete market
> coverage, verified notification delivery or continuous operation is claimed.

Start with [`HANDOFF.md`](HANDOFF.md) for status, evidence and next steps.

## Setup (macOS/Linux, Python ≥ 3.11)

```sh
cd astra-scanner
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e . --no-deps
.venv/bin/python -m pytest -q          # 85 tests, ~1.5 minutes, no network
```

No accounts or keys are needed for the demonstration. `requirements-ai.txt` is only for
the optional, disabled-by-default paid AI adapter.

## Run the sample workflow and dashboard

```sh
.venv/bin/astra demo --reset                     # full workflow into var/demo.db (synthetic)
.venv/bin/astra --db var/demo.db status          # text summary
.venv/bin/astra --db var/demo.db serve           # http://127.0.0.1:8765 (read-only, localhost only)
```

The demo runs: sample import → detection → frozen evidence → research import → state
update → dashboard render check → due recheck → outcome record, and fails loudly if any
expected invariant does not hold. Exports land in `var/demo_exports/`, notification
previews in `var/outbox/`.

## Everyday commands

```sh
astra --db DB init --mode sample|replay|live        # mode is fixed per database file
astra --db DB dataset FILE.json                      # provider + dataset definition
astra --db DB instruments FILE.json                  # instrument metadata
astra --db DB universe FILE.json                     # immutable universe version
astra --db DB bars FILE.csv --dataset ID [--as-of T] # incremental bar batch
astra --db DB announcements FILE.json                # retained announcements
astra --db DB sec-collect --issuers config/sec_issuers.example.json   # live/replay DB; needs ASTRA_SEC_USER_AGENT
astra --db DB scan --dataset ID --universe ID [--cutoff T]            # cutoff only in sample/replay
astra --db DB replay --dataset ID --universe ID --start D --end D [--every-minutes 30]
astra --db DB research-import FILE.json [--candidate ID]
astra --db DB process-due                            # urgent work, rechecks, expiries, outcomes, backlog
astra --db DB cycle --dataset ID --universe ID [--sec-issuers FILE]   # doctrine run priority order
astra --db DB candidates | candidate ID | transition ID STATE --reason R --evidence-json J | assign ID OWNER
astra --db DB notify list|deliver|ack --id N         # local preview queue only
astra --db DB import-hourly-alerts FILE.csv --source-name S --coverage-start T --coverage-end T
astra --db DB export candidates|detector-results|outcomes|runs|comparison --out FILE.csv
```

`python -m astra ...` works the same as `astra ...`.

## Documentation

| File | Contents |
|---|---|
| `HANDOFF.md` | status, evidence, limitations, next tasks (start here) |
| `IMPLEMENTATION_STATUS.json` | machine-readable status |
| `docs/IMPORT_FORMATS.md` | every input format (bars, datasets, announcements, research, alerts, SEC issuers) |
| `docs/DETECTORS.md` | detector definitions, completed-bar rules, missing-data behaviour, coverage labels |
| `docs/STATE_MACHINE.md` | candidate states, transitions, blockers, research mapping |
| `docs/OUTCOMES.md` | frozen outcome definition, calendars, statuses, comparison exports |
| `docs/CALENDAR.md` | exchange calendar rules, sources and known conflicts |
| `docs/SCHEMA.md` | database tables and migration procedure |
| `docs/VERIFICATION.md` | commands actually executed and observed results |
| `docs/REVIEW_ROUND1.md` | review findings of 2026-09-30, root causes, fixes and reproduction tests |
| `doctrine/DOCTRINE_REFERENCE.md` | doctrine sources read, hashes, adopted rules, open conflicts |
