# codex-rollout-audit

Inspect Codex CLI rollouts for goal continuations, waiting calls, token usage,
quota snapshots, and rejected numeric arguments. Background:
[goal-mode token burn](https://relux.works/en/blog/codex-goal-token-burn/)
([русская версия](https://relux.works/ru/blog/codex-goal-token-burn/)).

One Python script, standard library only, Python 3.9+. Reads local files;
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
select a nonoverlapping archive for aggregate totals.

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
namespaces are recognized too. Calls can return results or errors; classification
does not establish that a process was live, a wait succeeded, or tokens were wasted.

Arbitrary shell commands, `tail`, `ls`, `gh`, code-mode `exec`, compound shell
commands, and `update_plan` are unclassified. This deliberately misses some real
polling rather than labeling builds, edits, or analysis as waiting. Inspect a
session with `--commands` to investigate such turns; no shell command is executed.

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

The parser targets CLI 0.144–0.154 record shapes and newer compatible lifecycle
records. Regression tests use synthetic data. For an unsupported shape, open an
issue with a redacted example. MIT license.
