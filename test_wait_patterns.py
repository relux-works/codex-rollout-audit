import json
import tempfile
import unittest
from pathlib import Path

from wait_patterns import literal_exec_calls, load_rules, observation_call


class WaitPatternTests(unittest.TestCase):
    def test_literal_exec_forms_from_rollouts(self):
        for code in [
            'text(await tools.write_stdin({session_id:123,chars:"",yield_time_ms:1000}));',
            'const r = await tools.write_stdin({"session_id":123, chars:"",yield_time_ms:1000}); text(r.output);',
            "let r = await tools.write_stdin({'session_id':123, chars:'',yield_time_ms:1000,}); text(r);",
            '// @exec: {"yield_time_ms":1000}\ntext(await tools.write_stdin({session_id:123,chars:"",yield_time_ms:1000}));',
        ]:
            with self.subTest(code=code):
                self.assertEqual(literal_exec_calls(code), [('write_stdin', {'session_id':123,'chars':'','yield_time_ms':1000})])

    def test_multiple_calls_and_string_contents(self):
        code = 'text(await tools.exec_command({cmd:"sleep 30"})); text(await tools.write_stdin({session_id:1}));'
        self.assertEqual(literal_exec_calls(code), [('exec_command', {'cmd':'sleep 30'}), ('write_stdin', {'session_id':1})])
        self.assertEqual(literal_exec_calls('text(await tools.exec_command({cmd:"echo \\"tools.foo({})\\""}));'),
                         [('exec_command', {'cmd':'echo "tools.foo({})"'})])

    def test_opaque_js_cannot_hide_work(self):
        for code in [
            'text(await tools.exec_command({cmd: command}));',
            'text(await tools.exec_command({cmd: `sleep ${seconds}`}));',
            'text(await tools.exec_command({...args, cmd:"sleep 30"}));',
            'text(await tools.exec_command({cmd:"build",cmd:"sleep 30"}));',
            'text(await tools.write_stdin({session_id:1})); await tools.apply_patch(patch);',
            'for (;;) { text(await tools.write_stdin({session_id:1})); }',
            'const r = await tools.write_stdin({session_id:1}); text(other.output);',
            'text(await tools.exec_command({__proto__:null,cmd:"sleep 30"}));',
        ]:
            with self.subTest(code=code): self.assertIsNone(literal_exec_calls(code))

    def test_task_board_launch_is_distinct_from_wait_and_monitor(self):
        for command, expected in [
            ('task-board spawn TASK-example --background', None),
            ('task-board spawn directive add RUN-example --text next', None),
            ('task-board spawn wait RUN-example --timeout 8s', 'wait'),
            ('/opt/bin/task-board spawn observe RUN-example --cursor RUN-example:2 --until terminal --format compact', 'wait'),
            ('task-board spawn status RUN-example --json', 'monitor'),
            ('task-board spawn events RUN-example --cursor RUN-example:2', 'monitor'),
            ('task-board spawn events RUN-example --follow', 'wait'),
            ('task-board spawn status --help', None),
            ('task-board spawn status RUN-example --output result.json', None),
            ('task-board spawn wait RUN-example; make build', None),
            ('task-board spawn wait RUN-$(run)', None),
            ('task-board spawn status RUN-example | jq .', None),
        ]:
            with self.subTest(command=command):
                self.assertEqual(observation_call('exec_command', {'cmd':command}), expected)

    def test_other_orchestrators_use_exact_declarative_rules(self):
        rules = [
            {'kind':'wait','argv':['runner','jobs','wait','*','--timeout','*']},
            {'kind':'monitor','argv':['runner','jobs','status','*']},
            {'kind':'wait','tool':'mcp__runner__await_job','arguments':{'cancel':False}},
        ]
        for cmd, expected in [
            ('runner jobs wait job-1 --timeout 30', 'wait'),
            ('runner jobs status job-1', 'monitor'),
            ('runner jobs start job-1', None),
            ('runner jobs status job-1 --write out', None),
            ('runner jobs wait job-1 --timeout 30; build', None),
        ]:
            self.assertEqual(observation_call('exec_command', {'cmd':cmd}, rules), expected)
        self.assertEqual(observation_call('shell', {'command':['runner','jobs','status','job-1']}, rules), 'monitor')
        self.assertEqual(observation_call('mcp__runner__await_job', {'cancel':False}, rules), 'wait')
        self.assertIsNone(observation_call('mcp__runner__await_job', {'cancel':True}, rules))
        self.assertIsNone(observation_call('mcp__runner__await_job', {'cancel':0}, rules))
        rules.append({'kind':'monitor','argv':['runner','jobs','wait','*','--timeout','*']})
        self.assertIsNone(observation_call('exec_command', {'cmd':'runner jobs wait job-1 --timeout 30'}, rules))

    def test_tool_rules_cannot_bypass_wrapper_validation(self):
        for name in ['exec','exec_command','shell','shell_command','functions.exec_command']:
            self.assertIsNone(observation_call(name, {'cmd':'sleep 1; build'},
                                               [{'kind':'wait','tool':name}]))

    def test_shell_expansions_fail_closed_before_custom_rules(self):
        rules = [{'kind':'wait','argv':['runner','wait','*']}]
        for command in ['runner wait job-*','runner wait job-?','runner wait [ab]',
                        'runner wait {job-1,--cancel}','runner wait ~',
                        'runner wait =(make)', 'task-board spawn wait RUN-example --cursor =(make)',
                        'runner wait %JOB%', 'runner wait !JOB!']:
            with self.subTest(command=command):
                self.assertIsNone(observation_call('exec_command', {'cmd':command}, rules))
        # A literal argv list bypasses the shell; its wildcard characters do not expand.
        self.assertEqual(observation_call('shell', {'command':['runner','wait','job-*']}, rules), 'wait')

    def test_code_mode_namespaces_and_direct_mcp_names_agree(self):
        self.assertEqual(observation_call('clock__sleep', {'duration_ms':1000}), 'wait')
        self.assertEqual(observation_call('collaboration__wait_agent', {}), 'wait')
        rules = [{'kind':'wait','tool':'mcp__runner__await_job','arguments':{'cancel':False}}]
        self.assertEqual(observation_call('mcp__runner.await_job', {'cancel':False}, rules), 'wait')
        self.assertEqual(observation_call('mcp__runner__await_job', {'cancel':False}, rules), 'wait')

    def test_nested_tool_constraints_are_type_sensitive(self):
        rules = [{'kind':'wait','tool':'await_job','arguments':{'options':{'cancel':False}}}]
        self.assertEqual(observation_call('await_job', {'options':{'cancel':False}}, rules), 'wait')
        self.assertIsNone(observation_call('await_job', {'options':{'cancel':0}}, rules))
        rules = [{'kind':'wait','tool':'await_job','arguments':{'options':[False]}}]
        self.assertIsNone(observation_call('await_job', {'options':[0]}, rules))

    def test_rule_validation(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'rules.json'
            valid = {'version':1,'rules':[{'kind':'wait','argv':['runner','wait','*']}]}
            path.write_text(json.dumps(valid))
            self.assertEqual(load_rules(path),valid['rules'])
            for bad in [{'version':2}, {'version':1,'rules':[{'kind':'wait','argv':['*']}]},
                        {'version':1,'rules':[{'kind':'wait','tool':'runner','code':'do()'}]},
                        {'version':1,'rules':[{'kind':'wait','argv':['runner'],'tool':'runner'}]},
                        {'version':1,'rules':[{'kind':'wait','tool':'functions.exec'}]},
                        {'version':1,'rules':[{'kind':'wait','tool':'exec_command'}]}]:
                path.write_text(json.dumps(bad))
                with self.assertRaises(ValueError): load_rules(path)
