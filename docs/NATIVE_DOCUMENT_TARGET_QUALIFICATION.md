# Native qualification harness for build 2026.10.18.6

`scripts/qualify_native_document_targets.py` is an opt-in maintenance test. It
does not activate a release, change configuration, start/stop the broker,
acknowledge recovery, or restart/terminate SolidWorks. Import and `--help` are
COM-free. Run only after the new source and matching interpreter have been
activated, the queue is open/idle and other CAD work has stopped.

```powershell
& $queuePython -B scripts/qualify_native_document_targets.py `
  --acknowledge-idle-session `
  --output-dir C:\CAD-Qualification\target-attempt-unique
```

The output's parent must exist on a fixed local unsynchronized disk. The output
directory itself must not exist. Every attempt retains a new private report,
stdio logs and its own synthetic part files. Never reuse an old attempt directory.

The harness records the initial strict document inventory and saved working-file
SHA256, size, timestamp and file identity. If working documents are open, the
active one must be saved, clean and outside editing/command modes. Existing
documents are never saved or closed. They may have unchanged dirty inactive
state; inability to read their state/files rejects the run before test creation.

Two uniquely named saved parts are seeded using the previously qualified closed,
fully defined profile and typed extrusion route. Each starts as one 20 mm cube.
Two independent Python client processes each open their own real MCP stdio
connection, verify the exact `.6` handshake and target schema, and share the
existing native broker. Six operations are admitted A, B, A, B, A, B. The second
request of each pair is submitted before waiting for completion of the first;
the report must prove actual overlapping queue wait, not just serial clients.
Each complete targeted synchronous operation verifies its active path and
configuration, increments depth by 1 mm, rebuilds, saves and reads back geometry.
Each part then receives an ordinary `execute_cad_plan` with `target_document`:
its single `body_volume` step reads geometry, checks the expected one-body volume,
and the plan saves that owned synthetic part under the same queue lease. The
plan contract saves after success, so this is deliberately not described as a
read-only plan. No checkpoint copies or automatic rollback are requested.

Before the first CAD call the harness pins the existing broker owner through
`SOLIDWORKS_MCP_EXPECTED_OWNER_ID`. The actual MCP frontends and direct status
clients use the production client's pin: broker auto-start is disabled, and a
replacement broker is rejected before admission. The harness refuses a source
that does not enforce this pin. A vanished broker remains a failed attempt.

Acceptance requires three increments per part (20 x 20 x 23 mm, 9,200 mm3),
correct per-model history, FIFO order and non-overlapping broker/journal times.
Both parts are closed/reopened and independently measured again. Cleanup closes
only the recorded paths inside this run's output; original documents and saved
file hashes/stats must match. The original active saved document is restored if
one existed. With an initially empty session, no documents remain open.

Final evidence must show the same live CAD generation and broker, no unexpected
external jobs and an open idle queue after both MCP clients close. On any failure,
the harness leaves uncertain jobs/test parts for operator review. It never
replays a mutation under another ID or forces cleanup through a recovery fence.
Nested async exception groups retain every concrete failure and its job details.
After failure, further worker commands stop; a broker-owner-pinned read-only
observer retains outcomes for stable request IDs already dispatched, including
a later job that completes after the first failure. An absent request remains
unknown until a later lookup or the observation deadline, unless the worker
explicitly proves it stopped before sending that request. Closing a frontend
alone is not proof that broker admission cannot still finish. A recovery fence
is recorded as an uncertain native outcome. Diagnostic write/observation errors
are retained when possible and do not replace the original native failure.
The report is `native-document-targets-qualification.json` under the new output.

Offline harness contract tests are in `tests/test_native_document_targets.py`.
They do not qualify real COM activation or native geometry. Native failures,
such as an unsupported COM return shape, remain failed attempts and must not be
overwritten when the source is corrected and a new attempt is run.
