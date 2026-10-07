# Status and evidence limits

## Verified in this packaging pass

- The five source archive SHA-256 values match the supplied saved inputs.
- Every included source, test and fixture is copied byte-for-byte from the selected archive member. The accepted repair supplies the only ledger implementation.
- Python files were parsed with the standard-library AST parser without importing or executing them. JSON files were parsed as data.
- Internal relative layout and import references were statically inspected. Known missing dependencies are listed below.
- The final ZIP has a complete file inventory and a per-file SHA-256 manifest. The source tree is unchanged by packaging.

No source code or test suite was executed in this packaging pass. There is no new runtime pass, CI result, deployment, live scan or performance claim.

## Historical results retained as summaries

The saved 7 October CLI repair assessment recorded, in both normal and optimized Python 3.12.14:
- Core 31/31, delivery 10/10 and persistence 12/12.
- Original recovery suite 26/27: the remaining assertion expects the deliberately removed raw-CLI write success. It was not edited or reported as passing.
- Seven submitted refusal tests 7/7.
- Four post-submission supplemental checks 4/4. These were not held out.

The accepted ledger SHA-256 is `af4e1af77632ef82a9b4947d7a7119053e270f32d328b9dd6b864f4d5ebe7de8`. The unmodified recovery module SHA-256 is `048749aab675505eff3fe19e7f07cdec701ce88a071a71713117d8755daeadac`.

The saved 6 October radar candidate recorded 25/25 narrow radar tests in each Python mode. The separate collector handoff recorded 23/23 in each mode against its original complete archived tree and its older nested ledger. Those results do not certify compatibility with the accepted ledger in this package. No source-truth or original-detector equivalence claim follows from them.

## Incomplete or blocked

1. The combined collector handoff cannot import its original nested entry bridge because the nested baseline is omitted. Repointing it to the accepted engine would be a separate code change and compatibility test, not packaging.
2. Recovery initialization and the full recovery tests are incomplete here: the original protocol README, legacy checkpoint fixture and selected account-shaped baseline tests were excluded for privacy. They were not replaced or resealed. Included tests retain their original missing dependencies.
3. The intake trial implementation and schema bind to a private source identity and are omitted; this package does not implement the trial.
4. Private historical evidence is omitted. The pinned archive adapter cannot replay it from this package, and its pins were not changed.
5. Original R3 execution coverage is still missing because the original `r3_scenarios.py` helper is unavailable. No replacement was invented.
6. The frozen 99-case evaluator was lost and not recovered or rerun. There is no frozen 99-case score.
7. The original ASTRA executable/configuration equivalence, live market collection, real alerts, source authenticity, full integration, workbook rendering/recalculation, deployment and real-account migration remain unverified.
8. The unchanged export behavior can write a requested output inside a chain directory and thereby interfere with later verification. This is an existing hazard, not a solved issue.

This package is a reviewable consolidation candidate, not an all-green release. Test counts describe overlapping bounded suites, not unique or exhaustive coverage.
