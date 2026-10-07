# Component compatibility and omissions

## Accepted recovery CLI

The original relative `candidate_tree/candidate/catalyst_paper_delivery` and sibling `catalyst_paper_v2` names are preserved. Ledger source identity still names the exact four source files: `catalyst.py`, `accounting.py`, `ledger.py` and `build_workbook.mjs`. Their source bytes are not renamed or resealed.

The benchmark ledger is replaced only at its documented patch target with the accepted submitted ledger bytes; `SOURCE_FILE_MAP.json` records the original `patched_files/ledger.py` member and its target. The accepted new test is placed at the patch's documented `cli_refusal_tests/test_cli_refusal.py` target. This is the assessed repair composition, not a second patch or new implementation.

`ledger.py` loads its sibling core; `recovery.py` loads that ledger; `entry_bridge.py` loads recovery, ledger and the core. Those source files and `entry_contract.json` are present. Imports were inspected statically, not run. Ledger initialization also reads the original core README; that private-content-bearing file is absent. Recovery test helpers also import the omitted `test_catalyst.py` and need an omitted legacy checkpoint fixture. No initialization or regression-readiness claim is made.

The workbook builder is retained as an exact source-identity dependency, not a workbook. It requires the separately installed `@oai/artifact-tool`; that dependency is not bundled or installed, and rendering was not checked.

## Offline radar and collector handoff

The radar module imports only Python standard-library modules. Its narrow test worker imports `test_radar.py`, which loads the preserved config and cases fixtures and `radar_ops.py`; its replay fixture layout is intact. These are synthetic/described-case fixtures with explicit evidence limitations, not retained private source messages.

The collector handoff adds `baseline/candidate_tree/candidate/catalyst_paper_delivery` to its import path. That older baseline is deliberately not copied. The accepted recovery engine lives in a different component and has not been wired in. The handoff therefore remains source-only until an explicitly reviewed integration change resolves this conflict.

The archive adapter's original packet-generation and manifest pins are unchanged. Its private packet is excluded. The handoff tests require that packet in setup, so they cannot be reproduced using this sanitized package. No synthetic substitution was made under the historical pins.

## Intake template

Only the empty `message_template.json` is included. The trial implementation, source-bound schema, source config, capture template, trial-specific tests, private source URL and embedded archive are omitted together. The placeholder alone does not provide validation.

## Exclusion policy

Excluded: raw or redacted private chats/captures; account balances and account-shaped baseline fixtures/tests; live or historical account checkpoints; journals, logs and process traces; generated workbooks; credentials/cookies; personal goals; source conversation identifiers/URLs; private machine paths; internal storage IDs; duplicate archives and older nested engines. Source archive names and digests are retained solely as component provenance; no private archive is included.

The old ASTRA repository remains a distinct legacy component at its original repository paths. This candidate does not overwrite or claim to replace it. Packaging did not change any source implementation. Privacy omissions are explicit and are not repaired by silently changing source identity, fixture amounts, source pins or contracts.
