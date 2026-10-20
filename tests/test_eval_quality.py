"""Offline regressions: independent judge, real grading entries, failure accounting."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from eval import judge, runner, suites, worker
from eval.compare import compare, paired_interval, verified_run
from graders import assignments, config, eval_policy, providers, telemetry

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "eval/examples"


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="quality-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.suite = suites.load(EXAMPLES / "short")

    def copied_suite(self):
        target = self.root / "suite"
        shutil.copytree(EXAMPLES / "short", target)
        return target

    def change_rows(self, path, fn):
        rows = suites.rows(path)
        fn(rows)
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")

    def test_judge_controls_all_task_types(self):
        for name in ("short", "math", "essay", "lab"):
            with self.subTest(name=name):
                audit = judge.audit(suites.load(EXAMPLES / name))
                self.assertTrue(audit["healthy"])
                self.assertEqual(len(audit["positive_controls"]), 9)
                self.assertTrue(all(n > 0 for n in audit["negative_controls_rejected"].values()))

    def test_split_keeps_related_variants_together(self):
        split = suites.make_split(self.suite)
        suites.check_split(self.suite, split)
        self.assertEqual(split, suites.make_split(self.suite))
        self.assertEqual(set(split["groups_per_split"].values()), {1})
        case = self.suite["cases"][0]
        split["case_assignments"][case["id"]] = next(
            p for p in suites.SPLITS if p != split["case_assignments"][case["id"]])
        with self.assertRaises(ValueError):
            suites.check_split(self.suite, split)

    def test_changed_teacher_label_invalidates_split(self):
        split = suites.make_split(self.suite)
        path = self.copied_suite()
        self.change_rows(path / "labels.private.jsonl", lambda rows: rows[0].update(tolerance=1))
        with self.assertRaises(ValueError):
            suites.check_split(suites.load(path), split)

    def test_private_labels_cannot_enter_worker_input_schema(self):
        path = self.copied_suite()
        self.change_rows(path / "cases.jsonl", lambda rows: rows[0]["input"].update(expected_score=10))
        with self.assertRaisesRegex(ValueError, "隐藏标注"):
            suites.load(path)

    def test_attachment_cannot_escape_dataset(self):
        path = self.copied_suite()
        (self.root / "private.txt").write_text("private", encoding="utf-8")
        self.change_rows(path / "cases.jsonl", lambda rows: rows[0]["input"].update(files=["../private.txt"]))
        with self.assertRaises(ValueError):
            suites.load(path)

    def test_invalid_teacher_score_and_evidence_rejected(self):
        path = self.copied_suite()
        self.change_rows(path / "labels.private.jsonl", lambda rows: rows[0].update(score=float("nan")))
        with self.assertRaises(ValueError):
            suites.load(path)

    def test_rule_scale_cannot_silently_change(self):
        path = self.copied_suite()
        self.change_rows(path / "cases.jsonl", lambda rows: rows[0]["input"]["rules"][0].update(full=999))
        with self.assertRaisesRegex(ValueError, "评分项"):
            suites.load(path)

    def test_judge_catches_right_score_wrong_reason_and_fake_quote(self):
        case = next(c for c in self.suite["cases"] if self.suite["labels"][c["id"]]["deductions"])
        label = self.suite["labels"][case["id"]]
        for field, value in (("reason_code", "unrelated"), ("evidence", "invented student words")):
            output = judge.oracle(case, label)
            output["deductions"][0][field] = value
            result = judge.judge(case, label, output)
            self.assertFalse(result["accepted"])
            self.assertEqual(result["score_error"], 0)
            self.assertIn("unsupported_deduction", result["issues"])

    def test_all_review_does_not_count_as_automatic_success(self):
        records = []
        for case in self.suite["cases"]:
            label = self.suite["labels"][case["id"]]
            output = judge.oracle(case, label)
            output.update(review_required=True, review_reason="always")
            records.append(dict(status="completed", judgment=judge.judge(case, label, output),
                                review_expected=label["review_required"], seconds=1, calls=[]))
        result = judge.summarize(records)
        self.assertEqual(result["automatic_coverage"], 0)
        self.assertEqual(result["unnecessary_review_rate"], 1)
        self.assertLess(result["correct_delivery_rate"], 1)

    def test_failures_and_unknown_usage_remain_in_denominator(self):
        records = [dict(status=s, seconds=2, calls=[{"usage": None}],
                        review_expected=False) for s in ("timeout", "service_error", "truncated")]
        result = judge.summarize(records)
        self.assertEqual(result["requested"], 3)
        self.assertEqual(result["failure_rate"], 1)
        self.assertIsNone(result["score_mae_completed_clear"])
        self.assertIsNone(result["cost"])
        self.assertEqual(result["provider_attempts"], 3)
        self.assertEqual(result["usage_known_attempts"], 0)

    def test_missing_and_extra_claims_do_not_produce_rate_above_one(self):
        case = next(c for c in self.suite["cases"] if self.suite["labels"][c["id"]]["deductions"])
        label = self.suite["labels"][case["id"]]
        output = judge.oracle(case, label)
        output["deductions"][0]["rule_id"] = "nonexistent"
        result = judge.judge(case, label, output)
        self.assertLessEqual(result["unsupported_deductions"], result["deductions"])
        self.assertFalse(result["accepted"])

    def test_policy_is_opt_in_and_context_local(self):
        result = {"score": 5, "dimensions": [{"name": "A", "score": 5, "full": 10}]}
        rules = [{"id": "a", "name": "A", "full": 10, "reason_codes": ["wrong"]}]
        self.assertNotIn("review_required", eval_policy.finalize(result, {}, "text"))
        with eval_policy.activate({"name": "evidence", "require_evidence": True}, rules):
            self.assertTrue(eval_policy.finalize(result, {}, "text")["score_provisional"])
            self.assertIn("deductions", eval_policy.instructions())
        self.assertEqual(eval_policy.instructions(), "")
        with self.assertRaises(ValueError):
            eval_policy.validate({"name": "tamper", "teacher_score": 1})

    def test_provider_attempts_include_retry_usage_and_actual_model(self):
        response = Mock(status_code=200, headers={})
        response.json.return_value = {"model": "actual-version", "usage": {
            "prompt_tokens": 9, "completion_tokens": 4, "api_key": "secret", "total_tokens": float("nan")},
            "choices": [{"finish_reason": "stop"}]}
        with telemetry.capture() as events, patch.object(providers, "MAX_RETRIES", 2), \
                patch.object(providers.time, "sleep"), \
                patch.object(providers.requests, "post", side_effect=[providers.requests.Timeout(), response]):
            providers._post_with_retry("https://example.invalid", {"Authorization": "secret"},
                                       {"model": "requested", "messages": ["private prompt"]})
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["error_type"], "Timeout")
        self.assertEqual(events[1]["actual_model"], "actual-version")
        self.assertEqual(events[1]["usage"], {"prompt_tokens": 9, "completion_tokens": 4})
        self.assertNotIn("secret", json.dumps(events))
        self.assertNotIn("private prompt", json.dumps(events))
        self.assertIsNone(telemetry.begin_attempt({}))

    def test_journal_preserves_inflight_attempt(self):
        path = self.root / "calls.jsonl"
        with telemetry.capture(telemetry.Journal(path)):
            event = telemetry.begin_attempt({"model": "x"})
        self.assertTrue(suites.rows(path)[0]["pending"])
        self.assertIsNone(event["usage"])

    def test_live_preflight_and_locked_test_gate(self):
        split = self.root / "split.json"
        suites.write_json(split, suites.make_split(self.suite))
        with patch.dict(os.environ, {config.PROVIDERS["deepseek"]["env_key"]: ""}):
            with self.assertRaisesRegex(ValueError, "API Key"):
                runner.run(EXAMPLES / "short", split, EXAMPLES / "baseline.json", self.root)
        with self.assertRaisesRegex(ValueError, "locked-test"):
            runner.run(EXAMPLES / "short", split, EXAMPLES / "baseline.json", self.root,
                       replay=EXAMPLES / "short/replay.json", partition="locked-test")

    def test_group_bootstrap_does_not_count_repeats_as_groups(self):
        result = paired_interval({"family1": [-1, -1, -1], "family2": [0, 0, 0]})
        self.assertEqual(result["groups"], 2)
        self.assertEqual(result["mean"], -0.5)
        self.assertEqual(result, paired_interval({"family1": [-1]*3, "family2": [0]*3}))

    def test_worker_timeout_kills_owned_process_tree_and_keeps_all_cases(self):
        split = self.root / "split.json"
        suites.write_json(split, suites.make_split(self.suite))
        process = Mock(pid=876543)
        process.wait.side_effect = [subprocess.TimeoutExpired("worker", .01), None] * 3
        with patch.object(runner.subprocess, "Popen", return_value=process), \
                patch.object(runner.subprocess, "run") as kill, patch.object(runner.os, "killpg", create=True) as killpg:
            dest = runner.run(EXAMPLES / "short", split, EXAMPLES / "baseline.json", self.root,
                              replay=EXAMPLES / "short/replay.json", timeout=.01)
        records = suites.rows(dest / "records.jsonl")
        self.assertEqual(len(records), 3)
        self.assertEqual({r["status"] for r in records}, {"timeout"})
        self.assertEqual(kill.call_count if os.name == "nt" else killpg.call_count, 3)
        self.assertEqual(suites.read_json(dest / "report.json")["metrics"]["failure_rate"], 1)

    def test_runner_launch_error_is_recorded(self):
        split = self.root / "split.json"
        suites.write_json(split, suites.make_split(self.suite))
        with patch.object(runner.subprocess, "Popen", side_effect=OSError("no process")):
            dest = runner.run(EXAMPLES / "short", split, EXAMPLES / "baseline.json", self.root,
                              replay=EXAMPLES / "short/replay.json")
        self.assertEqual(suites.read_json(dest / "report.json")["metrics"]["error_categories"], {"runner_error": 3})

    def test_legacy_failure_is_preserved_and_never_best(self):
        from eval.run_eval import run_engine, _best_line
        from graders.extract import inspect_text_file
        source = self.root / "student.txt"
        source.write_text("student text", encoding="utf-8")
        items = [dict(file="student.txt", report=inspect_text_file(source), label={"score": 90})]
        with patch("eval.run_eval._grade_real", side_effect=RuntimeError("upstream secret")):
            result = run_engine("agent", items, "", None, False)
        self.assertEqual(result["items"][0]["status"], "error")
        self.assertIsNone(result["metrics"]["mae"])
        self.assertIn("失败", _best_line({"agent": result}))
        self.assertNotIn("secret", json.dumps(result))

    def test_locked_confirmation_is_single_use_and_releases_its_lock(self):
        split = self.root / "split.json"
        split_data = suites.make_split(self.suite)
        suites.write_json(split, split_data)
        candidate = suites.read_json(EXAMPLES / "baseline.json")
        choice = providers.ModelChoice("deepseek", "", use_vision=False).resolved()
        selection = dict(adopt_candidate=True, dataset=self.suite["fingerprint"],
            implementation=runner.implementation_hash(), partition="validation", timeout=180, repeats=1,
            candidate_hashes=[suites.digest(candidate)], split_digest=suites.digest(split_data),
            options=dict(provider="deepseek", model=choice.model, base_url=choice.base_url,
                         use_vision=False, engine="assignment", ocr_mode="off"))
        frozen = self.root / "selection.json"
        suites.write_json(frozen, selection)
        with patch.dict(os.environ, {config.PROVIDERS["deepseek"]["env_key"]: "test-only"}), \
                patch.object(runner.subprocess, "Popen", side_effect=OSError("fixture")):
            # Failure consumes the confirmation attempt; it cannot be silently retried.
            runner.run(EXAMPLES / "short", split, EXAMPLES / "baseline.json", self.root,
                       partition="locked-test", final_selection=frozen)
            with self.assertRaisesRegex(ValueError, "已使用"):
                runner.run(EXAMPLES / "short", split, EXAMPLES / "baseline.json", self.root,
                           partition="locked-test", final_selection=frozen)
        self.assertFalse(split.with_suffix(".locked-test.json").with_suffix(".mutex").exists())

    def test_worker_uses_actual_image_request_and_opt_in_evidence(self):
        from PIL import Image
        source = self.root / "answer.png"
        Image.new("RGB", (200, 200), "white").save(source)
        case = next(c for c in self.suite["cases"] if c["id"].endswith("partial"))
        inp = copy.deepcopy(case["input"])
        inp["files"] = [str(source)]
        replies = suites.read_json(EXAMPLES / "short/replay.json")[case["id"]]
        replies = copy.deepcopy(replies)
        replies[0].pop("deductions")
        payload = dict(input=inp, candidate=suites.read_json(EXAMPLES / "evidence.json"),
                       task_type="short", mode="live",
                       options=dict(provider="openai", model="gpt-4o", base_url="",
                                    use_vision=True, ocr_mode="off", engine="assignment"))
        with patch.dict(os.environ, {config.PROVIDERS["openai"]["env_key"]: "test-only"}), \
                patch.object(config, "OUTPUT_DIR", self.root), patch.object(config, "WORK_DIR", self.root), \
                patch.object(providers.requests, "post", side_effect=worker.replay_transport(replies)) as http:
            result = worker.run(payload, self.root / "output.json")
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["output"]["score_provisional"])
        self.assertEqual(result["output"]["raw_score"], replies[0]["score"])
        self.assertIn("image_url", json.dumps(http.call_args.kwargs["json"]))

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional MCP dependency is not installed")
    def test_mcp_eval_tools_prepare_and_run(self):
        from mcp_server.server import prepare_grading_eval, run_grading_eval
        prepared = prepare_grading_eval(str(EXAMPLES / "lab"), str(self.root / "prepared"))
        self.assertTrue(prepared["healthy"])
        before = Path(prepared["split_file"]).read_bytes()
        prepare_grading_eval(str(EXAMPLES / "lab"), str(self.root / "prepared"), seed=123)
        self.assertEqual(before, Path(prepared["split_file"]).read_bytes())
        result = run_grading_eval(str(EXAMPLES / "lab"), prepared["split_file"],
            str(EXAMPLES / "evidence.json"), str(self.root / "mcp-run"),
            replay_file=str(EXAMPLES / "lab/replay.json"), timeout=30)
        self.assertEqual(result["summary"]["metrics"]["requested"], 3)
        self.assertEqual(result["summary"]["metrics"]["failure_rate"], 0)


class RealEntryReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="eval-entry-tests-")
        cls.root = Path(cls.temp.name)
        cls.runs = {}
        for name in ("short", "math", "essay", "lab"):
            suite = suites.load(EXAMPLES / name)
            split = cls.root / (name + "-split.json")
            suites.write_json(split, suites.make_split(suite))
            for policy in ("baseline", "evidence"):
                cls.runs[name, policy] = runner.run(
                    suite["root"], split, EXAMPLES / (policy + ".json"), cls.root / name,
                    replay=suite["root"] / "replay.json", timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_all_four_real_entries_complete_with_expected_reviews(self):
        for key, path in self.runs.items():
            with self.subTest(key=key):
                report, records = verified_run(path)
                metrics = report["metrics"]
                self.assertEqual(metrics["requested"], 3)
                self.assertEqual(metrics["correct_delivery_rate"], 1)
                self.assertEqual(metrics["automatic_coverage"], 2/3)
                self.assertFalse(report["human_verified"])
                for r in records:
                    self.assertTrue(r["calls"])
                    self.assertEqual({c["actual_model"] for c in r["calls"]}, {"offline-replay"})

    def test_worker_has_public_rules_but_no_teacher_labels(self):
        for work in (self.runs["lab", "evidence"] / "attempts").iterdir():
            payload = suites.read_json(work / "input.json")
            self.assertEqual(set(payload), {"input", "candidate", "task_type", "options", "mode", "replies"})
            self.assertIn("rules", payload["input"])
            self.assertNotIn("labels", payload)
            self.assertFalse(list(work.rglob("labels.private.jsonl")))
            # Replay fixture is explicitly tagged; live runs do not include replies.
            self.assertEqual(payload["mode"], "replay")

    def test_lab_real_tool_loop_is_exercised(self):
        records = suites.rows(self.runs["lab", "evidence"] / "records.jsonl")
        self.assertTrue(all(len(r["calls"]) == 3 for r in records))
        self.assertIn("submit_grade", json.dumps(records))

    def test_replay_comparison_cannot_claim_improvement(self):
        path = self.root / "compare.json"
        result = compare(self.runs["short", "baseline"], self.runs["short", "evidence"], path)
        self.assertFalse(result["adopt_candidate"])
        self.assertFalse(result["formal_sample_requirements_met"])
        self.assertFalse(result["gates"]["tokens"])
        self.assertEqual(result["paired_intervals"]["coverage"]["groups"], 1)
        self.assertTrue(path.with_suffix(".patch").is_file())

    def test_tampered_records_are_not_trusted(self):
        with tempfile.TemporaryDirectory(prefix="eval-tamper-") as temp:
            dest = Path(temp) / "run"
            shutil.copytree(self.runs["short", "baseline"], dest)
            with (dest / "records.jsonl").open("a", encoding="utf-8") as f:
                f.write("{}\n")
            with self.assertRaisesRegex(ValueError, "明细"):
                verified_run(dest)

    def test_failed_requests_cannot_be_dropped_for_comparison(self):
        with tempfile.TemporaryDirectory(prefix="eval-drop-") as temp:
            dest = Path(temp) / "run"
            shutil.copytree(self.runs["short", "baseline"], dest)
            rows = suites.rows(dest / "records.jsonl")[:-1]
            target = dest / "records.jsonl"
            target.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            report = suites.read_json(dest / "report.json")
            report["metrics"] = judge.summarize(rows)
            report["records_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
            suites.write_json(dest / "report.json", report)
            with self.assertRaisesRegex(ValueError, "失败请求"):
                compare(self.runs["short", "baseline"], dest, Path(temp) / "compare.json")

    def test_candidate_round_limit_is_enforced(self):
        with tempfile.TemporaryDirectory(prefix="eval-history-") as temp:
            out = Path(temp) / "compare.json"
            history = Path(temp) / "history.json"
            args = (self.runs["short", "baseline"], self.runs["short", "evidence"], out)
            compare(*args, history=history, hypothesis="Evidence required", max_rounds=1)
            with self.assertRaisesRegex(ValueError, "上限"):
                compare(*args, history=history, hypothesis="Again", max_rounds=1)

    def test_report_escapes_untrusted_student_markup(self):
        with tempfile.TemporaryDirectory(prefix="eval-html-") as temp:
            dest = Path(temp) / "run"
            shutil.copytree(self.runs["short", "baseline"], dest)
            records = suites.rows(dest / "records.jsonl")
            for work in (dest / "attempts").iterdir():
                inp = suites.read_json(work / "input.json")
                inp["input"]["answer"] = "<script>alert(1)</script>"
                suites.write_json(work / "input.json", inp)
            runner.render_report(dest, suites.read_json(dest / "report.json"), records)
            text = (dest / "report.html").read_text(encoding="utf-8")
            self.assertNotIn("<script>", text)
            self.assertIn("&lt;script&gt;", text)


if __name__ == "__main__":
    unittest.main()
