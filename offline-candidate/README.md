# Moonshot offline source candidate

This is one sanitized source-and-test consolidation candidate, assembled on 7 October 2026. It preserves selected existing source bytes. It is not a combined runnable scanner, a release, a deployment, or an operational trading system.

## Start here

- The offline radar implementation and its replay fixtures are in `radar_handoff/`. It consumes supplied fixtures; it does not fetch market data, run the original ASTRA detectors, or send live alerts.
- The accepted raw-CLI refusal repair is the sole `ledger.py` in `recovery_cli/`. The surrounding core and recovery modules come from its benchmark baseline. No old nested ledger is copied into the radar tree.
- `intake_template/` contains only a generic placeholder message envelope. The source-bound trial implementation is deliberately absent.
- Read `STATUS.md` and `COMPATIBILITY.md` before trying any command. `TEST_INVENTORY.json` lists the included test methods and their dependency limits.
- `VERSION_MAP.json` identifies the component versions. `SOURCE_FILE_MAP.json` gives every copied file's original archive member and exact digest. `UPLOAD_INVENTORY.txt` is the complete proposed file list; `MANIFEST.sha256` hashes all members except itself.

## Offline radar use

The narrow radar replay subtree retains its original internal layout. In a disposable local environment with Python 3.11 or later, enter:

`radar_handoff/Moonshot_Collector_Research_Handoff_2026-10-06/candidate/Moonshot_Radar_Operational_Candidate_2026-10-06`

The existing standalone replay command is:

`python3 -I -B candidate/radar_ops.py fixtures/demo_replay.json`

Its existing narrow test commands are:

`python3 -I -B tests/run_worker.py /absolute/path/outside-the-package/radar-normal.json`

`python3 -I -B -O tests/run_worker.py /absolute/path/outside-the-package/radar-optimized.json`

The output parent must already exist. Review source before execution. These commands were not run while making this package. Historical results below describe the original archived version, not a new run of this sanitized copy. Do not use the old aggregate runners against this reduced tree.

## Preserve the existing repository

This folder is intended as an additive `offline-candidate/` subtree on a separate branch of the existing Moonshot-Scanner repository. Do not replace its root README, old ASTRA code, configuration, or history. The previously observed legacy branch was `claude/loving-gauss-1o1ldm`, short commit `def376e`; that reference must be rechecked before any upload.

No license is newly granted by this packaging operation. Existing repository licensing and source ownership still apply. No credentials, real account records, source conversation URLs, raw private captures, checkpoints, journals, or generated workbooks belong in this folder.
