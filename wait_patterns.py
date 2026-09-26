"""Conservative, non-executing recognizers for literal waiting tool calls."""
import json
import re
import shlex
from pathlib import Path

WRAPPERS = {"exec", "exec_command", "shell", "shell_command"}

# This is a tiny accepted grammar, not a JavaScript or shell interpreter.
STRING = r'''(?:"(?:[^"\\\r\n]|\\.)*"|'(?:[^'\\\r\n]|\\.)*')'''
KEY = rf'(?:{STRING}|[A-Za-z_]\w*)'
VALUE = rf'(?:{STRING}|-?\d+(?:\.\d+)?|true|false|null)'
PROPERTY = rf'{KEY}\s*:\s*{VALUE}'
OBJECT = rf'\{{\s*(?:{PROPERTY}\s*(?:,\s*{PROPERTY}\s*)*,?)?\}}'
CALL = rf'await\s+tools\.(\w+)\s*\(\s*({OBJECT})\s*\)'
INLINE = re.compile(rf'text\(\s*{CALL}\s*\)\s*;')
ASSIGNED = re.compile(rf'(?:const|let)\s+(\w+)\s*=\s*{CALL}\s*;\s*text\(\s*\1(?:\.output)?\s*\)\s*;')
PAIR = re.compile(rf'({KEY})\s*:\s*({VALUE})')


def tool_name(name):
    # Direct namespace.name and code-mode namespace__name denote the same tool.
    return name.removeprefix("functions.").replace(".", "__")


def literal(value):
    if value.startswith("'"):
        # Only the JSON escape subset plus escaped apostrophes is supported.
        value = '"' + value[1:-1].replace('"', '\\"').replace("\\'", "'") + '"'
    return json.loads(value)


def literal_exec_calls(code):
    """Return calls only if the entire script matches literal call/print statements."""
    if not isinstance(code, str) or len(code) > 100_000:
        return None
    code = re.sub(r'^\s*// @exec:[^\n]*\n', '', code).strip()
    calls = []
    while code and len(calls) < 128:
        match = INLINE.match(code)
        if match:
            name, obj = match.groups()
        else:
            match = ASSIGNED.match(code)
            if not match:
                return None
            _, name, obj = match.groups()
        args = {}
        try:
            for key, value in PAIR.findall(obj):
                key = literal(key) if key[0] in "\"'" else key
                if key in args or key == '__proto__':
                    return None
                args[key] = literal(value)
        except (ValueError, TypeError):
            return None
        calls.append((name, args))
        code = code[match.end():].lstrip()
    return calls if calls and not code else None


def load_rules(path):
    """Load opt-in CLI/tool semantics; no code or regexes are evaluated."""
    if path is None:
        return []
    if Path(path).stat().st_size > 1_000_000:
        raise ValueError("rules file exceeds 1 MB")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("rules must have version 1 and a rules array")
    rules = data.get("rules")
    if not isinstance(rules, list) or len(rules) > 128:
        raise ValueError("rules must be an array of at most 128 entries")
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("kind") not in ("wait", "monitor"):
            raise ValueError("each rule needs kind: wait or monitor")
        if set(rule) - {"kind", "argv", "tool", "arguments"}:
            raise ValueError("unknown rule field")
        if "argv" in rule:
            argv = rule["argv"]
            if ("tool" in rule or "arguments" in rule or not isinstance(argv, list)
                    or not argv or len(argv) > 64
                    or not all(isinstance(v, str) and v for v in argv)
                    or argv[0] == "*"):
                raise ValueError("argv rules need a command and exact tokens (* matches one argument)")
        elif not isinstance(rule.get("tool"), str) or not rule["tool"]:
            raise ValueError("rule needs argv or a tool name")
        elif tool_name(rule["tool"]) in WRAPPERS:
            raise ValueError("exec/shell wrappers require argv rules, not tool rules")
        elif not isinstance(rule.get("arguments", {}), dict):
            raise ValueError("tool rule arguments must be an object")
    return rules


def rule_kind(matches):
    kinds = {rule["kind"] for rule in matches}
    return next(iter(kinds)) if len(kinds) == 1 else None


def shell_observation(command, rules=()):
    """Recognize simple commands only; reject expansions, pipelines and side effects."""
    if isinstance(command, list):
        if not command or not all(isinstance(word, str) for word in command):
            return None
        words = command
    else:
        if not isinstance(command, str) or re.search(r'[\n\r;&|<>$`\\(){}\[\]*?~%!]', command):
            return None
        try:
            words = shlex.split(command)
        except ValueError:
            return None
    custom = [r for r in rules if "argv" in r and len(r["argv"]) == len(words)
              and all(expected == "*" or expected == actual
                      for expected, actual in zip(r["argv"], words))]
    if custom:
        return rule_kind(custom)
    if len(words) == 2 and words[0] in ('sleep', '/bin/sleep'):
        return 'wait' if re.fullmatch(r'\d+(?:\.\d+)?', words[1]) else None
    if len(words) < 4 or words[0].split('/')[-1] != 'task-board' or words[1] != 'spawn':
        return None
    action = words[2]
    if action not in ('wait', 'observe', 'watch', 'status', 'events'):
        return None
    # Unknown flags (including output-file options) fail closed. A RUN is required.
    values = {'--timeout', '--cursor', '--format', '--poll-interval', '--board-dir'}
    switches = {'--json', '--any', '--all', '--allow-blocking', '--follow'}
    runs, index = 0, 3
    while index < len(words):
        word = words[index]
        if re.fullmatch(r'RUN-[A-Za-z0-9_-]+', word):
            runs += 1
        elif word == '--until':
            index += 1
            if index >= len(words) or words[index] != 'terminal':
                return None
        elif word in values:
            index += 1
            if index >= len(words) or words[index].startswith('--'):
                return None
        elif word not in switches:
            return None
        index += 1
    if not runs:
        return None
    return 'wait' if action in ('wait', 'observe', 'watch') or '--follow' in words else 'monitor'


def observation_call(name, args, rules=()):
    """Classify the attempted operation, not its result, usefulness or token cost."""
    if not isinstance(args, dict):
        return None
    name = tool_name(name)
    custom = [r for r in rules if name not in WRAPPERS
              and tool_name(r.get("tool", "")) == name
              and all(key in args and json.dumps(args[key], sort_keys=True) == json.dumps(value, sort_keys=True)
                      for key, value in r.get("arguments", {}).items())]
    if custom:
        return rule_kind(custom)
    if name == 'clock__sleep':
        return 'wait' if type(args.get('duration_ms')) is int and args['duration_ms'] > 0 else None
    if name in ('wait_agent', 'collaboration__wait_agent'):
        return 'wait'
    if name == 'wait':
        return 'wait' if args.get('cell_id') and args.get('terminate', False) is False else None
    if name == 'write_stdin':
        return 'wait' if 'session_id' in args and args.get('chars', '') == '' else None
    if name in ('exec_command', 'shell', 'shell_command'):
        return shell_observation(args.get('cmd', args.get('command', '')), rules)
    return None
