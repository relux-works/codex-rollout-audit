import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import codex_rollout_audit as audit


def record(kind, payload, second=0):
    return {"type": kind, "payload": payload,
            "timestamp": f"2026-09-01T00:00:{second:02d}Z"}


def context(turn_id="t1", second=0):
    return record("turn_context", {"turn_id": turn_id, "model": "example"}, second)


def usage(total, last, second=1):
    def fields(n):
        return {"input_tokens": n, "cached_input_tokens": n // 2, "output_tokens": n // 10}
    return record("event_msg", {"type": "token_count", "info": {
        "total_token_usage": fields(total), "last_token_usage": fields(last)}}, second)


def call(name, arguments, second=1):
    return record("response_item", {"type": "function_call", "call_id": f"c{second}",
                                   "name": name, "arguments": json.dumps(arguments)}, second)


def message(role, text, second=0):
    return record("response_item", {"type": "message", "role": role,
                                   "content": [{"type": "output_text", "text": text}]}, second)


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "rollout.jsonl"

    def parse(self, *records):
        self.path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
        return audit.parse_turns(str(self.path))

    def test_repeated_usage_snapshots_do_not_charge_twice(self):
        _, turns = self.parse(context(), usage(100, 100), usage(100, 100, 2), usage(200, 100, 3))
        self.assertEqual((turns[0]["inp"], turns[0]["cached"], turns[0]["out"]), (200, 100, 20))

    def test_previous_turn_usage_is_not_charged_to_next_turn(self):
        _, turns = self.parse(context(), usage(100, 100), context("t2", 2),
                              usage(100, 100, 3), usage(200, 100, 4))
        self.assertEqual([t["inp"] for t in turns], [100, 100])

    def test_inherited_total_and_counter_reset_use_only_last_usage(self):
        meta, turns = self.parse(context(), usage(1000, 100), usage(1100, 100, 2), usage(50, 50, 3))
        self.assertEqual(turns[0]["inp"], 250)
        self.assertEqual(meta["usage_warnings"]["counter_resets"], 1)
        self.assertEqual(meta["usage_warnings"]["baseline_input_excluded"], 900)

    def test_snapshot_before_first_turn_establishes_baseline(self):
        _, turns = self.parse(usage(1000, 100), context(), usage(1100, 100, 2))
        self.assertEqual(turns[0]["inp"], 100)

    def test_missing_totals_are_reported_instead_of_summed(self):
        sample = usage(100, 100)
        del sample["payload"]["info"]["total_token_usage"]
        meta, turns = self.parse(context(), sample, sample)
        self.assertEqual(turns[0]["inp"], 0)
        self.assertEqual(meta["usage_warnings"]["missing_totals"], 2)

    def test_output_only_increment_and_missing_initial_last_usage(self):
        first = usage(100, 100)
        del first["payload"]["info"]["last_token_usage"]
        second = usage(100, 0, 2)
        second["payload"]["info"]["total_token_usage"]["output_tokens"] = 35
        meta, turns = self.parse(context(), first, second, second)
        self.assertEqual((turns[0]["inp"], turns[0]["cached"], turns[0]["out"]), (0, 0, 25))
        self.assertEqual(meta["usage_warnings"]["missing_initial_usage"], 1)

    def test_first_last_usage_is_bounded_by_each_cumulative_field(self):
        sample = usage(50, 100)
        sample["payload"]["info"]["total_token_usage"]["cached_input_tokens"] = 7
        _, turns = self.parse(context(), sample)
        self.assertEqual((turns[0]["inp"], turns[0]["cached"], turns[0]["out"]), (50, 7, 5))

    def test_pure_wait_calls_are_recognized(self):
        for name, args in [("clock.sleep", {"duration_ms": 1000}),
                           ("functions.exec_command", {"cmd": "sleep 30"}),
                           ("shell", {"command": ["sleep", "30"]}),
                           ("functions.write_stdin", {"session_id": 5, "chars": ""}),
                           ("collaboration.wait_agent", {"timeout_ms": 60000}),
                           ("functions.wait", {"cell_id": "a"})]:
            with self.subTest(name=name):
                _, turns = self.parse(context(), call(name, args))
                self.assertTrue(audit.is_wait_only(turns[0]))

    def test_shell_work_and_opaque_code_are_unclassified(self):
        for name, args in [("exec_command", {"cmd": "make build"}),
                           ("exec_command", {"cmd": "tail -n 20 result.log"}),
                           ("exec_command", {"cmd": "sleep 1; make build"}),
                           ("exec_command", {"cmd": "sleep 30 > result.txt"}),
                           ("shell", {"command": ["bash", "-lc", "sleep 30"]}),
                           ("exec", {"code": "await tools.exec_command({cmd: 'make build'})"}),
                           ("write_stdin", {"session_id": 5, "chars": "make\n"}),
                           ("wait", {"cell_id": "a", "terminate": True}),
                           ("update_plan", {"plan": []})]:
            with self.subTest(args=args):
                _, turns = self.parse(context(), call(name, args))
                self.assertFalse(audit.is_wait_only(turns[0]))

    def test_mixed_turn_is_not_wait_only(self):
        _, turns = self.parse(context(), call("clock.sleep", {"duration_ms": 1000}),
                              call("exec_command", {"cmd": "make build"}, 2))
        self.assertFalse(audit.is_wait_only(turns[0]))

    def test_native_calls_are_not_no_tool_or_wait_only(self):
        for name in ["web_search_call", "tool_search_call", "local_shell_call", "image_generation_call", "computer_call"]:
            with self.subTest(name=name):
                native = record("response_item", {"type": name}, 1)
                _, turns = self.parse(context(), native)
                self.assertEqual(turns[0]["calls"], 1)
                self.assertFalse(audit.is_wait_only(turns[0]))
                _, turns = self.parse(context(), native, call("clock.sleep", {"duration_ms": 1000}, 2))
                self.assertEqual(turns[0]["calls"], 2)
                self.assertFalse(audit.is_wait_only(turns[0]))

    def test_no_tool_is_not_an_empty_final(self):
        _, turns = self.parse(context(), message("assistant", "Still waiting."))
        self.assertEqual(turns[0]["calls"], 0)
        self.assertTrue(turns[0]["has_text"])

    def test_lifecycle_boundaries_and_repeated_context(self):
        _, turns = self.parse(
            record("event_msg", {"type": "task_started", "turn_id": "t1"}),
            context(second=1), context(second=2), usage(100, 100, 3),
            record("event_msg", {"type": "task_complete", "turn_id": "t1"}, 10),
            context("t2", 12), usage(200, 100, 13))
        self.assertEqual(len(turns), 2)
        self.assertEqual(audit.duration(turns[0]), 10)
        self.assertEqual(audit.ts(turns[1]["start"]) - audit.ts(turns[0]["end"]), 2)

    def test_goal_marker_must_start_message(self):
        _, turns = self.parse(context(), message("user", "Please explain: " + audit.GOAL_MARKER))
        self.assertEqual(turns[0]["kind"], "user")

    def test_native_goal_wrapper_and_separate_tool_namespace(self):
        sleep = call("sleep", {"duration_ms": 1000})
        sleep["payload"]["namespace"] = "clock"
        _, turns = self.parse(context(), message("user", '<codex_internal_context source="goal">\n'
                                                + audit.GOAL_MARKER + '\n</codex_internal_context>'), sleep)
        self.assertEqual(turns[0]["kind"], "goal")
        self.assertTrue(audit.is_wait_only(turns[0]))

    def test_legacy_goal_wrapper_is_recognized(self):
        _, turns = self.parse(context(), message("user", '<goal_context>\n' + audit.GOAL_MARKER + '\n</goal_context>'))
        self.assertEqual(turns[0]["kind"], "goal")

    def test_legacy_start_without_id_joins_context(self):
        _, turns = self.parse(record("event_msg", {"type": "task_started"}), context(second=1), usage(100, 100, 2))
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["inp"], 100)

    def test_overlapping_roots_are_read_once(self):
        self.parse(context())
        paths = list(audit.find_rollouts([self.temp.name, str(self.path), self.temp.name]))
        self.assertEqual(paths, [str(self.path)])

    def test_summary_preserves_short_durations_and_separates_goal_tokens(self):
        self.parse(context(), usage(100, 100), context("t2", 2),
                   message("user", audit.GOAL_MARKER, 2), call("clock.sleep", {"duration_ms": 1000}, 3),
                   usage(200, 100, 4))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            audit.main(["summary", str(self.path), "--json"])
        rows = json.loads(out.getvalue())
        self.assertEqual((rows[0]["input"], rows[0]["goal_input"], rows[0]["wait_only_goal_input"]),
                         (200, 100, 100))
        self.assertAlmostEqual(rows[0]["wait_only_goal_hours"], 2 / 3600)

    def test_zero_usage_does_not_invent_one_token(self):
        self.parse(context())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            audit.main(["summary", str(self.path)])
        self.assertNotIn("restarted for nothing", out.getvalue())
        self.assertNotIn("0.5M", out.getvalue())


if __name__ == "__main__":
    unittest.main()
