import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import load_config
from hooks.guard import evaluate

def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result

report = module("keyhole_report", ROOT / "tools/report.py")
state = module("keyhole_state", ROOT / "tools/state.py")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.project = self.home / "repo"
        self.project.mkdir()
        (self.project / ".git").mkdir()
        self.config = load_config(self.project)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return path

    def jsonl(self, records, path=None):
        path = path or self.home / "sample.jsonl"
        return self.write(path, "".join(json.dumps(r) + "\n" for r in records))

    def decision(self, name, data):
        return evaluate({"tool_name": name, "tool_input": data, "cwd": str(self.project)})


class GuardTests(Fixture):
    def test_unknown_tool_and_non_object_metadata_are_ignored(self):
        self.assertIsNone(self.decision(17, {"cmd": "rg needle src"}))
        path = self.jsonl([[], None, {"type": "session_meta", "payload": {"cwd": str(self.project)}}])
        self.assertEqual(report.transcript_cwd(path), str(self.project))

    def test_native_exec_and_canonical_bash_agree(self):
        self.assertTrue(self.decision("functions.exec_command", {"cmd": "rg needle src"}))
        self.assertTrue(self.decision("Bash", {"command": "rg needle src"}))
        self.assertIsNone(self.decision("exec_command", {"cmd": "rg -m20 needle src"}))
        self.assertIsNone(self.decision("exec_command", {"cmd": "rg needle src", "max_output_tokens": 1000}))
        self.assertTrue(self.decision("exec_command", {"cmd": "rg needle src", "max_output_tokens": 0}))

    def test_multiple_cat_files_share_a_budget(self):
        for name in ("a.txt", "b.txt"):
            self.write(self.project/name, "x\n"*300)
        self.assertTrue(self.decision("Bash", {"command": "cat a.txt b.txt"}))
        self.assertIsNone(self.decision("Bash", {"command": "cat a.txt"}))

    def test_quoted_pipes_do_not_hide_unbounded_grep(self):
        self.assertTrue(self.decision("Bash", {"command": "rg 'foo|bar' src"}))
        self.assertIsNone(self.decision("Bash", {"command": "rg 'foo|bar' src | head -20"}))

    def test_cua_explicit_frames_and_named_options_pass(self):
        for code in ["await tab.screenshot({fullPage: false})",
                     'await tab.screenshot({"fullPage": false})',
                     "await tab.screenshot({clip: {x: 1, y: 2, width: 30, height: 40}})",
                     "await tab.screenshot(options)"]:
            with self.subTest(code=code):
                self.assertIsNone(self.decision("mcp__cua_repl__js", {"code": code}))

    def test_cua_empty_frames_and_multiple_calls_are_narrowed(self):
        for code in ["await tab.screenshot()", "await tab.screenshot({})",
                     "await tab.screenshot({scale: 0.5})",
                     "await a.screenshot({fullPage: false}); await b.screenshot({fullPage: false})"]:
            with self.subTest(code=code):
                self.assertTrue(self.decision("mcp__cua_repl__js", {"code": code}))

    def test_cua_strings_comments_and_dom_do_not_trigger(self):
        for code in ["const example = 'tab.screenshot()';",
                     "// tab.screenshot()\nawait tab.playwright.domSnapshot()",
                     "/* tab.screenshot() */ await tab.title()"]:
            self.assertIsNone(self.decision("mcp__cua_repl__js", {"code": code}))

    def test_shared_config_works_from_subdirectory_without_claude_env(self):
        self.write(self.project/".claude/keyhole.json", '{"read_max_lines":10}')
        self.write(self.project/".agents/keyhole.json", '{"read_max_lines":20}')
        nested=self.project/"web/portal"
        nested.mkdir(parents=True)
        self.assertEqual(load_config(nested)["read_max_lines"], 20)
        self.write(nested/"big.txt", "line\n"*21)
        self.assertTrue(evaluate({"tool_name":"Read", "cwd":str(nested),
                                 "tool_input":{"file_path":"big.txt"}}))

    def test_malformed_cli_input_is_fail_open(self):
        for data in ["[]", "invalid", '{"tool_input":42}', '{"tool_name":null,"tool_input":{}}']:
            r=subprocess.run([sys.executable,str(ROOT/"hooks/guard.py")],input=data,
                             text=True,capture_output=True)
            self.assertEqual((r.returncode,r.stdout),(0,""))


class ReportTests(Fixture):
    def test_claude_pairing_images_and_duplicate_message_usage(self):
        image={"type":"image","source":{"data":"A"*100000}}
        records=[
            {"uuid":"a","cwd":str(self.project),"message":{"id":"m","role":"assistant",
             "usage":{"input_tokens":10,"output_tokens":2},"content":[
                 {"type":"tool_use","id":"c","name":"Bash","input":{"command":"npm test"}}]}},
            {"uuid":"b","message":{"role":"user","content":[
                {"type":"tool_result","tool_use_id":"c","content":[{"type":"text","text":"test"*10},image]}]}},
            {"uuid":"c","message":{"id":"m","role":"assistant",
             "usage":{"input_tokens":10,"output_tokens":3},"content":[]}},
        ]
        session=report.scan(self.jsonl(records))
        self.assertEqual(len(session.rows),1)
        self.assertEqual((session.rows[0].text_chars,session.rows[0].images),(40,1))
        self.assertEqual(session.usage,{"input_tokens":10,"output_tokens":3})

    def test_codex_calls_wrappers_duplicates_and_usage_snapshots(self):
        def item(value):return {"type":"response_item","payload":value}
        records=[
            {"type":"session_meta","payload":{"cwd":str(self.project)}},
            item({"type":"function_call","id":"f1","name":"exec_command","namespace":"functions",
                  "call_id":"c1","arguments":json.dumps({"cmd":"rg needle src"})}),
            item({"type":"function_call_output","id":"o1","call_id":"c1","output":"found "*4}),
            item({"type":"function_call_output","id":"o1","call_id":"c1","output":"found "*4}),
            item({"type":"custom_tool_call","id":"f2","name":"exec","call_id":"c2",
                  "input":"text(await tools.exec_command({cmd:'pwd'}));"}),
            item({"type":"custom_tool_call_output","id":"o2","call_id":"c2","output":[
                {"type":"text","text":"done"},{"type":"input_image","image_url":"data:image/png;base64,"+"A"*100000}]}),
            {"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{
                "input_tokens":100,"cached_input_tokens":80,"output_tokens":5}}}},
            {"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{
                "input_tokens":250,"cached_input_tokens":200,"output_tokens":10}}}},
        ]
        result=report.summarize([report.scan(self.jsonl(records))])
        self.assertEqual(result["reported_usage_by_agent"]["codex"]["input_tokens"],250)
        self.assertEqual(result["tools"]["codex:functions.exec_command"]["calls"],1)
        self.assertEqual(result["tools"]["codex:functions.exec_command"]["would_narrow_calls"],1)
        self.assertEqual(result["tools"]["codex:exec"]["images"],1)
        self.assertEqual(result["diagnostics"]["opaque_code_mode_calls"],1)

    def test_usage_records_are_deduplicated_when_no_cumulative_snapshot(self):
        entry={"type":"token_usage_record","payload":{"response_id":"r",
              "usage":{"input_tokens":100,"output_tokens":5}}}
        session=report.scan(self.jsonl([entry,entry]))
        self.assertEqual(session.usage["input_tokens"],100)

    def test_report_replays_actual_guard_not_any_unlimited_shell(self):
        session=report.Session("fixture","claude",str(self.project))
        session.rows=[
            report.Row("claude","Bash",{"command":"npm run test"},str(self.project),1000,0),
            report.Row("claude","Bash",{"command":"rg needle ."},str(self.project),1000,0)]
        result=report.summarize([session])
        self.assertEqual(result["tools"]["claude:Bash"]["would_narrow_calls"],1)

    def test_partial_active_transcript_is_reported(self):
        path=self.jsonl([{"type":"response_item","payload":{
            "type":"function_call_output","call_id":"missing","output":"lost"}}])
        with path.open("a") as f:f.write('{"partial":')
        session=report.scan(path)
        self.assertEqual(session.unmatched_outputs,1)
        self.assertEqual(session.malformed_lines,1)

    def test_native_command_events_do_not_double_count_wrapper_output(self):
        event={"type":"event_msg","payload":{"type":"item_completed","item":{
            "type":"CommandExecution","id":"e1","command":["/bin/zsh","-lc","rg secret-value src"],
            "formatted_output":"line"*100}}}
        session=report.scan(self.jsonl([{"type":"session_meta","payload":{"cwd":str(self.project)}},event,event]))
        session.rows.append(report.Row("codex","exec",{"code":"opaque"},str(self.project),40,0))
        result=report.summarize([session])
        self.assertEqual(result["tools"]["codex:exec"]["estimated_text_tokens"],10)
        self.assertEqual(result["codex_command_events_overlapping"]["rg"]["calls"],1)
        self.assertNotIn("secret-value",json.dumps(result))

    def test_serialized_mcp_images_are_not_counted_as_text(self):
        content=json.dumps({"content":[{"type":"image","data":"A"*100000},
                                       {"type":"text","text":"abcd"}]})
        self.assertEqual(report.metrics(content),(4,1))

    def test_discovery_filters_other_projects(self):
        folder=self.home/".codex/sessions/2026/09/08"
        good=self.jsonl([{"type":"session_meta","payload":{"cwd":str(self.project/"web")}}],
                        folder/"ours.jsonl")
        self.jsonl([{"type":"session_meta","payload":{"cwd":str(self.home/"other")}}],
                   folder/"theirs.jsonl")
        self.assertEqual(report.discover(self.project,"codex",5,self.home),[good])


class StateTests(Fixture):
    def event(self, session="session-a"):
        return {"cwd":str(self.project/"web"),"session_id":session}

    def test_session_state_does_not_read_another_active_task(self):
        path=state.state_path(self.project,self.config,"session-a")
        self.write(path,"# Task A\nPrivate decision A")
        self.write(state.state_path(self.project,self.config,"session-b"),"# Task B\nPrivate decision B")
        text=state.inject(self.event())
        self.assertIn("Private decision A",text)
        self.assertNotIn("Private decision B",text)

    def test_new_session_lists_candidates_without_adopting_their_content(self):
        self.write(state.state_path(self.project,self.config,"session-b"),"# Task B\nDo unrelated work")
        text=state.inject(self.event())
        self.assertIn("session-b.md",text)
        self.assertNotIn("Do unrelated work",text)

    def test_legacy_current_and_explicit_shared_state_still_load(self):
        self.write(self.project/".claude/state/CURRENT.md","old state")
        self.assertIn("old state",state.inject(self.event()))
        self.write(self.project/".agents/keyhole/CURRENT.md","shared handoff")
        text=state.inject(self.event())
        self.assertIn("shared handoff",text)
        self.assertNotIn("old state",text)

    def test_injection_is_bounded_and_never_rewrites_files(self):
        path=self.write(self.project/".agents/keyhole/CURRENT.md","state\n"*5000)
        before=path.read_bytes()
        text=state.inject(self.event())
        self.assertLessEqual(len(text),4000)
        self.assertIn("[truncated; read relevant sections]",text)
        self.assertEqual(path.read_bytes(),before)

    def test_session_symlinks_cannot_inject_outside_the_project(self):
        target=self.write(self.home/"outside.md","not project state")
        link=state.state_path(self.project,self.config,"session-a")
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        with self.assertRaises(ValueError):state.state_path(self.project,self.config,"session-a")

    def test_project_state_configuration_and_session_key_validation(self):
        self.write(self.project/".agents/keyhole.json",json.dumps({
            "state_dir":".agents/local/state","state_files":["docs/next.md"]}))
        self.write(self.project/"docs/next.md","canonical plan pointer")
        self.assertIn("canonical plan pointer",state.inject(self.event()))
        with self.assertRaises(ValueError):state.state_path(self.project,self.config,"../outside")
        with self.assertRaises(ValueError):state.inside(self.project,"../outside.md")

    def test_hook_wrapper_emits_codex_and_claude_compatible_context(self):
        r=subprocess.run(["bash",str(ROOT/"hooks/state-inject.sh")],input=json.dumps(self.event()),
                         text=True,capture_output=True,check=True)
        output=json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"],"SessionStart")
        self.assertIn("session-a.md",output["additionalContext"])


if __name__ == "__main__":
    unittest.main()
