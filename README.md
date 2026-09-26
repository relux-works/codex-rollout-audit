# codex-rollout-audit

Inspect Codex CLI rollouts for goal continuations, waiting calls, token usage,
quota snapshots, and rejected numeric arguments. Background:
[goal-mode token burn](https://relux.works/en/blog/codex-goal-token-burn/)
([русская версия](https://relux.works/ru/blog/codex-goal-token-burn/)).

Python CLI with a small recognition module, standard library only, Python 3.9+. Reads local files;
does not upload data or modify rollouts. Works on Linux, macOS, and Windows.

## Tools and commands

```bash
git clone https://github.com/relux-works/codex-rollout-audit
cd codex-rollout-audit
python3 codex_rollout_audit.py summary
python3 codex_rollout_audit.py summary /path/to/rollouts --json
python3 codex_rollout_audit.py limits
python3 codex_rollout_audit.py floatbug
python3 codex_rollout_audit.py session /path/to/rollout.jsonl --commands
python3 -m unittest -v
```

Git obtains the source. Python runs the CLI and synthetic regression tests;
no additional packages are required. Commands print to stdout. Redirect output
to `.temp/` for local reports and logs. On Windows, use `python` if needed.
GitHub Actions runs the same tests; its output is in the workflow logs.

`summary`, `limits`, and `floatbug` accept multiple directories or files,
defaulting to `~/.codex/sessions`. Overlapping paths and symlinks to the same
file are read once. Backup files are excluded unless `--include-backups` is
set. Separate copies or inherited histories in forked files are not deduplicated;
select a nonoverlapping archive for aggregate totals. JSON identifies `session_id`
and `forked_from`; text reports warn when forks are present. The first session
metadata record owns the file, even when a fork embeds parent metadata later.
A fork's continuation prompts may be inherited: their presence does not prove
that the child activated its own goal. Timestamps can also be rewritten on fork.

Managed orchestrators may store rollouts in private `CODEX_HOME` directories.
Pass those rollout directories explicitly; the default scans only `~/.codex/sessions`.
Do not add both a copied history and its original to a usage aggregate.

## Reading the results

**`summary`** prints observed usage and a table of goal continuations.
`--top N` changes the number of rows; `--json` returns all rows.

- `input`, `cached`, `output`: observed token increments across the file.
- `goal_input`: input attributed only to recognized goal continuation turns.
  The separate session share includes all work in files containing a goal.
- `wait`: goal turns whose calls are all recognized waiting attempts.
- `none`: goal turns with no tool calls. Text or reasoning can still be useful.
- `other`: goal turns with calls whose intent is unclassified or mixed.
- `hours`: whole durations of wait-only turns, including inference and reporting.
  This is not a measurement of time blocked inside a tool.
- `M/h`: observed input per hour of those turns, calculated before rounding.
- `ctx`: largest reported `last_token_usage.input_tokens` on a usage increment.

Recognized waits are `clock.sleep`, `wait_agent`, nonterminating `wait`,
empty-input `write_stdin`, and a simple shell `sleep NUMBER` command. Supported
namespaces are recognized too. The built-in task-board profile recognizes
`spawn wait/observe/watch` as waits, and `spawn status/events` as monitoring
(`events --follow` is a wait). Launching an agent and sending directives are
unclassified work. Shell pipelines, compound commands and expansions fail closed.
Calls may return useful results or errors: classification does not prove that a
wait succeeded, a child was still running, or any tokens were wasted.

Code-mode `exec` is inspected without executing it. Only entire scripts made of
literal `text(await tools.NAME({...}));` statements or
`const r = await tools.NAME({...}); text(r.output);` (also `let`, `text(r)`) are
recognized. Flat literal arguments, multiple statements and a leading `@exec`
pragma are supported. Variables in arguments, loops, arbitrary expressions,
`Promise.all`, and other JavaScript remain opaque. A mixed script containing
an agent launch or build never becomes wait-only just because it also waits.

Additional JSON fields expose coverage:

- `observation_only_goal_turns`: turns containing only recognized waits and
  monitoring calls; includes `wait_only_goal_turns`. Monitoring-only/mixed
  observation turns remain in the existing `unclassified_goal_turns` field.
- `goal_wait_calls`, `goal_monitor_calls`: outer tool calls classified as waits
  or observations (an entirely recognized exec wrapper counts once).
- `goal_nested_wait_calls`, `goal_nested_monitor_calls`: recognized calls inside
  fully parsed exec scripts, including scripts that also do work. These overlap
  with the outer counters; **do not add them together**.
- `goal_opaque_exec_calls`: exec scripts outside the accepted grammar. A parsed
  script may still contain commands whose intent is unknown.

Per-call counters identify waiting inside productive turns. They do not assign
input tokens to individual calls. `tail`, arbitrary `gh` commands, board queries,
and unrecognized tools remain unclassified. Inspect `session --commands` when
appropriate; command display covers direct shell calls only.

## Other orchestrators

Tool/exec parsing does not depend on task-board. Add CLI or MCP semantics with
an optional declarative file, without changing Python code:

```json
{
  "version": 1,
  "rules": [
    {"kind": "wait", "argv": ["runner", "jobs", "wait", "*", "--timeout", "*"]},
    {"kind": "monitor", "argv": ["runner", "jobs", "status", "*"]},
    {"kind": "wait", "tool": "mcp__runner__await_job", "arguments": {"cancel": false}}
  ]
}
```

```bash
python3 codex_rollout_audit.py summary /path/to/rollouts --rules rules.json --json
python3 codex_rollout_audit.py session /path/to/rollout.jsonl --rules rules.json
```

`argv` matches the **entire** tokenized simple command; `*` matches exactly one
argument. Specify separate rules for optional flags and full executable paths.
Tool rules match a name and any required literal arguments (including nested
JSON values, with type-sensitive equality); omitted constraints
mean every call of that tool. Rules apply to direct calls and recognized exec
wrappers. Direct `namespace.name` and code-mode `namespace__name` tool names
are normalized consistently. Conflicting matching kinds stay unclassified. Matching custom rules
precede built-ins. Tool rules cannot override `exec` or shell wrappers; use
`argv` rules for their commands. Shell compounds/expansions are rejected even
with custom rules.

Rules are user-supplied semantic assumptions, not independently verified facts;
JSON reports their count as `custom_rules`, and text reports disclose their use.
No configuration can prove that a read was useless. Configure only operations
whose meaning you know, and retain the rules alongside reports for reproducibility.

## Goal and quota interpretation

Goal detection recognizes the continuation prompt at the beginning of a user
message, including native `codex_internal_context source="goal"` and legacy
`goal_context` wrappers.
It does not reconstruct the full persisted goal lifecycle or prove the message's
origin. Turn IDs and `task_started` / `task_complete` events provide boundaries;
older logs fall back to `turn_context` and the last observed response/usage event.

**`session FILE`** shows turn timestamps, gaps, duration, tool calls, input and
context size. Gaps from logs without lifecycle events are estimates, not exact
scheduler delays. `--commands` includes the most repeated shell command.

**`limits`** summarizes available quota snapshots: episodes reaching 99%, with an
exit threshold below 80%, and time to the first observed 99% sample relative to
`resets_at - window_minutes`. These are sampled estimates; reset semantics and
missing observations affect them. Supply logs from one account at a time.
Text matches of usage-limit errors can include quoted messages. These observations
do not establish that goals caused the quota consumption.

**`floatbug`** counts observed integer-parser rejections by tool and model.
It does not infer failure of other waiting calls from one rejected call.

## Token accounting and limitations

Usage is calculated from differences between successive `total_token_usage`
snapshots across the file. Repeated snapshots add zero, including across turn
boundaries; equal-sized requests with increasing totals are counted separately.

For the first snapshot or a counter reset, only `last_token_usage`, bounded by
the cumulative values, is included. Earlier cumulative input is reported as
`baseline_input_excluded`, not charged to the current turn. Snapshots before the
first turn establish a baseline without charging a turn. Missing cumulative
values are skipped and reported as `missing_totals`; missing first-request usage
and resets also produce caveats. Truncated or unusual logs may therefore be
incomplete. Deltas spanning missing records cannot reliably be assigned to an
individual request or turn. Token snapshots are not counted as model requests.

Cached input is a subset of input, not an additional charge. Raw tokens do not
directly determine API prices or subscription quota percentages. Session shares,
waiting-call counts, and no-tool turns do not by themselves measure wasted work.
Previous article totals require a fresh audit before comparison with this version.

JSON rows now contain `schema_version: 2`. The old heuristic fields were replaced:

| Previous field | Version 2 field |
| --- | --- |
| `poll_only_goal_turns` | `wait_only_goal_turns` |
| `empty_goal_turns` | `no_tool_goal_turns` |
| `goal_wait_hours` | `wait_only_goal_hours` |
| `goal_wait_input` | `wait_only_goal_input` |

The renamed fields have narrower interpretations; update consumers accordingly.
Additional fields expose `goal_input`, `no_tool_goal_input`,
`unclassified_goal_turns`, and `usage_warnings`.

## Privacy and versions

Rollouts can contain prompts, file contents, commands and credentials. Default
reports print counts, filenames, model/provider and tool names. `--commands`
also prints command prefixes, which may contain secrets; review before sharing.

The parser targets CLI 0.143–0.154 record shapes and newer compatible lifecycle
records. Regression tests use synthetic data, including shapes observed in local
rollouts; private rollout contents are not included. For an unsupported shape, open an
issue with a redacted example. MIT license.
