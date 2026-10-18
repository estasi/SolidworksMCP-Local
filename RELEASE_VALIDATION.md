# Accepted release: 2026.10.18.6

**Native part-targeting acceptance passed on 2026-10-18** against the unchanged
runtime from commit `d0aa04ec84a501b7a2384bd734300bc615157960`, using SOLIDWORKS 2025 API 33.2.0 on Windows.
The machine-readable [validation record](RELEASE_VALIDATION.json) ties the results
to that commit and the exact runtime manifest. Raw operational evidence is kept
privately; this package contains only the sanitized facts and evidence digests.

## What passed

- Two real MCP stdio clients reported `.6` and shared one existing SW process.
- Six complete operations ran A, B, A, B, A, B on two uniquely created saved parts.
  Queue wait overlapped, while native operations followed FIFO without overlap
  or lost changes. Each part received three 1 mm depth increments.
- Each part then received an ordinary targeted `execute_cad_plan`: a body-volume
  check and normal save under the same operation lease. Both parts were closed,
  reopened and independently measured as one 20 x 20 x 23 mm solid, 9,200 mm3.
- All 31 qualification jobs and 17 native checks passed. Only the owned test parts
  were closed. The initial and final inventories were empty; the same SW and
  broker generations remained alive.
- The full offline suite collected 525: **524 passed, 1 capability skip, 0 failures,
  0 errors**. The separate broker suite passed **9/9**. Native entry points were
  blocked during these offline runs, with zero attempts and unchanged sources.
- Final upgrade verification passed **18/18**: the physical journal and prior
  115 jobs / 464 events were preserved, SQLite integrity passed, and the queue
  was open and idle with no queued/running/recovery jobs.

## Client reconnect accepted

On 2026-10-18, after the user restarted Codex and left SolidWorks running,
actual tools from the existing chat connected to SW2025, reported MCP build
`2026.10.18.6` and confirmed an open, idle queue without recovery jobs.
The post-restart verification is bound by its evidence digest in
[RELEASE_VALIDATION.json](RELEASE_VALIDATION.json). It qualifies this one observed
client restart on the tested workstation; it is not a universal client guarantee.

The earlier `Transport closed` probe from the cached previous connection remains
retained historical evidence. This later pass neither rewrites that result nor
changes the separately recorded two-client native qualification above.

## Scope and limits

This accepts two chats submitting work for different already-open, saved **parts**
in **one** SW2025 instance. Whole CAD operations run sequentially. Assembly and
drawing target switching, two-instance parallel execution, RAM/commit admission
management, native activation fault injection and low-memory stress are not
qualified. RAM admission and separate SW-instance queues remain future work.
No existing working models were loaded during this run, so preservation under
that condition is not claimed. Manual UI/macros/other COM clients bypass this lock.

## Retained earlier failures

The first `.5` two-model attempt failed before activation. A later already-admitted
same-model operation completed; that is not a `.5` acceptance pass. Its original
error was initially obscured by an async ExceptionGroup; `.6` preserves the
concrete error and observes already-dispatched job outcomes without replay.

The inherited `.5` full-suite attempt reported 501 passes, 1 skip and one Windows
temporary-directory cleanup error after image assertions. Its cause remains
unresolved. Those incidents are retained historical evidence, **not current `.6`
failures**. The fresh passing `.6` run does not retroactively explain or erase them.

## Publication boundary

All 75 Python files are byte-identical to the native-tested runtime and exact
commit. Public documentation was updated after acceptance; each such change is
recorded in [publication-manifest.json](publication-manifest.json). The original
MIT license uses its canonical Git bytes. No Git history, private evidence,
runtime state, model payloads or raw native logs are bundled. The two previously
reviewed Git LFS pointers are also omitted from this GitHub source tree; their
identities remain recorded in the manifest and model notices. The publication
target is [estasi/SolidworksMCP-Local](https://github.com/estasi/SolidworksMCP-Local),
tag `v2026.10.18.6`. Publication transport and CI results are separate from this
retained local/native acceptance record.
