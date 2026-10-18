"""MCP schemas for durable queue controls and operation identity guards."""

import copy
from mcp.types import Tool


def _schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {},
            "required": required or [], "additionalProperties": False}


QUEUE_TOOLS = [
    Tool(name="get_queue_status",
         description="Read the shared CAD queue, owner and recovery fence without COM.",
         inputSchema=_schema()),
    Tool(name="get_queue_request",
         description="Recover a durable job by its request ID after a lost response. "
                     "Reads only the journal and never contacts CAD or repeats work.",
         inputSchema=_schema({"request_id": {"type": "string", "minLength": 1}},
                             ["request_id"])),
    Tool(name="cancel_queued_job",
         description="Cancel an operation only while it is waiting. Running COM calls "
                     "cannot safely be cancelled or released by a timeout.",
         inputSchema=_schema({"job_id": {"type": "string"}}, ["job_id"])),
    Tool(name="queue_events",
         description="Read the durable journal events without contacting CAD. "
                     "Arguments and model source values are not included in event summaries.",
         inputSchema=_schema({"job_id": {"type": "string"},
                              "limit": {"type": "integer", "minimum": 1,
                                        "maximum": 1000, "default": 100}})),
    Tool(name="acknowledge_queue_recovery",
         description="Acknowledge exactly the listed uncertain jobs after review. "
                     "The broker still requires proof that old execution returned or "
                     "the original CAD process ended; it never blindly replays jobs.",
         inputSchema=_schema({
             "expected_job_ids": {"type": "array", "items": {"type": "string"}},
             "note": {"type": "string", "minLength": 1},
             "operator": {"type": "string", "minLength": 1}},
             ["expected_job_ids", "note", "operator"])),
]

_COORDINATION = _schema({
    "request_id": {"type": "string", "description":
                   "Stable unique ID. Reuse on a transport retry to avoid duplicate changes."},
    "trusted_python": {"type": "boolean", "default": False,
                       "description": "Explicitly allow trusted synchronous raw Python. "
                                      "Scripts must not create threads/processes, rebind COM "
                                      "or leave background work. Static checks are not a sandbox."},
    "expected_document": {"oneOf": [
        {"type": "string", "description": "Absolute path of the expected active document"},
        _schema({"path": {"type": "string"}, "title": {"type": "string"},
                 "allow_unsaved_document": {"type": "boolean", "default": False},
                 "document_type": {"type": "integer", "enum": [1, 2, 3]},
                 "configuration": {"type": "string"}})],
        "description": "Checked after queue wait. Required for active-document changes. "
                       "Unsaved files need an exact title and allow_unsaved_document=true."},
    "target_document": {"oneOf": [
        {"type": "string", "description": "Absolute path of an already open saved model"},
        _schema({"path": {"type": "string"},
                 "document_type": {"type": "integer", "enum": [1, 2, 3]},
                 "configuration": {"type": "string", "minLength": 1}}, ["path"])],
        "description": "Opt in to selecting an already open saved model inside this queued operation. "
                       "Mutually exclusive with expected_document. Never opens/saves/closes models, "
                       "rebuilds on activation or changes configuration. Switching requires clean "
                       "saved active and target documents and no sketch/in-context edit. "
                       "Type/configuration are assertions only; ambiguous names and unknown states reject."},
    "queue_timeout_sec": {"type": "number", "exclusiveMinimum": 0, "maximum": 86400,
                          "description": "Maximum time waiting before execution starts"},
    "execution_timeout_sec": {"type": "number", "exclusiveMinimum": 0, "maximum": 86400,
                              "description": "On overrun fence the queue; never interrupt COM"},
    "wait_timeout_sec": {"type": "number", "minimum": 0, "maximum": 60,
                         "description": "How long this request waits for a result; "
                                        "the queued operation survives client disconnect"},
})


def augment_queue_schemas(tools):
    from .protocol import CONTROL_TOOLS
    for tool in tools:
        if tool.name in CONTROL_TOOLS:
            continue
        tool.inputSchema.setdefault("properties", {})["coordination"] = copy.deepcopy(_COORDINATION)
        if tool.name in {"run_transaction", "execute_cad_plan"}:
            tool.description += (" The queue owns the complete operation, including "
                                 "preflight, all steps, rollback and final verification.")
        if tool.name == "execute_python":
            tool.description += (" Trusted synchronous scripts only: no threads, "
                                 "processes, background tasks or independent COM connections. "
                                 "Static checks are not a security sandbox.")
    return tools
