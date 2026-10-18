# SolidworksMCP 2026.10.18.6: operations and qualification

## Release status

Build `.6` corrects idle-command output normalization in the opt-in selector.
The first native `.5` switch rejected before activation and remains a failed
qualification; see [DOCUMENT_TARGET.md](DOCUMENT_TARGET.md) for the cause. Its
offline selection and queue checks are recorded in [QUEUE_VALIDATION.md](QUEUE_VALIDATION.md).
This immutable source snapshot does not assert native `.6` acceptance. Install
for qualification only in an authorized idle maintenance window; production use
requires the native acceptance result tied to the exact release manifest. Retain
that report beside the release. Native assembly/drawing targeting is not certified.

Historical predecessor qualification:
Build `2026.10.18.4` completed the SW2025 station qualification on 2026-10-18:
two native client processes, geometry/save-reopen checks, successful synthetic
cleanup, two MCP stdio sessions and reconnect after client-application restart.
The same broker and CAD generations were preserved. Earlier failures and the
limits of this single-station result remain in [QUEUE_VALIDATION.md](QUEUE_VALIDATION.md).
An upgrade on a different workstation still needs its own controlled activation
and synthetic native qualification; status from this workstation does not certify it.

The queue CLI does not edit Codex configuration. No instruction in this document
requires killing an existing MCP process, closing another user's documents, or
restarting SOLIDWORKS. Updating a configuration file alone does not unload code
from an already running MCP process.
Use the client's supported reconnect lifecycle, or fully restart its host
application when cached connections persist. Verify the new handshake afterward;
these instructions do not assume a particular settings button exists.

## Ownership and operation boundaries

All participating MCP clients must use the same local queue and the new runtime.
The runtime serializes CAD access through one COM executor in the same Windows
user and desktop session. Its instance mutex
is independent of the release version and configured state directory, so a
second installation or alternate journal location cannot create a second owner
of the same CAD instance.

The protected unit is a complete submitted CAD operation: preflight, document
identity checks, checkpoint preparation where supported, changes, rebuild,
verification, save/export, and final journal result. Error handling and any
supported rollback stay inside this boundary. Submit a multistep workflow as one
CAD plan. Separate tool requests remain separate operations; the queue does not
reserve a document while a client decides what to do next.

The queue is a coordination mechanism, not a universal undo mechanism. Existing
operation-specific checkpoint and rollback restrictions still apply. Manual UI
edits, external macros, and arbitrary background automation that bypasses this
runtime do not respect its lock. Use a controlled CAD session during execution.
Do not launch detached CAD work from a Python operation.

Raw `execute_python` and `execute_python_async` require the explicit per-request
`coordination.trusted_python=true` opt-in. The latter submits asynchronously to
the queue; its script still executes synchronously inside the owned operation.
Scripts must not create threads or child processes, rebind COM, call independent
CAD automation or leave background work. Static syntax checks catch common
mistakes but are **not a sandbox**. The supplied automation objects and Python
runtime are powerful trusted code interfaces. Do not opt in for unreviewed code
or claim that this mode contains a hostile script.

Automatic dialog handling (`ui_guard.auto_resolve_known`) and automatic recovery
(`recovery.auto_recover`) are disabled in the queued native executor. The UI
watchdog observes the selected process; it does not confirm dialogs or send keys.
Resolve a blocker explicitly and then use the recovery procedure below.

Old MCP processes do not participate in the mutex protocol. The new runtime must
fail closed when legacy MCP processes are detected before activation or before
an individual job starts. An unreadable or ambiguous process inventory is not
proof that the station is clear. Process scanning is a guard, not an atomic
barrier against an unrelated program starting immediately after a scan.

The `.4` guard distinguishes an observed process from a stale CIM row using its
kernel creation identity and a nonblocking wait on a process handle. A signaled
handle proves that generation exited; a matching live unknown process remains
a blocker. Generation matching compares UTC FILETIME tokens at CIM's microsecond
precision. An exited, missing or reused process in the first snapshot triggers
one fresh complete scan, for at most two scans. On the final scan only positively
signaled, matching-generation exited objects can be omitted; a missing or reused
process still means an unstable observation and blocks admission. Access errors,
ambiguous generations and unresolved scan instability fail closed.
No elapsed grace period, client-parent whitelist or automatic process termination
is used to make an unknown process safe. This check never authorizes recovery of
an uncertain CAD operation: that still requires the separate recovery procedure.

## Local state and diagnostics

### Independent Windows broker lifetime

The broker must outlive each MCP frontend. Native Windows auto-start uses the
already running Explorer desktop in the same user and session. The fixed child
bootstrap verifies that its immediate parent is that exact Explorer process
generation before importing the broker. The existing desktop view provides
`IShellDispatch2.ShellExecute`; a newly created `Shell.Application` object is not
used as a substitute. An attached child of the MCP frontend is never a fallback.

The broker may retain an operating-system desktop Job Object. This is **frontend
lifetime independence**, not a claim that the process belongs to no Windows job
or survives Windows logoff/shutdown. The installed SDK's exact
`KILL_ON_JOB_CLOSE` job is tested separately: the actual frontend helper is first
verified inside that job, the desktop broker is verified outside it, then closing
the job must terminate the frontend while the same broker generation responds.
The available host retains an outer job even after `CREATE_BREAKAWAY_FROM_JOB`;
the generic full-breakaway positive test is explicitly unavailable there.

For an operator-initiated prestart using the same verified route, run from the
activated release with its configured Python environment:

```powershell
python -m solidworks_mcp.coordination.launch --backend native --desktop
```

Without `--desktop`, `python -m solidworks_mcp.coordination.launch --backend
native` requires no Windows Job Object. Direct native `broker` module invocation
enforces the same check. If Explorer identity/session/provenance cannot be
verified, startup fails closed; it does not start attached, change OS job policy,
register a persistent task, launch SOLIDWORKS or replay uncertain CAD work.

The bootstrap pins the physical state directory and the existing journal's
volume/file identity, including MSIX AppData redirection. It derives its own
`LOCALAPPDATA` from that verified physical location and revalidates the canonical
session path. It never copies or resets the journal. The endpoint and private
launch receipt bind the release version, launch nonce, broker process generation
and physical state identity. The base Python interpreter makes Explorer ancestry
unambiguous; subsequent helpers use the validated venv interpreter. Qualification
checks actual child imports of pywin32/Pillow as well as existing journal rows.

Private launch tickets, consumed tickets and receipts remain under `launch/` for
diagnosis. Status and graceful shutdown still use the normal queue CLI. Frontends
connect to the existing verified broker without creating another owner.

The desktop route follows Microsoft's [existing Explorer automation pattern](https://devblogs.microsoft.com/oldnewthing/20131118-00/?p=2643)
and [ShellExecute contract](https://learn.microsoft.com/en-us/windows/win32/shell/ishelldispatch2-shellexecute).
Windows [nested jobs](https://learn.microsoft.com/en-us/windows/win32/procthread/nested-jobs)
explain why process group flags or partial breakaway alone do not prove lifetime
independence.

The canonical native state directory is:

```text
%LOCALAPPDATA%\SolidworksMCP\coordination\v1\<sessionhash>
```

`<sessionhash>` is a 16-character hash of the Windows user/session identity. All
participating clients in that session resolve the same directory. Another RDP
session uses a different journal and endpoint, matching the mutex scope. Native
clients reject an alternate state directory; arbitrary isolated directories are
reserved for the explicitly enabled synthetic test backend.

Keep local application data on a local, writable fixed disk outside Git,
network shares and cloud synchronization. Do not move or redirect state into
YandexDisk, OneDrive or a temporary directory. The directory's protected ACL
allows the current user and SYSTEM; the endpoint credential stays there.

State may contain CAD plans, document paths, results, error details and recovery
information. Treat it as private operational data. Do not attach the raw journal,
SQLite sidecars, diagnostic logs or recovery bundles to a public GitHub issue.
Share a separately reviewed summary with synthetic paths and counts instead.

Activation-guard failures in `.4` retain a private diagnostic record under the
canonical state's `guard-events/` directory. Retention is limited to 16 records,
each at most 64 KiB, with at most 12 projected process rows and a truncation count.
Records contain the format, observation time, error code and bounded process
fields: PID, name, creation time, parsed server-script path and kernel status.
They exclude full command lines, environment values, free-form exception text
and tracebacks. Even these limited process identities and paths are private
runtime data, not public release artifacts. The current record is atomically
committed before older records are pruned. A write or pruning failure blocks the
guard; it never converts an unknown observation into successful admission.
The 16-record limit applies after successful pruning. If deletion fails, the
new failure record is retained even if the folder temporarily exceeds that limit;
admission remains blocked until the storage problem is resolved.
This diagnostic retention is separate from the durable CAD queue journal and
does not rewrite the history of submitted or failed CAD jobs.

Run CLI commands from the candidate checkout using its chosen Python runtime:

```powershell
python -m solidworks_mcp.coordination.cli --help
python -m solidworks_mcp.coordination.cli status
python -m solidworks_mcp.coordination.cli doctor
```

Use each subcommand's `--help` for the accepted arguments in this release. Status
and recovery diagnostics must remain available when CAD is blocked; they must
not require reading the active model through COM. A running `SLDWORKS.exe` process
alone does not prove that COM access, the expected document, or model validation
works.

Before submitting a mutation, assign a stable `coordination.request_id` and keep
it until the outcome is known. After a lost response or client restart, call the
MCP tool `get_queue_request` with the same `request_id`; it reads the durable
journal without COM or replay. `get_job_result`, `get_queue_status` and
`queue_events` are also control-plane reads. Do not generate a new request ID to
work around an uncertain result. `coordination.expected_document` is checked
after the queue wait: use an absolute saved path, or an exact unsaved title with
`allow_unsaved_document=true`. It remains a compare-only guard: a model being
active when submitted is insufficient, and this field never activates a model.

Build `.6` optionally accepts `coordination.target_document` instead. It selects
an already open saved model after waiting, inside exclusive ownership, then
checks full identity and preservation before ordinary preflight. Never supply
both fields. Pin document-specific reads too; otherwise they use the active model.
A real switch requires clean current/target models and known safe edit state.
Unknown state, ambiguous identity, active sketches and in-context edits reject
switching. Same-model dirty/sketch continuation remains subject to normal tool
preflight, but an assembly must always be editing itself, even when already active.

Chats may submit concurrently; whole operations execute strictly sequentially.
Finish dependent edits, exit sketch/edit mode, verify and explicitly save within
one whole operation before yielding to another model. The selection layer never
opens, saves, closes, rebuilds, changes configurations or confirms dialogs.
Ordinary rejected preconditions do not create a recovery fence; uncertain
activation does. See [DOCUMENT_TARGET.md](DOCUMENT_TARGET.md) for the exact
contract, example and separate native acceptance gate.

## Safe installation and cutover

1. Keep the working release and its Python environment immutable. Prepare the
   candidate in a separate directory, for example
   `%LOCALAPPDATA%\SolidworksMCP\releases\<candidate>`. Do not install or upgrade
   packages in the environment used by active MCP sessions.
2. Run the local unit and independent-process tests without connecting to CAD.
   Record the candidate commit, Python/dependency versions, test command,
   completed scenarios, failures, and skipped checks. Use synthetic data only.
3. Wait for ongoing CAD work to finish. Have clients stop submitting new jobs
   and disconnect their old MCP sessions normally. A chat finishing does not
   establish that all its MCP processes have exited. Inspect `doctor` and the
   process inventory without signalling the processes.
4. Require no legacy MCP processes and an unambiguous target SOLIDWORKS process.
   Select its explicit PID and expected API major revision 33. Do not bind only
   by a version-independent ProgID when more than one instance is possible.
5. Inspect activation arguments, then activate the candidate for that explicit
   target and version:

   ```powershell
   python -m solidworks_mcp.coordination.cli activate --help
   python -m solidworks_mcp.coordination.cli activate --target-pid <PID> --expected-major 33 --acknowledge-idle-session
   ```

   Activation must refuse a missing/mismatched target or legacy owners. Never
   bypass the guard to accommodate an active neighbouring session. Replace
   `<PID>` with the verified numeric target PID. The idle-session acknowledgement
   is mandatory and must reflect the observed station state, not an assumption.
   Activation is a non-COM binding of the selected PID, process generation,
   expected version and release. Actual API identity is checked by the native
   executor. It never deletes or replaces the journal and never clears a recovery
   fence. Retargeting requires the broker to be stopped; any retained uncertain
   jobs still require separate verified recovery before CAD work can resume.
6. When the station is clear, change only the intended client's SolidWorks MCP
   configuration block to the candidate interpreter and entry point. Preserve
   its previous block exactly and preserve unrelated settings. This is a
   separate operator action; the queue CLI does not perform it. Reconnect clients
   through their normal lifecycle, with no mixed old/new runtimes.
7. Run the native acceptance below on disposable synthetic documents. Activate
   normal work only after inspecting and retaining the results. Do not qualify
   against the operator's already open production documents.

Verify the MCP `initialize` handshake's `serverInfo.version` against the prepared
release version after reconnecting. A changed configuration or activation file
does not establish which code a previously started transport is serving. For
the document-target candidate the expected build is `2026.10.18.6`; retain this proof
with the release manifest and native qualification result.

Do not reuse a previous release's activation or rollback script without checking
its expected source path, interpreter and saved-state format. An installer that
expects the original upstream installation may reject or incorrectly describe a
transition from a locally modified release.

## Prepare a release and switch the client configuration

The preparation script requires a clean committed checkout and a destination
that does not exist. It copies `git archive HEAD`, records SHA256 values and the
package version, and writes the manifest last. It does not activate a broker,
change client configuration, install dependencies or fetch Git LFS objects.

```powershell
python scripts/prepare_queue_release.py --repo "C:\MCP-Source\SolidworksMCP" --output "$env:LOCALAPPDATA\SolidworksMCP\releases\2026.10.18.6"
```

Run the switch script from the prepared release. Replace the fictional source
and runtime paths below with the reviewed installation paths. The first command
is a dry-run: it validates the TOML transformation but creates no backup, acquires
no lease and does not establish that the station is idle.

```powershell
$queueSwitchArgs = @(
    'scripts/switch_mcp_config.py',
    '--config', "$env:USERPROFILE\.codex\config.toml",
    '--release-source', "$env:LOCALAPPDATA\SolidworksMCP\releases\2026.10.18.6",
    '--python', 'C:\MCP-Runtime\venv\Scripts\python.exe',
    '--expected-old-source', 'C:\MCP-Releases\2026.10.18.5\source',
    '--backup-dir', "$env:LOCALAPPDATA\SolidworksMCP\config-backups"
)
python @queueSwitchArgs
```

The example shows the document-selection update from build `2026.10.18.5`. Set
`--expected-old-source` to the exact currently configured source for the station;
the script must reject a different old installation rather than guess.

Only after the maintenance prerequisites above are satisfied, append the
explicit write flag:

```powershell
python @queueSwitchArgs --apply
```

Apply requires the stable CAD lease to be free and the legacy-process scan to
pass. It changes only `cwd`, `command` and `args` in the SolidWorks table; other
table settings, environment entries and foreign bytes remain intact. Conventional
single-line assignments are required; ambiguous layouts are rejected. BOM,
line endings, assignment comments and file access permissions are preserved.

Each apply creates a unique private backup with exact old/new MCP-block bytes
and a manifest containing block and whole-config hashes. Other providers'
configuration values are not copied into the backup. Keep the returned backup
path and activated whole-config hash for restoration. Output masks the user
profile prefix and never prints TOML values.

The installer lock coordinates this installer only. It compares current bytes
again immediately before atomic replacement and verifies afterwards. Do not save
the same file concurrently from an unrelated editor; the filesystem operation
does not provide a universal compare-and-swap contract for uncooperative writers.

## Recovery after an uncertain operation

Timeout is not cancellation of a COM call. Loss of the client connection is not
evidence that the operation stopped. The next job must not start while the old
executor may still affect CAD. An interrupted operation with an unknown result
must remain blocked for recovery and must not be automatically replayed.

1. Inspect `status` and `doctor`; retain the journal and recovery evidence.
2. Establish that no old executor is still running or able to act on the target.
   Do not clear a lock or delete a journal to make the warning disappear.
3. Inspect the actual CAD and filesystem state: document identity, unsaved
   changes, checkpoint outcome, rebuild result and any saved/exported files.
   A rollback of the model does not prove that an external export was undone.
4. Follow the explicit recovery acknowledgement required by this release:

   ```powershell
   python -m solidworks_mcp.coordination.cli recover --help
   python -m solidworks_mcp.coordination.cli recover --job-id <JOB_ID> --operator "<operator-label>" --note "<verified recovery reason>"
   ```

   Repeat `--job-id` when reconciling more than one job. Use a meaningful operator
   label and record the verified reason, avoiding sensitive model details. The
   command can reject recovery while the uncertain SOLIDWORKS process remains
   alive without a proven late completion; acknowledgement does not bypass that
   verification.

5. Resume only after the uncertain operation has been reconciled. Preserve its
   historical outcome. A successful later retry does not change the original
   interruption into a successful execution.

If a COM call remains blocked, leave the queue fenced and arrange a maintenance
window with the CAD operator. Administrative recovery must not force-release
ownership while the executor is alive. Do not copy or edit a live SQLite database
and its sidecars independently; use a supported consistent backup or a verified
quiescent state.

## Graceful shutdown and rollback

Inspect shutdown options before using them:

```powershell
python -m solidworks_mcp.coordination.cli shutdown --help
python -m solidworks_mcp.coordination.cli shutdown
```

Stop accepting work, complete the current operation, and verify that the new
executor has relinquished ownership. If an operation remains uncertain, resolve
it before restoring legacy access. Preserve the new journal; do not make an older
release consume an unrecognised journal schema.

Shutdown preserves queued jobs in the journal; it does not execute the backlog
or cancel it. Before abandoning the candidate, inspect and cancel pending jobs
that should not resume on a future broker restart. A shutdown response that says
`draining` is not proof the current operation has finished.

Restore the previously saved client configuration block only after the new
runtime has stopped, then reconnect the old client normally. Keep the candidate
and its evidence for diagnosis. Re-enabling a legacy client while the new broker
or executor is active bypasses the new safety protocol and is not a valid rollback.
If obsolete entrypoints were retired to prevent cached launches, restoring only
the client configuration is insufficient: restore the exact hash-verified
entrypoint backups within the same stopped and reconciled maintenance window.

Use the exact backup directory and activated whole-config hash from the switch
result. Start with the default dry-run:

```powershell
$queueRestoreArgs = @(
    'scripts/switch_mcp_config.py',
    '--config', "$env:USERPROFILE\.codex\config.toml",
    '--restore-backup', "$env:LOCALAPPDATA\SolidworksMCP\config-backups\<backup-id>",
    '--expected-current-sha256', '<activated-full-config-sha256>'
)
python @queueRestoreArgs
python @queueRestoreArgs --apply
```

Restore refuses any configuration change since activation, including an unrelated
table edit. It also refuses corrupt backups, a live broker, legacy clients or
still-connected candidate MCP clients that might restart the broker. It does not
overwrite later edits to force a rollback. Review and reconcile those changes
separately; do not weaken the expected hash or edit the backup manifest.

## Required qualification evidence

### Pin qualification clients to one broker owner

For native qualification, record the existing idle broker's `owner_id` from a
verified queue status/endpoint and pass it to **every** control client and MCP
stdio frontend as `SOLIDWORKS_MCP_EXPECTED_OWNER_ID`. The value must be exactly
32 lowercase hexadecimal characters, captured from that broker rather than
invented. Native `QueueClient` reads the environment at construction; Python
callers may instead pass `expected_owner_id=recorded_owner`. If both are supplied
they must match. An invalid/conflicting value fails before endpoint access.

A pin forces `auto_start=False`. Each request checks the endpoint's exact owner
as well as the existing source, native lifetime and process-generation checks.
A changed/missing owner fails with `BROKER_OWNER_CHANGED`; a stopped broker fails
without launching a replacement. Record the stopped qualification and inspect
its original journal instead of continuing automatically with another broker.
The endpoint authentication token still binds each transport connection. Keep
owner/endpoint evidence private; do not print endpoint credentials.

This is an opt-in qualification control. Normal clients without a pin retain
the existing verified desktop auto-start behavior. Do not leave a qualification
owner pin in a general client configuration after deliberately replacing its
broker; reconnect according to the operator procedure.

Record local/fake-COM tests separately from the following live CAD checks:

The corrective harness creates and verifies a fully defined profile, confirms
sketch exit, and uses the typed extrusion path before starting concurrent client
work. Retain complete job results and diagnostics, including seed failures, in
private local evidence. A failed seed is an incomplete native qualification,
not a two-process pass. Keep the original failed run even if a later run passes.

| Scenario | Required evidence |
| --- | --- |
| Two independent MCP client processes | Ordered full-operation start/end records with no overlapping CAD access; both intended results verified |
| Multistep plan | No second operation between preflight, mutation, rebuild, verification and save |
| Background submission and ordinary request | Both use the same queue and executor; polling stays responsive without model access |
| Pending cancellation | The cancelled job never reaches CAD |
| Client loss and repeat request | Result remains queryable; duplicate submission does not repeat a completed mutation |
| Executor loss | Uncertain operation is retained; later CAD jobs remain blocked until explicit recovery |
| COM timeout | No successor starts while the original call may still run |
| Journal or process-inventory failure | CAD work fails closed with actionable diagnostics |
| Alternate state directory or release copy | The stable instance fence still prevents a second CAD owner |
| Legacy MCP process appears | New CAD jobs are refused; no process is killed automatically |
| Save/export or rollback failure | Outcome is preserved accurately; partial filesystem effects are identified |
| Successful native result | Rebuild/errors, geometry, save, close and reopen verified on synthetic documents |

Until these live checks pass, report the candidate as staged with native
qualification outstanding. Do not extrapolate SW2025 results to SW2026 or SW2027.
