# SolidworksMCP Local — 2026.10.18.6

Control an interactive **SOLIDWORKS 2025** session from an MCP-capable AI client.
This independent fork adds a durable shared queue, whole-operation locking and
explicit recovery to the inherited modeling tools. Its date-based build number
is independent of upstream: this is **not upstream version 6.6.0**.

Original project: [alisamsam/Solidworks-MCP](https://github.com/alisamsam/Solidworks-MCP),
by **Samsaam Ali Baig**. Immediate upstream:
[czuryk/SolidworksMCP](https://github.com/czuryk/SolidworksMCP), baseline
[`38468b3` (6.5.31)](https://github.com/czuryk/SolidworksMCP/commit/38468b32890a9fc4c4aeec50fa97f679496c4524).
The original [MIT license](LICENSE) and copyright notice are preserved.
This fork is not endorsed by either upstream or by Dassault Systèmes.

## Verified scope

**Build `2026.10.18.6` passed native acceptance on 2026-10-18** using SOLIDWORKS
2025 API 33.2.0. Two real MCP stdio clients operated on two disposable saved parts:
six alternating whole operations, FIFO order, no native overlap or lost changes,
two ordinary targeted CAD plans, and matching geometry after save/close/reopen.
All 31 native qualification jobs and the final 18 upgrade checks passed.
The existing Codex chat also passed reconnect verification after the user
restarted the client application on 2026-10-18: its actual MCP tools connected to
SW2025, reported build `2026.10.18.6` and confirmed an open, idle queue without
recovery jobs. SolidWorks was left running. The earlier `Transport closed` probe
is retained as history; this is one observed restart on the qualified workstation.

The complete offline suite passed 524 tests with one capability skip (525 collected,
zero failures/errors); the separate broker suite passed 9/9. These results apply
to the unchanged runtime at commit `d0aa04ec84a501b7a2384bd734300bc615157960`.
Read [release validation and limits](RELEASE_VALIDATION.md) or its
[machine-readable record](RELEASE_VALIDATION.json). They also retain the failed
`.5` native attempt and its unresolved historical Windows test-cleanup incident.

The accepted mode is **two chats, different saved parts, one SW instance**. CAD
operations run sequentially. Multi-instance execution and RAM/commit admission
management remain future work; assembly/drawing switching is not natively
qualified. The test began and ended with no working documents open. Qualification
on this station does not certify every client, installation or model workflow.

The separately qualified predecessor has the following historical evidence.
Build `.4` passed a native two-process qualification on **SW2025 API 33.2.0**:
six ordered changes to a synthetic solid, no overlap or lost updates, and matching
geometry after save/close/reopen. Two real MCP stdio clients shared one broker.
Cleanup and reconnect passed; the broker and SolidWorks survived a restart of
the client application. These are results from one Windows workstation, not
certification of every tool, CAD version or AI client. No user documents were
initially open. Earlier failures remain recorded. See
[acceptance and limits](docs/QUEUE_VALIDATION.md).

## AI client compatibility

The server uses standard **MCP over local stdio**, not a model-specific API.
Use a client that can launch a local MCP process and call tools. The model may
be cloud-hosted or local; the MCP server and licensed SolidWorks run on the
Windows CAD workstation in its interactive user session. An application without
MCP support needs an adapter. No remote HTTP/SSE endpoint or browser-only
connection is provided here.

Different compatible clients can share the queue. All must use the **same
activated source path**, interpreter and Windows user/desktop session. Check the
MCP initialization response for `serverInfo.version = 2026.10.18.6`.
Compatibility with every client has not been independently tested.

## Two chats and different models

Chats may prepare and submit jobs concurrently. One broker executes CAD
operations **one at a time**, in FIFO admission order. Its lock covers the whole
submitted tool, `execute_cad_plan`, `run_transaction` or trusted synchronous
script, including supported verification, rollback and save/export steps.

In `.6`, opt in with `coordination.target_document`: after waiting, the executor
selects the specified **already open, saved model** before preflight. A real
switch requires both the current and target models to be clean and their edit
states to be known and safe; unknown state, unsaved changes, active sketches or
in-context edits prevent switching. The selector never opens, saves or closes
models, changes configurations, or confirms dialogs. A target configuration is
an assertion, not a request to change it.

Finish dependent steps, leave sketch/edit mode, verify and explicitly save within
the submitted operation before yielding to another model. A dirty model left
between requests prevents a switch; a same-model continuation may retain its
sketch/dirty state subject to normal tool preflight. An assembly must always be
editing itself, even when already active.

`coordination.expected_document` retains its compare-only behavior: it checks the
active document after waiting and never activates it. Do not supply both fields.
Pin document-specific reads as well as writes. There is no reservation between
separate requests; submit dependent steps as one plan. Manual UI, external macros
and other COM applications do not respect this lock. See the complete
[selection and recovery contract](docs/DOCUMENT_TARGET.md).

Example arguments for `execute_cad_plan` below **modify and save** the model.
They are not an installation smoke test. The saved `DemoA.SLDPRT` must already
be open and clean, and have one solid named `Body1`. Replace both IDs for each new
operation; retain the request ID for transport retries/result lookup.

```json
{
  "plan_id": "demo-a-rename-001",
  "operations": [
    {"op": "rename_body", "args": {"old_name": "Body1", "new_name": "Housing"}},
    {"op": "body_volume", "args": {"name": "Housing", "unit": "mm"}}
  ],
  "transaction": {"checkpoint_before": true, "rollback_on_failure": true},
  "invariants": {"solid_body_count": 1, "required_bodies": ["Housing"], "no_modal_dialog": true},
  "coordination": {
    "request_id": "demo-a-request-001",
    "target_document": {"path": "C:\\CAD-Test\\DemoA.SLDPRT", "document_type": 1},
    "wait_timeout_sec": 25
  }
}
```

The second chat supplies its own IDs and target path, for example
`C:\CAD-Test\DemoB.SLDPRT`. Each whole plan waits for exclusive ownership, selects
its model and completes before the next begins. Unsafe or ambiguous selection
rejects before geometry; uncertain activation fences the queue for inspection.
Rollback is operation-specific and may be refused before mutation when reliable
undo or snapshots are unavailable. This is not a universal undo facility.

## Install the queue/core runtime

Requirements: Windows 10/11 with an interactive Explorer desktop, licensed
SolidWorks 2025, and Python 3.11+. Python **3.14** is the locally qualified
runtime. SolidWorks installers, licenses and proprietary templates are excluded.
Use a fixed local source directory outside cloud synchronization. Keep activated
sources and their environment unchanged while clients are using them.

Clone the tagged source release from
[estasi/SolidworksMCP-Local](https://github.com/estasi/SolidworksMCP-Local). Optional
model weights and their LFS pointers are excluded; Git LFS is not required for
this queue/core installation.

```powershell
git clone --branch v2026.10.18.6 https://github.com/estasi/SolidworksMCP-Local.git C:\MCP-Releases\2026.10.18.6
Set-Location C:\MCP-Releases\2026.10.18.6
py -3.14 -m venv C:\MCP-Runtime\sw2025-2026.10.18.6
$queuePython = 'C:\MCP-Runtime\sw2025-2026.10.18.6\Scripts\python.exe'
& $queuePython -m pip install -r requirements-queue.txt -c constraints-core-py314-win.txt
& $queuePython -m pip check
```

The direct core dependencies are pinned. See [dependency notices](THIRD_PARTY_NOTICES.md)
for the tested transitive inventory. These constraints describe Windows/Python
3.14 only, not a cross-platform lock. Optional raster/vector dependencies are
separate. Installing core packages does not qualify those backends.

### Bind the intended SolidWorks process

Start SolidWorks manually. For an upgrade, finish CAD work, stop submissions,
disconnect old MCP sessions normally and drain/stop the existing broker using
[the operations procedure](docs/QUEUE_OPERATIONS.md). Do not mix old/new clients
or delete state to bypass recovery.

```powershell
& $queuePython -m solidworks_mcp.coordination.cli doctor
Get-Process SLDWORKS | Select-Object Id, ProcessName
& $queuePython -m solidworks_mcp.coordination.cli activate --target-pid <VERIFIED-PID> --expected-major 33 --acknowledge-idle-session
```

Replace `<VERIFIED-PID>` with the intended numeric PID after confirming the
session is idle. Activation binds its generation and source path; it does not
change client configuration, launch SolidWorks or reset the journal. Native API
identity is checked when attaching. Multiple SolidWorks instances may make COM
attachment ambiguous; a mismatch fails closed.

### Configure a local MCP client

This is a common JSON configuration format; adapt its wrapper to your client's
format. Keep absolute command, arguments and working directory consistent.
Point every participating client at this same release.

```json
{
  "mcpServers": {
    "SolidworksMCP": {
      "command": "C:\\MCP-Runtime\\sw2025-2026.10.18.6\\Scripts\\python.exe",
      "args": ["-u", "C:\\MCP-Releases\\2026.10.18.6\\solidworks_mcp_server.py"],
      "cwd": "C:\\MCP-Releases\\2026.10.18.6"
    }
  }
}
```

Reconnect through the client's supported lifecycle. Check the handshake version
and call `get_queue_status`: an open, idle native queue is the expected control
state. Status alone does not prove CAD correctness; qualify native operations
on disposable synthetic models. Native clients auto-start the broker through
the verified existing Explorer desktop. Optional operator prestart:

```powershell
& $queuePython -m solidworks_mcp.coordination.launch --backend native --desktop
& $queuePython -m solidworks_mcp.coordination.cli status
```

## Journal and recovery

The private SQLite WAL journal preserves FIFO sequence, request identities,
outcomes and recovery evidence under
`%LOCALAPPDATA%\SolidworksMCP\coordination\v1\<sessionhash>`.
The broker outlives MCP frontends; this does not promise survival of Windows
logoff/shutdown. Uncertain mutations are never automatically replayed.

- Keep a unique `coordination.request_id` until the outcome is known. After a
  lost response, call `get_queue_request` with that ID, instead of inventing a
  new ID for a possibly completed change.
- A client wait timeout does not stop CAD. An execution overrun fences the queue;
  it does not interrupt COM or release ownership while that call can still act.
- `cancel_queued_job` only cancels waiting jobs. `queue_events` and
  `get_queue_status` read control state without COM.
- Recovery requires inspecting CAD/files, proving execution returned or the old
  CAD generation ended, and explicitly acknowledging the affected jobs. Follow
  [recovery and rollback](docs/QUEUE_OPERATIONS.md#recovery-after-an-uncertain-operation).

Do not publish journals, endpoint credentials, client configuration, recovery
bundles, logs, screenshots or working models. AI clients may send returned tool
data to their chosen model provider; this server does not control that provider's
retention policy.

## Other tools and optional models

Inherited tools cover sketches, features, bodies, references, dimensions,
transactions, views, exports and vectorization. Live MCP discovery provides the
schemas. Raw Python requires `coordination.trusted_python=true` and reviewed
synchronous code; static checks are **not a sandbox**. No detached CAD work or
independent COM connections are allowed. Automatic dialog confirmation and
automatic recovery are disabled in the queued executor.

Native tool dimensions normally use millimetres; raw COM uses metres. Verify
body count, dimensions and volume rather than relying only on a success flag.

This source distribution excludes optional line-art weight binaries **and their
Git LFS pointers**. Their original paths, pointer hashes and expected object
identities are recorded in `publication-manifest.json` and the model notices.
Do not replace missing weights or disable hash checks. Exact export provenance
and redistribution rights require separate review: code and training-data terms
are distinct. See [model notices](solidworks_mcp/models/lineart/LICENSES.md) and
[third-party notices](THIRD_PARTY_NOTICES.md). CUDA/vectorization and SW2026/2027
are outside this queue qualification.

## Development, attribution and publication

Run offline tests in an isolated checkout/environment, never against working
CAD models. The CI/core route uses `requirements-queue.txt` and tests named
`test_queue_*.py`. The complete inherited suite additionally needs the five
scientific packages in `requirements-testextras.txt`, not GPU packages or weights:

```powershell
# In a separate test environment, from the source root:
python -m pip install -r requirements-queue.txt -r requirements-testextras.txt -c constraints-testextras-py314-win.txt
python -m pip check
python -m unittest discover -s tests -p "test_*.py"
```

Historical `.4` fresh Windows/Python 3.14 qualification: 423 passed and one explicit Windows-job
capability skip, plus 9 separate broker tests passed. The native entry points
were blocked by the retained offline qualification runner; no CAD was contacted.
Earlier failed core-only/full-suite and station TEMP runs remain recorded in
[validation](docs/QUEUE_VALIDATION.md). The constraints capture this platform;
they are not a universal Python/OS lock. See [publication review](docs/GITHUB_READINESS.md).

`SolidworksMCP` remains the MCP server name, `solidworks_mcp` the Python package;
existing tool/protocol identifiers are retained. Local queue/reliability work
is independent of upstream. The MIT license does not grant a SolidWorks license
or replace third-party asset terms. Runtime data and local Git history are not
part of this source-only GitHub distribution.
