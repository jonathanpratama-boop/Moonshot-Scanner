# Candidate states and controls

Candidates are **research records only**. There are no holdings, positions, simulated
trades or orders anywhere in this prototype; a candidate's state never implies a position.

## Transition map

```
(new) ──▶ DETECTED
DETECTED   ──▶ RESEARCHED | BLOCKED | REJECTED | EXPIRED
RESEARCHED ──▶ BLOCKED | REJECTED | EXPIRED
BLOCKED    ──▶ RESEARCHED (only when every blocker is cleared with evidence) | REJECTED | EXPIRED
REJECTED, EXPIRED: terminal
CONDITIONAL_READY, ENTRY_ELIGIBLE: RESERVED — unreachable
```

- Every transition stores actor, non-empty reason, optional reason type
  (`NEW_EVENT`, `PRICE_OR_CONDITION_CHANGE`, `CORRECTED_INPUT`, `NEWLY_FOUND_OLD_INFORMATION`,
  `CHANGED_INFERENCE`, `INITIAL_RESEARCH`, `EXPIRY`, `OPERATOR_DECISION`), a non-empty
  evidence delta, and the snapshot of blockers still open afterwards. `state_transitions`
  is append-only (database trigger).
- Reserved states are refused in code **and** by database triggers (even direct SQL cannot
  set them). No model output, research completeness or operator checkbox grants readiness.
- Terminal candidates accept no further research. New material evidence about the same
  issuer creates a new candidate (announcement: new item; anomaly: a new episode once the
  previous one is terminal). Terminal transitions cancel pending rechecks, expiries and
  research requests and clear the displayed next recheck/expiry (the previous expiry is
  kept in the condition text).
- Optimistic concurrency: each update checks the candidate `version`.

## How research maps to state

| Research content | Resulting state |
|---|---|
| any open blocker after the import | `BLOCKED` |
| no open blockers, `proposed_disposition: reject` (manual/sample only) | `REJECTED` |
| no open blockers otherwise | `RESEARCHED` |

`RESEARCHED` may remain unforecastable; no targets or probabilities are required. Unknown
sections (e.g. dilution) block only the conclusions they list. AI research may propose
`researched` or `blocked` only, may cite only evidence ids or URLs already in the candidate's
saved context, and may never clear blockers (it retrieves no new evidence). Clearing a blocker
requires manual research citing the clearing evidence.

## Blockers and clearing evidence

A blocker has a key, description, the conclusions it blocks, the clearing evidence
required, and an owner (defaults to the candidate owner). A later research record must
either re-list each open blocker or clear it in `cleared_blockers` with the observed
clearing evidence and at least one cited source; otherwise the import is refused.

## Ownership, rechecks and expiry

- Owner defaults to `astra:anomaly-lane (unassigned)` or `astra:disclosure-lane (unassigned)`;
  `astra assign ID OWNER` changes it (logged as an event).
- On detection (sample/live): a research request is queued, and a default expiry is set to
  the close of the 5th US session after detection (`ASTRA_DEFAULT_EXPIRY_SESSIONS`).
- Research sets the recheck (with condition and priority) and expiry; older pending
  rechecks/expiries are cancelled as superseded. A research revision with `recheck: null`
  also cancels the pending recheck job (review round 1). An expiry can be replaced but not
  removed. Missing values are shown as `UNKNOWN` with the reason, never omitted.
- A revision of an announcement with an open candidate queues a research request (state is not
  changed by the revision itself).
- `process-due` handles due work in doctrine order (urgent first). A due recheck records an
  event, queues a research request (the research backlog), clears the displayed next
  recheck ("awaiting research update") and queues a `recheck_due` notification. A due
  expiry transitions an open candidate to `EXPIRED` (skipped if superseded).
- Replay candidates never get rechecks, expiries or notifications.

## Work queue recovery

`work_items` are claimed with a conditional `UPDATE` inside `BEGIN IMMEDIATE`, so two
workers (or processes) cannot claim the same item. Handling and marking done happen in one
transaction; if a worker dies, its lease (`ASTRA_LEASE_SECONDS`, default 300) expires and the
next run reclaims the item, recording `recovered expired lease held by …`. Every claim counts
as an attempt and no item is claimed once `attempts = max_attempts` (default 3): failures are
retried up to the limit and then marked `dead`, and a lease that expires after the last
permitted attempt (a worker interrupted every time) is dead-lettered on the next claim pass
(`lease expired after attempt n/n … not retried`). Dead items are visible on the dashboard.
A worker whose lease was taken over cannot complete the item (`lease_lost`).

## Notifications

Local preview queue only. Kinds: `candidate_detected`, `state_changed`,
`new_detector_evidence`, `announcement_revised`, `recheck_due`, `research_updated`,
`coverage_incomplete`. Each has a unique dedup key, so repeated runs do not re-notify.
Statuses: `queued` → `attempted` (written to the local outbox; `ASTRA_OUTBOX_DIR`) →
`acknowledged` (operator ran `astra notify ack --id … --by …`). The `external` channel
records `not_configured`. Payloads carry the ASTRA-OPS r1.2 decision row (candidate/state,
source publication and public-availability time, first actual observation, quote/reference
role, unresolved dependencies with clearing evidence, owner, next recheck, expiry, delivery
time) — each either a value (UTC + America/New_York + Asia/Jakarta) or
`{"value": "UNKNOWN", "reason": …}`.

## Doctrine constraint preserved for later integration

`doctrine/DOCTRINE_REFERENCE.md` records the confirmed **maximum planned price-stop distance
of 5%** (GD-1.1). It is not used by any code path here because no plan or entry logic
exists; it is not an account loss budget and does not guarantee realised loss.
