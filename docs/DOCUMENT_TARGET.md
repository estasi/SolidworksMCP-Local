# Queued document selection — 2026.10.18.6 accepted part-targeting release

This opt-in extension selects an already open model after queue waiting, inside
the same broker-owned CAD operation as preflight, all plan steps, verification
and save. Two chats can submit jobs for different models; one STA executor runs
those complete operations sequentially. This is not simultaneous native CAD
execution, and it does not reserve a model between requests.

Native part-targeting acceptance for `.6` passed against the unchanged runtime
commit. Two actual MCP clients performed six alternating changes on two synthetic
saved parts, two ordinary targeted CAD plans and independent save/reopen geometry
checks. See the [sanitized release validation](../RELEASE_VALIDATION.md).
Assembly/drawing switching, multiple SW instances and RAM admission management
remain outside that accepted scope. Existing saved models must still satisfy the
preconditions below. Historical `.5` failure and `.4` acceptance remain separate.

## Request contract

Use `coordination.target_document` on each document-specific read or write that
should select its own model. It accepts an absolute saved Windows path, or an
object with required `path` and optional `document_type` (1 part, 2 assembly,
3 drawing) and exact `configuration`. The file must already be open. Paths are
compared using Windows normalization and case folding; aliases and linked paths
are not treated as interchangeable without matching the actual opened path.

`expected_document` remains a compare-only guard, with its `.4` behavior intact.
Supplying both fields is an error, even if they name the same model. A target
configuration is an assertion; this feature never changes configurations. Tools
that create/open documents or operate without an active document reject targeting.

Example arguments for `execute_cad_plan`, assuming two saved, clean models are
already open and this model contains a single body named `Body1`:

```json
{
  "plan_id": "<new unique plan ID>",
  "operations": [
    {"op": "rename_body", "args": {"old_name": "Body1", "new_name": "Housing"}},
    {"op": "body_volume", "args": {"name": "Housing", "unit": "mm"}}
  ],
  "transaction": {"checkpoint_before": true, "rollback_on_failure": true},
  "invariants": {"solid_body_count": 1, "required_bodies": ["Housing"]},
  "coordination": {
    "request_id": "<new UUID retained for transport retries>",
    "target_document": {
      "path": "C:\\CAD\\model-A.SLDPRT",
      "document_type": 1,
      "configuration": "Default"
    },
    "wait_timeout_sec": 25
  }
}
```

The second chat uses its own IDs and `C:\CAD\model-B.SLDPRT`. This example changes
and saves the selected model; it is not a read-only connectivity test. Existing
checkpoint and rollback capability limits still apply. Poll a returned job until
completion. After a lost response, look up its original `request_id`; do not
replay an uncertain mutation under a new ID.

## Selection and safety boundary

Before activation the runtime requires a complete, stable document inventory,
an unambiguous path and activation name, a ready process-scoped CAD window,
no active command or PropertyManager edit, no active sketch, no blocking state,
and no in-context assembly edit in the relevant documents when a switch is needed.
Unknown state rejects
the job before geometry starts. Duplicate basenames at different paths and
colliding unsaved titles are rejected before an actual switch rather than guessed.

A real switch requires both the previously active model (if any) and the target
to be saved and clean. Dirty saved models and an unsaved active document must be
saved explicitly before switching; no automatic save occurs. When the target is
already active, no activation is performed: dirty saved state and an existing
sketch/command edit are left to normal tool preflight, just as with a compare-only
guard. This permits a targeted create-sketch/draw/close-sketch sequence on the
same model. Complete dependent edits, exit edit/sketch mode, verify and explicitly
save within the whole submitted operation before yielding to another model;
the selector does not perform that save for the caller. Strict identity,
inventory and UI readiness checks still apply;
for assemblies the actual in-context edit target must always be that same
assembly, even when already active. Unknown or different edit identity rejects
the job before geometry; a component edit is not treated as a same-model sketch.
The operation's existing checkpoint policy is unchanged. Unsaved target documents
are unsupported.

The activation call uses the full filename including extension, ignores the
user's rebuild preference and explicitly requests no rebuild. The returned
document and actual active document are checked against the full path, type and
current configuration. The inventory's paths, titles, types, configurations,
dirty flags and application lifetime/visibility settings must remain unchanged.
Command, sketch, blocking and in-context edit states are checked again after
activation, before dispatching the requested operation.
These checks are state evidence, not a hash of every model's in-memory geometry.

The target is left active after the job. The selection layer never opens, saves,
closes or rebuilds any model, changes configurations, confirms a dialog, or tries
to restore a previous selection. The requested CAD operation may save or close
its selected target according to that tool's contract. It must not act on a
different model behind the selection layer's back.

An ordinary precondition failure rejects that job without a new recovery fence.
Once activation was attempted, any error, warning (including a rebuild-needed
warning), unreadable return, unexpected document or preservation failure fences
the queue. Nothing is replayed automatically. Existing timeout/watchdog and
operator reconciliation rules apply. Manual UI edits, macros, external COM and
trusted scripts can bypass the coordination contract; keep the CAD session
controlled while queued operations run.

## Idle-command normalization in build .6

The first native `.5` switching attempt rejected an idle SW2025 session before
activation. A retained read-only probe observed a void return, a written command
ID of -3, an untouched PropertyManager title, and `IsUiActive=False`. The command
API documents that flag as its activity indicator; the title is descriptive and
need not be rewritten when no PropertyManager page is active.

Build `.6` requires an explicitly false activity flag and a written integer
command ID. It accepts a string (including an unchanged title sentinel) or null
title; -3 is not hardcoded as the only idle ID. Unwritten command/flag outputs,
active commands and malformed returns still reject before activation/geometry.
This correction does not change inventory, sketch, assembly edit-target, UI,
activation preservation or recovery checks. It requires separate native `.6`
qualification; the `.5` failed run is not converted into a pass.

## API basis

- [`ISldWorks.ActivateDoc3`](https://help.solidworks.com/2023/english/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.ISldWorks~ActivateDoc3.html)
  selects a loaded document and returns activation status. The documented API
  predates SW2025; its filename-extension advice and warning behavior inform the
  strict selection/read-back policy.
- [`swRebuildOnActivation_e`](https://help.solidworks.com/2024/english/api/swconst/SolidWorks.Interop.swconst~SolidWorks.Interop.swconst.swRebuildOnActivation_e.html?id=84c6de35008147d98889b766dbc1b847)
  defines `swDontRebuildActiveDoc=1`; zero would request a user decision.
- [`ISldWorks.GetRunningCommandInfo`](https://help.solidworks.com/2019/english/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.ISldWorks~GetRunningCommandInfo.html)
  exposes active commands and PropertyManager pages, including non-modal edits.
- [`IModelDoc2.GetBlockingState`](https://help.solidworks.com/2026/english/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.IModelDoc2~GetBlockingState.html)
  and [`swBlockingStates_e`](https://help.solidworks.com/2026/English/api/swconst/SolidWorks.Interop.swconst~SolidWorks.Interop.swconst.swBlockingStates_e.html)
  provide the explicit no-block requirement. No blocking state is reset.
- [`IAssemblyDoc.GetEditTarget`](https://help.solidworks.com/2017/english/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.IAssemblyDoc~GetEditTarget.html)
  distinguishes the active assembly from a component being edited in context.

## Separate native acceptance gate

Pin all control clients and actual MCP stdio frontends to the verified starting
broker owner using `SOLIDWORKS_MCP_EXPECTED_OWNER_ID`, as described in
[the operations guide](QUEUE_OPERATIONS.md#pin-qualification-clients-to-one-broker-owner).
Losing or replacing that owner must stop the qualification, never auto-start a
new broker and continue.

In an authorized idle maintenance window, use two disposable saved SW2025 models
with distinct filenames and verified configurations. Retain the initial strict
inventory, then have two independent MCP clients submit repeated alternating
targeted plans. Check correct paths and configurations, FIFO/non-overlap, exact
per-model changes, saved/reopened geometry, and preservation of all other models.
Exercise an ordinary target mismatch and unsafe edit without changing user work;
retain the resulting failed jobs. Verify clean cleanup and transport reconnect.
Do not deliberately crash SW or inject uncertain activation into a user's live
session merely to obtain coverage. Such failures remain synthetic test evidence
unless a separately authorized qualification plan establishes them safely.
