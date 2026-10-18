# Queue candidate validation — 2026-10-18

Current accepted part-targeting build: **2026.10.18.6**. The final native run
passed 31 jobs and all 17 checks; upgrade verification passed 18/18. The full
offline suite passed 524 with one capability skip; the separate broker suite
passed 9/9. See [RELEASE_VALIDATION.md](../RELEASE_VALIDATION.md) for the exact
source binding, sanitized evidence, retained `.5` incidents and scope limits.

The sections below preserve historical source-freeze checks and diagnoses.
Their pending-gate descriptions are superseded for `.6` part targeting by the
linked acceptance record; their other limitations and `.5` failures still apply.

The qualified predecessor is **2026.10.18.4**, native source commit
`5862415ba43bc2586a764459ac9821eefb804f3d`. The `.4` publication changes were
documentation/notices and module documentation only. Its acceptance is preserved
below as historical evidence; it does not certify `.5` selection behavior.
The historical provisional name `6.6.0-queue.1` is not an upstream release.

## .6 offline correction verification

- Focused target/owner/routing suite: **115 PASS**, zero failures/errors/skips.
  It includes 58 document-target tests, 18 owner-pin tests and 39 routing guards.
- Complete queue suite: **225 collected, 224 PASS, 1 existing Windows-job
  capability SKIP**, zero failures/errors.
- Both runs blocked native entry points and recorded **zero native attempts**.
  All Python source hashes were identical before and after the passing runs.
- The exact observed idle by-reference reply fails against the previous `.5`
  guard in a retained red regression, then passes with the narrow normalization.
  New cases cover typed/null title, untouched title, other integer command IDs,
  unwritten outputs, malformed replies and active commands. Rejections prevent
  activation/geometry; active state discovered after activation fences the queue.
- These results are synthetic/queue evidence, not a new complete inherited-suite
  result, native `.6` qualification or clearance of earlier retained failures.

## Retained .5 native switching failure and .6 correction

The first native two-model `.5` qualification did not pass. Both actual MCP stdio
clients completed initialization with version `.5`. Switching from test model B
to A failed before activation with `TARGET_DOCUMENT_UNSAFE_EDIT`; no geometry
was dispatched for that request and it did not require a recovery fence. A
same-active-model B operation subsequently completed. This is not successful
A/B isolation or whole-run acceptance.

A retained read-only probe then observed `GetRunningCommandInfo` returning void,
command output -3, an unchanged title sentinel and `IsUiActive=False`; the model
was clean and its blocking state was zero. Two earlier diagnostic attempts were
rejected (a forbidden import, then a wrong configuration caused by decoding UTF-8
with the Windows default code page); they did not reach the diagnostic COM reads
and remain separate from the original selector failure.

The `.6` correction accepts string/null PMTitle without requiring it to be
rewritten, while still requiring a written integer command ID and explicit false
activity flag. The exact observed by-reference case fails on the old guard in a
retained offline regression and is added to the corrected test matrix. Active,
unwritten and malformed states still reject before activation/geometry.
The qualification harness also fixes ExceptionGroup masking so the originating
failure is retained instead of being replaced by a generic worker error.
Native `.6` acceptance remains a separate release report; these diagnoses are not
a pass for `.5` or `.6`. Assembly/drawing targeting is not natively certified.

## .5 offline selection and queue regression

- Focused selection suite: **47 PASS**, zero failures/errors.
- Owner-pin suite: **18 PASS**, zero failures/errors/native attempts.
- Final queue suite: **214 collected, 213 PASS, 1 explicit Windows-job capability SKIP**,
  zero failures/errors. Native entry points were blocked.
- Two independent client processes used the real queue and synthetic COM
  documents. Six queued operations each contained two synthetic counter steps;
  FIFO order, non-overlap and per-document counter isolation passed. This is not
  native geometry, a CAD-plan geometry workflow or save/reopen acceptance.
- Tests cover invalid/closed/ambiguous paths, dirty/edit states, configuration
  assertions, failed or uncertain activation, state preservation, and an already
  active assembly editing a different/unknown target. Same-model sketch
  continuity remains covered. Runtime source hashes were unchanged across the
  final passing runs.
- Earlier failed test attempts remain retained: a long test TEMP path produced
  Windows path-length errors, and a test double incorrectly exposed a mock as a
  COM property instead of a method. The final runs used a short isolated test
  TEMP and corrected test doubles; production checks were not weakened.

A later external test runner omitted the Windows multiprocessing main guard;
spawned children re-executed the suite and produced failed, incomplete concurrent
logs. That run is retained as invalid harness evidence, not acceptance. No native
entry was attempted and no production code changed to fix it. A new isolated
runner with the main guard passed the final 214-test queue run above, with all
Python source hashes equal before and after. The owner-pin tests passed 18/18
both before and after the runner correction.

Native part-targeting qualification, native assembly/drawing targeting and
uncertain native activation fault scenarios are not established by these tests.
See [DOCUMENT_TARGET.md](DOCUMENT_TARGET.md) for the separate native gate.
The fresh dependency/full-inherited-suite checks below belong to `.4`; they are
not a new full-suite or native `.5` result.

## Final .4 station acceptance

- SOLIDWORKS 2025 API **33.2.0**; original CAD process preserved.
- Two independent native client processes applied three increments each, depth
  20 to 26 mm. FIFO order, no whole-operation overlap/lost updates, one solid,
  20 x 20 x 26 mm bounds and volume 10,400 mm3; saved/reopened geometry matched.
- Sixteen native acceptance jobs completed. Only synthetic test parts were used;
  cleanup completed, with zero documents open afterward. Initial user documents: 0.
- Two real MCP stdio sessions reported `.4`, shared one broker and serialized
  native reads. The broker survived both frontend closures.
- After the user-reported client-application restart, the current chat reconnected
  successfully; exact live broker/CAD generations and owner identity matched.
  The queue was native/open/idle, with no pending recovery and no legacy clients.
- The same physical journal survived. Prefix hashes for 26 jobs/106 events and
  66 jobs/271 events matched; SQLite integrity passed. Original failures were not
  rewritten or replayed. Read-only reconnect checks submitted no CAD jobs.
- Final guarded regression: **423 PASS, 1 SKIP, 0 failures/errors**, zero blocked
  native attempts. Separate synthetic multi-process broker suite: **9 PASS**.
  The skip is a host Windows-job capability, not a passed native scenario.
- Passive process-inventory correction: 48 synthetic processes, three rounds,
  two positively proven exited CIM rows excluded; no legacy false blocker.
- The actual frontend SDK-job lifetime test passed; no claim of exclusion from
  every operating-system job or survival of Windows logoff is made.

### Retained failures and limits

The first two .4 regression runs had respectively two and one Windows temporary-
directory cleanup errors. Their cause remains unresolved. A later full run used
isolated test TEMP/TMP outside MSIX LocalTemp; production cleanup was not changed,
errors were not suppressed and failed runs remain failed. This does not establish
cleanup/CI stability. An initial passive probe missed the race window and remains
failed; a subsequent timing-corrected probe reproduced it. Earlier .1/.3 native
and cleanup failures remain recorded below; unrecorded offenders remain unknown.
A reconnect check detected a different whole application-config hash; the entire
SolidWorks MCP section matched the hash-verified activation backup. No config was
edited or cause attributed.

This acceptance does not cover multiple initially open user models, automatic
switching between models, every inherited tool, intentional real SolidWorks
crashes/stuck COM, other CAD versions, model weights or GPU/raster readiness.
Manual UI/external COM bypasses the queue. Raw Python checks are not a sandbox.
Native timings were observed, but no speedup comparison was performed.

## Publication installation check

A fresh isolated Windows/Python 3.14 environment installed the four pinned core
requirements from public PyPI on 2026-10-18; 31 distributions were resolved.
The dependency inventory is in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
This does not install optional scientific/GPU dependencies. Running the full
inherited suite in that core-only environment produced **385 passed, 2 failures,
36 errors, 1 skip**, with zero native attempts. The 36 errors were missing numpy,
OpenCV or Shapely; one failure also recorded missing Shapely. For the other,
the dependency cause was initially inferred from its code path. The failed run
and diagnosis are retained; it was not called a full-suite pass.

A second new environment installed the identical 31 core versions plus five
scientific test requirements (41 resolved distributions total), without PyTorch,
Transformers, model downloads or changes to the operating CAD environment.
`pip check` passed. With isolated test TEMP, the guarded full suite passed
**423/424 with 1 explicit host-capability skip**, zero failures/errors and zero
native attempts; both formerly failing assertions passed. The separate broker
suite passed **9/9**, with zero native attempts. No retries or cleanup-error
suppression were used. Version constraints and test extras are included with
the source. A separate core-only queue/document-boundary run passed **174/175**
with the same one explicit host-capability skip and zero native attempts.

These are local offline results, not a GitHub CI run or fresh native CAD trial.
The installed source, live queue and models were untouched during preparation.

The remaining sections preserve earlier stages and explain the correction.

## Original pre-cutover local evidence

The following results belong to the original staged candidate before live client
configuration was switched. They establish the tested synthetic/local boundaries;
they are not a fresh certification of every later corrective-release change.

- Windows guarded regression: **362 tests passed**, zero failures, errors or
  skips. The runner rejects unexpected COM attachment/creation and GUI actions;
  it recorded zero such attempts. It excludes the broker integration suite.
- Real independent-process broker integration: **9 tests passed** in 12.677 s.
  These use production journal, mutex, transport and client code with the
  explicitly enabled synthetic executor. Two client processes submit overlapping
  requests; the assertions verify FIFO execution and no full-operation overlap.
- Broker scenarios include crashes before/after synthetic mutation, no automatic
  replay, response-loss deduplication across client identity changes, timeout and
  late-return gating, responsive status, queued cancellation, graceful drain and
  restart, duplicate owner rejection and transport admission limits.
- Recovery regressions cover a crash after claim but before COM binding,
  activation failure before attachment, immutable native process identity,
  rejection of result-only no-execution claims, and blocked UI before recovery
  initialization. All native objects in these tests are doubles.
- The native qualification harness has seven pure tests. Static review corrected
  native mass-property adaptation and tolerance-based geometry comparison after
  reopening. Importing/testing the harness performs no native work.
- Deployment tests: **28 passed** after the final archive correction. They
  exercise exact TOML byte preservation, BOM/CRLF, unrelated
  settings, private unique backups, hash-checked restoration, Windows DACLs,
  refusing active owners, and source-only release manifests on temporary files.
  Two added cases verify disabled LFS filters and pointer-only archives without
  network fetches; these were added after the 362-test guarded run above.

The tested interpreter is Python 3.14 from the existing MCP virtual environment;
its packages were read without upgrading or installing into that environment.
Core dependency versions are recorded in `requirements-queue.txt`. The added
GitHub workflow has not run on GitHub; its Python 3.11 matrix is not a local
qualification claim.

The initial full-suite attempt failed because Windows multiprocessing cannot
spawn from a stdin runner and a legacy test still referenced the removed
in-memory job manager. That evidence was retained separately. A saved runner
and a test of the replacement shared-job result contract resolved the causes;
subsequent guarded runs passed. The first archive attempt exposed Git LFS smudge
attempting unavailable assets; the preparation path was corrected to prohibit
smudge/network access and retain honest pointer-only asset metadata.

## Build 2026.10.18.2 local regression

The final guarded regression for build `2026.10.18.2` passed **379/379 tests**,
with zero failures, errors, skips or attempted native/GUI actions. This is local
code evidence for `.2`, not final `.3` regression evidence. The nine earlier independent-
process synthetic broker scenarios above were unchanged and were not rerun;
their retained result is not presented as a new run or native evidence.

## First live cutover and retained native failure

The earlier statement that no configuration switch or native CAD operation had
occurred described only the pre-cutover stage. Build `2026.10.18.1` was later
selected in the live MCP client configuration and activated against the same
existing SW2025 process. This was a configuration transition to a separate
prepared source release; it did not overwrite the retained legacy source.

The first real native qualification created a disposable synthetic part, then
failed during seed geometry creation before reaching its two-process phase.
The failed run is retained and is not counted as a native concurrency pass.
The recovery-required gate blocked further CAD work. Recovery acknowledgement
was performed only after a genuine native return and UI readiness were verified;
the disposable test part was subsequently saved and closed. These observations
verify that recovery was required for this failed operation. They do not prove
stuck-COM or process-crash recovery, or establish a successful native CAD cycle.
The first-build broker subsequently drained and shut down while idle. The
retained journal contained eight completed jobs and one failed, reconciled job,
with no queued jobs. Shutdown did not erase or relabel the failed outcome.

The observed installation is SW2025 SP2.0 (revision 33.2.0.0128). Its identity and
the successful activation do not alone certify queue behavior inside CAD.

## Build 2026.10.18.2 native qualification: passed

The corrected seed uses a fully defined profile, verified sketch exit and the
typed extrusion path. The real SW2025 run reported native revision 33.2.0 and
completed the following checks:

| Check | Recorded result |
| --- | --- |
| Independent native clients | Two OS client processes, three increments each |
| Dimension changes | Six 1 mm increments; depth 20 to 26 mm |
| Queue execution | FIFO and no whole-operation overlap |
| Native geometry | One body; bounds 20 x 20 x 26 mm; volume 10,400 mm3 |
| Persistence | Save, close and reopen completed; reopened geometry matched |
| Existing work preservation | Initial user-document count zero; document-state and saved-file preservation checks passed for that empty initial set |
| Post-test cleanup | Synthetic part saved and closed; idle broker drained |

This is real native two-process evidence for `.2`. The empty initial document
set means this run does not demonstrate preservation while multiple user
documents are open. The original `.1` failure remains unchanged. Native crash
injection and stuck-COM fault recovery were not exercised by this geometry run.

## Build 2026.10.18.3: passes and retained failures

Build `.3` uses existing Desktop Explorer for native broker auto-start. It
verifies Explorer's executable, desktop HWND owner and process generation,
SID/session, and the child's immediate parent. A private receipt binds the
release version and launched process identity. Physical state-directory and
journal file IDs are pinned across MSIX virtualization; a logical path spelling
alone does not establish that two processes opened the same journal. Identity
or receipt mismatches fail closed.

The production desktop-launch route passed synthetic qualification in
`tests/qualify_desktop_launch.py`. The test verified the actual frontend process
inside the exact SDK Job Object, forced that disposable frontend's closure and
confirmed its exit, then verified that the same broker generation survived
outside that job. The broker may still belong to an OS desktop job; rejecting
all Windows job membership was an overly broad earlier draft invariant.
Original journal rows and events remained unchanged. The virtual-environment
helper passed pywin32 and Pillow import checks. This test exercised no native
CAD and does not establish native crash recovery or raster/vector capability.

Final `.3` guarded regression: **392 collected, 391 passed, one explicit skip,
zero failures/errors and zero unexpected native attempts**. The skipped generic
full-breakaway positive scenario is unavailable under the host's outer OS job;
the production desktop-launch route passed its separate actual SDK job test
described above. All **nine independent-process synthetic broker scenarios**
were rerun after the launch/client changes and passed without skips or failures.

Subsequent `.3` evidence extends those source-preparation results:

- The native run retained as attempt 05 passed all 16 submitted jobs, including
  six increments from two independent client processes. FIFO/non-overlap,
  one 20 x 20 x 26 mm solid, volume 10,400 mm3, and save/close/reopen matching
  geometry passed. The initial user-document inventory was empty.
- Two real MCP stdio sessions reported build `2026.10.18.3` in initialization,
  shared one native broker and performed serialized native document reads.
  The same broker survived both sessions closing. Its independent lifetime was
  also observed across a host-application restart.
- Final test-part cleanup was first rejected before admission with
  `LEGACY_CLIENTS_PRESENT`. A subsequent admitted cleanup also failed at that
  guard and left the queue fenced, despite a genuine returned native result.
  No successful final cleanup or zero-open-document state is inferred from the
  prior geometry and transport passes.
- Earlier `.3` failures remain retained. Attempt 03 encountered a known cached
  older MCP client and required explicit reconciliation. Attempt 04 seeded the
  synthetic part successfully, then both workers were rejected before admission.
  Those rejected requests had no assigned queue jobs. The exact offending
  processes for attempt 04 and the later cleanup failures were not captured.

These results prove the recorded scenarios, not unconditional readiness for
permanent operation. The `.3` process guard required a corrective release.

## Passive reproduction and corrective build 2026.10.18.4

A diagnostic comparison used unchanged production process inventory from both
the execution host and a helper launched through verified existing Explorer.
Five snapshots agreed; the observers, broker, MCP processes and CAD process had
matching user/session scope and medium integrity without elevation. The sampled
contexts did not support a persistent privilege mismatch. No CAD or queue
request was made by this diagnostic.

A separate bounded passive probe created 48 short-lived Python children in
20.4 seconds. All children exited. During churn the unchanged scanner emitted
11 legacy blockers with unavailable script paths; all 11 had signaled process
handles and kernel creation times matching their CIM rows within 0.85 ms. Three
were conclusively the probe's own children: their exact generations were recorded
alive initially, then the original retained handles signaled with exit code zero.
Baseline and post-churn snapshots were clear. This proves a false-positive path
for exited CIM rows; it does not retrospectively identify the missing offenders
in earlier failures. No native CAD operation or recovery action was used.

Build `.4` adds kernel process-generation/liveness checks, bounded fresh inventory
scans and private bounded diagnostics. Live unknown clients, access errors and
unresolved scan instability still block CAD. This correction is not permission
to clear an existing recovery fence or replay an uncertain operation.
The intended diagnostic contract retains at most 16 private records, bounded to
64 KiB and 12 projected process rows each, without raw command lines or environment
values. Its write/pruning failures must fail closed. The final `.4` tests must
verify this contract alongside liveness, generation reuse, bounded rescanning
and the continued rejection of live unknown or legacy processes.

At source preparation, `.4` acceptance was pending. The final `.4` results are
now recorded above against the installed source and initialization handshake;
earlier-build test counts have not been relabeled as `.4` evidence.

Run `scripts/qualify_native_queue.py` only in an idle maintenance session after
controlled activation and recovery reconciliation. Preserve existing document
states and saved working files without closing or saving user documents. Keep
resulting evidence alongside the exact prepared release; do not copy private
paths, process IDs, journals or customer information into publishable documents.
The harness does not kill or restart SolidWorks to simulate faults. Report each
completed native scenario and each remaining limitation separately.

## Publication limits

The current tree is prepared for a source release, with MIT attribution retained.
Selected credential/private-path checks are bounded screening, not a complete
history/privacy certification. The inherited missing historical README blob was
recovered only in the isolated repository from upstream after exact Git-object
hash verification. Git integrity checks and the bounded history scan passed;
the original installation's Git objects were left unchanged.
At the time of that earlier review, model paths held Git LFS pointers and no
GitHub repository, push, release or pull request had been created. The later
source-only GitHub publication omits those pointers and the weight binaries;
their identities remain in the publication manifest and model notices.
