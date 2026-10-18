# 2026.10.18.6 — idle-command normalization correction

**Accepted for native SW2025 part targeting.** The unchanged `.6` runtime passed
the two-client/two-part trial, full offline suite and final upgrade verification.
See [RELEASE_VALIDATION.md](../RELEASE_VALIDATION.md) for counts and limitations.
The source-freeze narrative below records the correction and the earlier state
of the acceptance gate; it is preserved as history, not a pending current result.

The first native `.5` switch was rejected before activation: SW2025 wrote its
command ID and `IsUiActive=False` but left the PropertyManager title untouched.
Build `.6` treats that title as descriptive, accepting string/null while still
requiring a written integer command ID and explicit false activity flag. It does
not hardcode -3 as an idle ID. Unknown/active state and malformed output remain
fail-closed; geometry is not dispatched after rejected command state.

The qualification harness now retains the underlying error when MCP task-group
cleanup wraps it in ExceptionGroup. The failed `.5` run and diagnostics remain
historical evidence, not a native pass. Native `.6` acceptance is a separate gate
against the exact release manifest; this source snapshot does not certify it.
Offline `.6` target/owner/routing checks passed 115/115; the complete queue suite
passed 224 with one existing Windows-job capability skip (225 collected). Both
runs recorded zero native attempts and stable Python source hashes. This is not
a new complete inherited-suite or native acceptance result.
See [selection contract](DOCUMENT_TARGET.md) and [validation](QUEUE_VALIDATION.md).

# Historical 2026.10.18.5 — queued selection of an already open document

Build `.5` adds optional `coordination.target_document` inside the same exclusive
CAD operation as preflight, plan execution, verification and save. Requests for
different models are admitted concurrently but executed strictly sequentially.
`expected_document` remains compare-only; supplying both is rejected.

Actual switching requires unambiguous already open saved models, clean current
and target state, and known safe edit state. Finish edits, verify and explicitly
save before yielding to another model. Same-model sketch/dirty continuation is
left to normal preflight, but an assembly must always be editing itself. Selection
never opens, saves, closes, rebuilds or changes configurations. Uncertain
activation fences the queue; ordinary failed preconditions reject only that job.
See [the complete targeting contract](DOCUMENT_TARGET.md).

Native qualification clients can pin `SOLIDWORKS_MCP_EXPECTED_OWNER_ID` to the
verified starting broker owner. The pin disables broker auto-start, rejects
owner changes and leaves ordinary unpinned client behavior unchanged. See
[qualification pinning](QUEUE_OPERATIONS.md#pin-qualification-clients-to-one-broker-owner).

Offline selection tests passed 47/47; owner-pin tests passed 18/18. The final
queue regression passed 213 with one existing host-capability skip (214 collected). These include synthetic COM and
two independent clients, not native CAD geometry/save-reopen proof. Native `.5`
acceptance must be retained separately against the exact immutable release
manifest before production use. Installation for that qualification is permitted
in an authorized idle maintenance window. Native assembly/drawing targeting is
not certified. Historical `.4` acceptance below remains a separate result.

# Historical 2026.10.18.4 — SW2025 queue and process-scan correction

Base: existing 6.5.31-local.1, commit `8338e62` (prior local geometry,
sketch, viewport, save and rollback corrections retained).

This is an independent local fork build. The earlier `6.6.0-queue.1` designation
was a provisional local candidate, not an upstream release.

## Corrective process guard in build 2026.10.18.4

Candidate `.4` addresses a passively reproduced `.3` defect: a Windows CIM
snapshot can include an already exited Python process with an unavailable command
line, which the earlier guard treated as a live unknown legacy client. The probe
retained exact process-generation and signaled-handle evidence without contacting
CAD or the queue. It proves the defect but does not identify the unrecorded
offenders in earlier failed qualification or cleanup attempts.

The correction checks kernel process generation and liveness, uses bounded fresh
scans for stale observations, and retains private bounded diagnostics. Live
unknown processes, inaccessible state and unresolved observation errors still
fail closed. No process is killed or accepted merely because a grace period has
elapsed. Existing uncertain CAD jobs still require independent explicit recovery.
Guard-failure records are separate from the CAD journal: at most 16 retained
files, 64 KiB per file and 12 projected process rows with truncation metadata.
No raw command lines or environment values are retained. Diagnostic write or
pruning failure blocks admission instead of silently losing the evidence.
**Runtime acceptance for `.4` completed on 2026-10-18** on SW2025 API 33.2.0.
Two-process native geometry, save/reopen, synthetic cleanup, two MCP stdio
sessions and current-chat reconnect passed. The same broker and CAD generations
survived the client-application restart. Final guarded regression: 423 passed,
1 explicit host-capability skip; separate broker tests: 9 passed. Earlier test
TEMP cleanup errors remain unexplained and retained. See
[validation and limits](QUEUE_VALIDATION.md).

## Correction after the first native attempt

Build `2026.10.18.1` was selected in the MCP client configuration and activated
against the existing SW2025 process. Its native qualification failed while
creating the disposable seed geometry, before the two-process phase. The failed
result remains recorded. The recovery-required gate blocked later CAD work;
explicit acknowledgement followed verified native return and UI readiness.
The disposable part was saved and closed after that reconciliation.

The corrected qualification uses a fully defined profile with verified sketch
exit and typed extrusion, retains complete failure/job diagnostics privately,
and checks the installed build through the MCP initialization handshake.
Build `2026.10.18.2` subsequently passed native qualification on SW2025 revision
33.2.0: two independent client processes each applied three 1 mm increments,
changing depth from 20 to 26 mm. One body, 20 x 20 x 26 mm bounds and 10,400 mm3
volume were verified, with FIFO order, no whole-operation overlap, and matching
geometry after save/close/reopen. No user documents were initially open; the
document/file preservation checks passed for that empty initial set. The
synthetic part was then saved and closed, and the idle broker drained. This
successful later run does not convert the first failed attempt into a pass.

## Independent broker startup in build 2026.10.18.3

Native auto-start delegates to the existing Desktop Explorer after checking its
executable, desktop-window handle and owning process generation, user SID and
session. The child verifies its immediate parent, and a private launch receipt
binds the build and process identity. Physical state-directory and journal file
IDs are pinned across MSIX path virtualization so the new broker cannot silently
open another queue at a similarly spelled path. Identity or receipt mismatches
fail closed. See [QUEUE_OPERATIONS.md](QUEUE_OPERATIONS.md) for the launch contract.

The broker may retain an OS desktop Job Object. The required independence is
from the exact MCP frontend SDK Job Object. The synthetic production-route test
`tests/qualify_desktop_launch.py` passed: the real frontend process was verified
inside that SDK job, its forced closure and exit were confirmed, and the same
broker generation survived outside that job. Original journal rows and events
remained unchanged. The virtual-environment helper also passed pywin32 and
Pillow import checks. Those imports are dependency smoke evidence, not CAD or
raster/vector acceptance.

The subsequent `.3` native attempt 05 passed 16 jobs, including six increments
from two independent client processes, verified final geometry and matching
save/close/reopen results. Two actual MCP stdio sessions confirmed `.3` through
initialization, shared one broker and completed serialized native reads. That
broker survived both session closures and a host-application restart.

Final synthetic-part cleanup then failed at the legacy-process guard, first
before admission and later in an admitted operation that left the queue fenced.
The returned native result permits inspection; it does not make that job a pass
or remove the recovery requirement. Earlier `.3` attempts and these failures
remain retained. Attempt 04's worker admissions and the later cleanup failures
did not retain offending process identities; their exact offenders remain unknown.
The successful geometry/stdio tests therefore do not establish completed cleanup
or unconditional permanent-operation readiness. See [QUEUE_VALIDATION.md](QUEUE_VALIDATION.md).

## Runtime architecture

- One dispatcher per Windows user/desktop session, independent of release path.
- One COM-owning STA main thread. MCP transports and status polling never read CAD.
- Authenticated localhost JSON transport, bounded messages/connections, no pickle.
- SQLite WAL/FULL journal with FIFO sequence, global request-ID deduplication,
  immutable execution identity, bounded admission, terminal retention tombstones.
- OS ownership mutex stays held while a CAD call is blocked. Timeout records an
  uncertain outcome without cancelling COM, releasing ownership or replaying it.
- Restart preserves queued jobs and fences interrupted jobs. Recovery requires
  exact job IDs, an operator note, and independently verified execution return or
  death of the previous CAD process generation before attempting native probes.
  A durable pre-COM marker also proves when a claimed job never reached native
  code. It is replaced before attachment; a result's unsupported claim is not proof.
  Recovery checks the target window without COM before initializing a new backend.
- Queue state and endpoints are private to the current user/SYSTEM and live on a
  local unsynchronized disk; desktop sessions have separate state directories.

## Deliberate interface changes

`coordination` supports `request_id`, `expected_document`, `trusted_python`,
`queue_timeout_sec`, `execution_timeout_sec`, and `wait_timeout_sec`.
Queue/execution deadlines must be positive. Response wait may be zero; a response
wait timeout returns a job ID and does not cancel the operation. Keep the same
explicit request ID when retrying after losing a response.

New controls: `get_queue_status`, `get_queue_request`, `cancel_queued_job`, `queue_events`,
`acknowledge_queue_recovery`. Existing `list_jobs`/`get_job_result` now refer to the
shared persistent journal. Only waiting jobs may be cancelled.

All results are structured JSON text. `execute_python_async` submits the same
single-threaded execution path and returns a durable job ID; it no longer creates
an independent COM worker. Raw Python is disabled without explicit trusted opt-in.
Its static checks are guardrails, not a security sandbox: dynamically concealed
thread/process creation, independent COM access and direct filesystem changes
are outside the supported synchronous-script contract.

The queue observes UI blockers but never confirms dialogs or retries mutations
automatically. Requests using the previous `ui_guard.auto_resolve_known` or
`recovery.auto_recover` policies receive an explicit unsupported-policy response.
This trades unattended recovery of selected dialogs for a verifiable ownership
boundary; it must be evaluated during native acceptance, not silently described
as unchanged functionality.

## Recovery and transaction limits

Mutual exclusion is not a universal undo transaction. Existing transaction
checkpoints and verified rollback remain in place; unsupported rollback modes
remain rejected. Raw scripts and multi-file output are not automatically ACID.
A failed mutation can leave geometry or published files requiring inspection;
the queue fences further execution rather than claiming successful rollback.
Recovery acknowledgement accepts the inspected state; it does not undo a job.

If a dispatcher dies inside COM, merely restarting it does not prove SolidWorks
has finished. The previous uncertain CAD generation must be confirmed ended, or
a genuine returned result must exist. The system never force-closes SolidWorks
or unsaved user documents to satisfy this gate.

## Validation boundary

Automated tests cover the durable journal, real OS process competition/crashes,
the real local transport, deadline/late-return recovery, native ownership and
document guards using doubles, and retained local reliability suites.
The final guarded regression for build `2026.10.18.2` passed **379/379 tests**, with
zero failures, errors, skips or attempted native/GUI actions. The nine unchanged
synthetic broker integration scenarios retain their earlier passing result;
they were not rerun as part of that guarded regression. These are version-specific
prior results, not the final `.3` regression. The `.3` synthetic startup evidence
above is a separate test.
The final `.3` guarded suite collected 392 tests: **391 passed, one explicit
host-capability skip, zero failures/errors and zero unexpected native attempts**.
All nine independent-process synthetic broker scenarios were separately rerun
and passed. These counts remain historical `.3` evidence; `.4` results are
recorded separately at the beginning of this document and in QUEUE_VALIDATION.md.
Synthetic executor evidence must be labelled synthetic. Every new native run
requires an idle activated session with no legacy owners or unresolved recovery
gate. No active working model is used as a test. Native crash injection and
stuck-COM fault recovery remain untested by the successful geometry run.

Earlier local source packages retained model-weight Git LFS pointers. This
GitHub source distribution omits both pointers and weight binaries, while retaining
their identities in the publication manifest and model notices. Queue tests do
not require those assets; raster/vector readiness is not certified by this release.
