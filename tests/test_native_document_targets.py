"""Offline qualification-harness contracts; never attach COM or run native CAD."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from scripts import qualify_native_document_targets as q
from solidworks_mcp.coordination.cad_backend import validate_script


def target(index=0):
    return {"path": rf"C:\synthetic\model-{index}.SLDPRT", "document_type": 1, "configuration": "Default"}


def state(index=0):
    t = target(index)
    return {"key": q._key(t["path"]), "path": t["path"], "title": f"model-{index}.SLDPRT",
            "type": 1, "configuration": "Default", "dirty": False}


def metrics(index=0, step=0):
    depth = .020 + (step+1)*.001
    return {"depth_m": depth, "body_count": 1, "bbox_m": [-.01, -.01, 0, .01, .01, depth],
            "volume_m3": .02*.02*depth, "solidworks_revision": "33.2.0",
            "before_m": depth-.001, "started_ns": (step*2+index)*100,
            "ended_ns": (step*2+index)*100+80,
            "identity_before": state(index), "identity_after": state(index)}


def operations():
    result=[]
    for step in range(3):
        for index in range(2):
            seq=step*2+index
            result.append({"worker": index, "client_pid": 100+index, "step": step,
                "metrics": metrics(index, step), "job": {"seq": seq+1, "state": "completed",
                    "queued_at": seq*10-5, "started_at": seq*10, "finished_at": seq*10+8,
                    "document_selection": {"selected": True}}})
    return result


def inventory(documents, active=None):
    return {"strict": True, "count": len(documents), "documents": documents,
            "active_document": active, "session": {"user_control": True, "visible": True}}


class NativeDocumentTargetsHarnessTests(unittest.TestCase):
    def test_nested_exception_group_retains_concrete_failure_job_and_other_leaves(self):
        job={"job_id":"first","state":"failed","native_result":{"code":"UNSAFE_EDIT"}}
        error=ExceptionGroup("async wrapper",[RuntimeError("transport"),
            ExceptionGroup("nested",[q.Failure("NATIVE_JOB_FAILED",job)])])
        details=q._exception_details(error)
        self.assertEqual(details["wrapper_type"],"ExceptionGroup")
        self.assertEqual(details["code"],"NATIVE_JOB_FAILED")
        self.assertEqual(details["job"],job)
        self.assertEqual(details["leaves"][1]["group_path"],[1,0])
        self.assertEqual(len(details["leaves"]),2)
        self.assertEqual(json.loads(json.dumps(details)),details)
        cancelled=BaseExceptionGroup("wrapper",[KeyboardInterrupt(),q.Failure("CAD_UNCERTAIN",job)])
        self.assertEqual(q._exception_details(cancelled)["code"],"CAD_UNCERTAIN")

    def test_failure_observer_retains_later_completed_job_using_only_lookup(self):
        owner="a"*32
        client=Mock(auto_start=False,expected_owner_id=owner)
        scheduled=[{"request_id":"first","phase":"A"},{"request_id":"second","phase":"B"}]
        failed={"id":"jobA","request_id":"first","seq":105,"state":"failed",
                "result":{"native_error":"UNSAFE_EDIT"}}
        running={"id":"jobB","request_id":"second","seq":106,"state":"running"}
        completed={**running,"state":"completed","result":{"depth_m":.021}}
        client.request.side_effect=[failed,running,failed,completed]
        evidence={"broker_owner_id":owner,"jobs":[]}
        with patch.object(q.time,"sleep"):
            q._observe_dispatched_jobs(client,scheduled,evidence,lambda:None,timeout=10)
        observed=evidence["jobs_after_failure"]
        self.assertTrue(observed["complete"])
        self.assertEqual(observed["requests"]["first"]["job"],failed)
        self.assertEqual(observed["requests"]["second"]["job"],completed)
        self.assertEqual([x["state"] for x in evidence["jobs"]],["failed","completed"])
        self.assertTrue(all(call.args[0]=="lookup" for call in client.request.call_args_list))
        client.submit.assert_not_called();client.call_tool.assert_not_called();client.wait.assert_not_called()

    def test_failure_observer_waits_for_late_admission_until_workers_closed(self):
        owner="a"*32
        client=Mock(auto_start=False,expected_owner_id=owner)
        job={"id":"job","request_id":"stable","seq":1,"state":"completed","result":{"success":True}}
        client.request.side_effect=[q.Failure("JOB_NOT_FOUND"),job]
        closed=Mock(side_effect=[True,True])
        evidence={"broker_owner_id":owner,"jobs":[]}
        with patch.object(q.time,"sleep"):
            q._observe_dispatched_jobs(client,[{"request_id":"stable","phase":"B"}],evidence,
                lambda:None,timeout=10,workers_closed=closed)
        self.assertEqual(client.request.call_count,2)
        self.assertEqual(evidence["jobs_after_failure"]["requests"]["stable"]["job"],job)
        self.assertTrue(evidence["jobs_after_failure"]["complete"])

    def test_missing_request_after_frontend_closure_remains_unknown_without_skip_proof(self):
        owner="a"*32
        client=Mock(auto_start=False,expected_owner_id=owner)
        client.request.side_effect=q.Failure("JOB_NOT_FOUND")
        evidence={"broker_owner_id":owner}
        with patch.object(q.time,"monotonic",side_effect=[0,2]):
            q._observe_dispatched_jobs(client,[{"request_id":"stable","phase":"B"}],evidence,
                lambda:None,timeout=1,workers_closed=lambda:True)
        outcome=evidence["jobs_after_failure"]
        self.assertFalse(outcome["complete"])
        self.assertFalse(outcome["native_outcomes_known"])
        self.assertEqual(outcome["requests"]["stable"]["admission"],"not_observed")
        self.assertEqual(outcome["errors"][0]["code"],"FAILURE_OBSERVATION_DEADLINE")

    def test_explicit_worker_skip_before_call_tool_is_proven_no_submission(self):
        owner="a"*32
        client=Mock(auto_start=False,expected_owner_id=owner)
        client.request.side_effect=q.Failure("JOB_NOT_FOUND")
        evidence={"broker_owner_id":owner}
        q._observe_dispatched_jobs(client,[{"request_id":"stable","phase":"B"}],evidence,
            lambda:None,timeout=1,workers_closed=lambda:True,not_submitted_ids=lambda:{"stable"})
        self.assertTrue(evidence["jobs_after_failure"]["complete"])
        self.assertTrue(evidence["jobs_after_failure"]["native_outcomes_known"])
        self.assertEqual(evidence["jobs_after_failure"]["requests"]["stable"]["admission"],"not_submitted")
        self.assertEqual(client.request.call_count,1)

    def test_failure_observer_no_owner_fallback_or_transport_retry(self):
        owner="a"*32
        scheduled=[{"request_id":"stable","phase":"A"}]
        for client in [Mock(auto_start=True,expected_owner_id=owner),
                       Mock(auto_start=False,expected_owner_id="b"*32)]:
            evidence={"broker_owner_id":owner}
            q._observe_dispatched_jobs(client,scheduled,evidence,lambda:None,timeout=1)
            client.request.assert_not_called()
            self.assertFalse(evidence["jobs_after_failure"]["complete"])
        client=Mock(auto_start=False,expected_owner_id=owner)
        client.request.side_effect=q.Failure("BROKER_OWNER_CHANGED")
        evidence={"broker_owner_id":owner}
        q._observe_dispatched_jobs(client,scheduled,evidence,lambda:None,timeout=1)
        self.assertEqual(client.request.call_count,1)
        self.assertEqual(evidence["jobs_after_failure"]["errors"][0]["code"],"BROKER_OWNER_CHANGED")

    def test_failure_observer_timeout_keeps_pending_unknown_and_original_summary(self):
        owner="a"*32
        client=Mock(auto_start=False,expected_owner_id=owner)
        client.request.return_value={"id":"job","request_id":"stable","seq":1,"state":"running"}
        evidence={"broker_owner_id":owner,"jobs":[]}
        with patch.object(q.time,"monotonic",side_effect=[0,2]):
            q._observe_dispatched_jobs(client,[{"request_id":"stable","phase":"B"}],evidence,
                lambda:None,timeout=1)
        self.assertFalse(evidence["jobs_after_failure"]["complete"])
        self.assertEqual(evidence["jobs_after_failure"]["errors"][0]["code"],"FAILURE_OBSERVATION_DEADLINE")
        self.assertEqual(evidence["jobs"][0]["state"],"running")
        q._retain_job(evidence,{"job_id":"job","state":"completed"})
        q._retain_job(evidence,{"job_id":"job","state":"running"})
        self.assertEqual(evidence["jobs"][0]["state"],"completed")

    def test_observed_recovery_fence_is_not_a_known_native_outcome(self):
        owner="a"*32
        client=Mock(auto_start=False,expected_owner_id=owner)
        client.request.return_value={"id":"job","request_id":"stable","seq":1,
            "state":"recovery_required","reason":"native outcome uncertain"}
        evidence={"broker_owner_id":owner}
        q._observe_dispatched_jobs(client,[{"request_id":"stable","phase":"B"}],evidence,
            lambda:None,timeout=1)
        self.assertTrue(evidence["jobs_after_failure"]["complete"])
        self.assertFalse(evidence["jobs_after_failure"]["native_outcomes_known"])
        self.assertEqual(evidence["jobs"][0]["state"],"recovery_required")

    def test_two_client_failure_records_already_admitted_later_completion(self):
        self._assert_failed_pair()

    def test_failure_report_write_does_not_replace_original_native_failure(self):
        self._assert_failed_pair(failing_writer=True)

    def test_failure_handle_close_does_not_replace_original_native_failure(self):
        self._assert_failed_pair(failing_close=True)

    def _assert_failed_pair(self, failing_writer=False, failing_close=False):
        owner="a"*32
        channel=queue.Queue()
        jobs=[]
        for index,state_name in enumerate(["failed","completed"]):
            jobs.append({"id":f"job{index}","request_id":f"run-model-{index}-step-0",
                "seq":105+index,"state":state_name,"result":{"native_result":state_name}})
        failed=q.base._summary(jobs[0],"A")
        failed["native_result"]=jobs[0]["result"]
        class Commands:
            def __init__(self,index):self.index=index;self.sent=[]
            def put(self,value):
                self.sent.append(value)
                if value is None:
                    channel.put({"kind":"closed","worker":self.index,"client_pid":100+self.index})
                    return
                if self.index==1:
                    details=q._exception_details(ExceptionGroup("anyio",[q.Failure("NATIVE_JOB_FAILED",failed)]))
                    channel.put({"kind":"failure","worker":0,"client_pid":100,
                        "code":details["code"],"job":failed,"exception":details})
                channel.put({"kind":"admitted","worker":self.index,"client_pid":100+self.index,
                    "job":q.base._summary(jobs[self.index],"admission"),"step":value})
                if self.index==1:
                    channel.put({"kind":"completed","worker":1,"client_pid":101,"step":value,
                        "job":q.base._summary(jobs[1],"B"),"metrics":{"depth_m":.021}})
            def close(self):pass
        commands=[Commands(0),Commands(1)]
        class Process:
            def __init__(self,index):self.index=index;self.pid=None;self.exitcode=0
            def start(self):
                self.pid=200+self.index
                channel.put({"kind":"ready","worker":self.index,"client_pid":100+self.index,
                    "owner_id":owner,"version":q.BUILD})
            def join(self,*args):pass
        context=Mock()
        context.Queue.side_effect=[channel,*commands]
        context.Process.side_effect=[Process(0),Process(1)]
        context.Event.return_value=threading.Event()
        def close():
            if failing_close:raise OSError("Synthetic close failure")
        channel.close=close
        control=Mock(auto_start=False,expected_owner_id=owner)
        control.request.side_effect=lambda action,payload:next(j for j in jobs if j["request_id"]==payload["request_id"])
        factory=Mock(return_value=control)
        evidence={"broker_owner_id":owner,"jobs":[],"checks":{}}
        def save():
            if failing_writer and any(x["kind"]=="failure" for x in evidence.get("client_events",[])):
                raise OSError("Synthetic evidence write failure")
        with patch.object(q.multiprocessing,"get_context",return_value=context), \
                patch.object(q.base,"_imports",return_value=(factory,)):
            with self.assertRaises(q.Failure) as stopped:
                q._two_stdio_clients("run",[target(0),target(1)],30,.5,Path("unused"),evidence,save)
        self.assertEqual(stopped.exception.code,"NATIVE_JOB_FAILED")
        self.assertEqual(stopped.exception.job,failed)
        factory.assert_called_once_with(auto_start=False,expected_owner_id=owner)
        self.assertEqual([x["state"] for x in evidence["jobs"]],["failed","completed"])
        self.assertTrue(evidence["jobs_after_failure"]["complete"])
        self.assertEqual(evidence["jobs_after_failure"]["requests"][jobs[1]["request_id"]]["job"],jobs[1])
        self.assertEqual([x for c in commands for x in c.sent if x is not None],[0,0])
        self.assertEqual(len(evidence["scheduled_requests"]),2)
        self.assertTrue(all(call.args[0]=="lookup" for call in control.request.call_args_list))
        self.assertFalse(evidence["checks"])
        if failing_writer:
            self.assertGreater(evidence["failure_evidence_write_errors"][0]["count"],0)
            self.assertEqual(evidence["failure_evidence_write_errors"][0]["exception"]["code"],"OSError")
        if failing_close:self.assertEqual(evidence["failure_handle_close_error"]["code"],"OSError")

    def test_help_missing_ack_and_invalid_bounds_are_com_free(self):
        cases=[(["--help"],0),(["--output-dir","never-create"],2),
            (["--output-dir","never-create","--acknowledge-idle-session","--delay-sec","nan"],2)]
        for argv, code in cases:
            with self.subTest(argv=argv), patch.object(q.base,"_imports",side_effect=AssertionError("No native import")), \
                    redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as stopped:
                    q.main(argv)
                self.assertEqual(stopped.exception.code,code)

    def test_reusing_output_directory_is_refused_before_imports(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(q.base,"_imports",side_effect=AssertionError("No native import")), \
                redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                q.main(["--output-dir",directory,"--acknowledge-idle-session"])
            self.assertEqual(stopped.exception.code,2)

    def test_generated_scripts_use_trusted_synchronous_contract(self):
        for code in [q._seed_code(target()),q._increment_code(target(),"synthetic",.5),q._verification_code(target())]:
            validate_script(code)
            compile(code,"<offline-target-qualification>","exec")
            self.assertNotIn("ActivateDoc",code)
            self.assertNotIn("OpenDoc",code)
            self.assertNotIn("CloseDoc",code)

    def test_native_identity_oracle_rejects_different_document_and_configuration(self):
        doc=object()
        actual=state()
        automation=Mock()
        automation._strict_document_state.side_effect=lambda _:dict(actual)
        context={"doc":doc,"sw":object(),"automation":automation,"com_get":lambda *_:doc}
        exec(q._identity_code(target()),context)
        self.assertEqual(context["identity_before"],state())
        for changes in ({"path":target(1)["path"]},{"configuration":"Other"},{"type":2}):
            actual.update(changes)
            with self.assertRaisesRegex(RuntimeError,"WRONG_ACTIVE_DOCUMENT"):
                context["qualification_identity"]()
            actual=state()

    def test_result_identity_check_requires_clean_saved_exact_model(self):
        value=metrics()
        q._check_identity(value,target())
        for changes in ({"path":target(1)["path"]},{"configuration":"Other"},{"dirty":True},{"type":2}):
            changed=copy.deepcopy(value)
            changed["identity_after"].update(changes)
            with self.subTest(changes=changes),self.assertRaises(q.Failure):
                q._check_identity(changed,target())

    def test_ordinary_plan_only_reads_geometry_and_saves_owned_target_without_checkpoint(self):
        args = q._plan_arguments("unique-plan", .023)
        self.assertEqual(args["operations"], [{"op":"body_volume", "args":{"unit":"m"}}])
        self.assertEqual(args["transaction"], {"checkpoint_before":False,"rollback_on_failure":False})
        self.assertFalse(args["allow_unsaved_document"])
        self.assertNotIn("save_path", args)
        self.assertEqual(args["invariants"]["solid_body_count"], 1)
        result={"success":True,"data":{"committed":True,"invariants":"passed",
            "save_result":{"success":True},"document_selection":{"selected":True,
                "document_type":1,"configuration_verified":True},
            "steps":[{"success":True,"data":{"operation":"body_volume","unit":"m",
                "bodies":[{"volume_exact":.020*.020*.023}]}}]}}
        q._check_plan(result,.023)
        for field, value in [("committed",False),("save_result",{"success":False}),
                             ("document_selection",{}),("invariants","failed")]:
            changed=copy.deepcopy(result);changed["data"][field]=value
            with self.subTest(field=field),self.assertRaises(q.Failure):q._check_plan(changed,.023)
        for volume in (.020**3,float("nan"),None):
            changed=copy.deepcopy(result)
            changed["data"]["steps"][0]["data"]["bodies"][0]["volume_exact"]=volume
            with self.subTest(volume=volume),self.assertRaises(q.Failure):q._check_plan(changed,.023)

    def test_six_alternating_jobs_prove_distinct_clients_fifo_geometry_and_overlap(self):
        ordered=q._check_operations(list(reversed(operations())),[target(0),target(1)])
        self.assertEqual([x["worker"] for x in ordered],[0,1,0,1,0,1])

    def test_same_client_missing_operation_wrong_target_lost_increment_overlap_reject(self):
        variants=[]
        same=operations()
        for item in same:item["client_pid"]=100
        variants.append(same)
        variants.append(operations()[:-1])
        wrong=operations();wrong[3]["metrics"]["identity_after"]=state(0);variants.append(wrong)
        lost=operations();lost[2]["metrics"]["before_m"]-=.001;variants.append(lost)
        overlap=operations();overlap[1]["metrics"]["started_ns"]=10;variants.append(overlap)
        order=operations();order[1]["job"]["seq"],order[2]["job"]["seq"]=3,2;variants.append(order)
        unselected=operations();unselected[1]["job"]["document_selection"]={};variants.append(unselected)
        for index, value in enumerate(variants):
            with self.subTest(index=index),self.assertRaises(q.Failure):
                q._check_operations(value,[target(0),target(1)])

    def test_serial_submission_alone_is_not_concurrent_pressure_proof(self):
        values=operations()
        for item in values:item["job"]["queued_at"]=item["job"]["started_at"]
        with self.assertRaisesRegex(q.Failure,"CONCURRENT_QUEUE_PRESSURE"):
            q._check_operations(values,[target(0),target(1)])

    def test_original_documents_states_and_active_document_preserved(self):
        old={**state(2),"dirty":True}
        before=inventory([old],old)
        after=inventory([old,state(0),state(1)],state(1))
        q._check_preserved(before,after,[target(0)["path"],target(1)["path"]])
        q._check_preserved(before,before,[],final=True)
        changed=copy.deepcopy(after);changed["documents"][0]["dirty"]=False
        with self.assertRaises(q.Failure):q._check_preserved(before,changed,[target(0)["path"],target(1)["path"]])
        changed=copy.deepcopy(before);changed["active_document"]=None
        with self.assertRaises(q.Failure):q._check_preserved(before,changed,[],final=True)
        with self.assertRaises(q.Failure):q._check_preserved(before,after,[target(0)["path"]])

    def test_cleanup_requires_exact_owned_path_under_unique_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            path=root/"test.SLDPRT"
            owned={q._key(path):{"path":str(path),"document_type":1,"configuration":"Default"}}
            self.assertEqual(q._owned_target(path,root,owned),owned[q._key(path)])
            for bad in [root/"other.SLDPRT",root.parent/"test.SLDPRT",root/"nested"/"test.SLDPRT"]:
                with self.subTest(path=bad),self.assertRaises(q.Failure):q._owned_target(bad,root,owned)

    def test_admission_uses_target_metadata_and_retains_exact_request_id(self):
        client=Mock()
        client.submit.return_value={"id":"job","request_id":"stable","seq":1,"state":"completed",
            "queued_at":1.,"started_at":2.,"finished_at":3.,"execution_context":{"target_pid":42},
            "result":[{"type":"text","text":json.dumps({"success":True,"data":{"document_selection":{"selected":True}}})}]}
        _,summary=q._operation(client,"execute_python",{"code":"result = 1"},"stable","test",target=target(),trusted=True)
        args,kwargs=client.submit.call_args
        self.assertEqual(args[1]["_coordination"],{"target_document":target(),"trusted_python":True})
        self.assertEqual(kwargs["request_id"],"stable")
        self.assertEqual(summary["queued_at"],1.)
        self.assertTrue(summary["document_selection"]["selected"])

    def test_actual_stdio_frontend_environment_pins_same_production_broker(self):
        owner = "a"*32
        with patch.dict(q.os.environ, {q.OWNER_PIN_ENV:"b"*32}):
            env = q._frontend_env(owner)
            self.assertEqual(env[q.OWNER_PIN_ENV],owner)
            self.assertEqual(env["PYTHONIOENCODING"],"utf-8")
            self.assertEqual(env["PYTHONDONTWRITEBYTECODE"],"1")
            self.assertEqual(q.os.environ[q.OWNER_PIN_ENV],"b"*32)

    def test_failed_or_uncertain_native_jobs_remain_failed_with_evidence(self):
        for state_name in ("failed","recovery_required","cancelled"):
            client=Mock()
            job={"id":"job","request_id":"stable","seq":1,"state":state_name,"reason":"synthetic",
                 "result":[{"type":"text","text":"{\"success\":false}"}]}
            with self.subTest(state=state_name),self.assertRaises(q.Failure) as stopped:
                q._finish_job(client,job,"test",30)
            self.assertEqual(stopped.exception.job["journal_reason"],"synthetic")
            self.assertEqual(stopped.exception.job["native_result"],job["result"])


if __name__ == "__main__":
    unittest.main()
