# codex-rollout-audit

Check your own Codex CLI sessions for the goal-mode token burn described in
[Why Codex burns a weekly limit in a day while the agent waits for the tide](https://relux.works/en/blog/codex-goal-token-burn/)
([русская версия](https://relux.works/ru/blog/codex-goal-token-burn/)).

One Python file, standard library only, Python 3.9+. It reads the rollout
files Codex already keeps under `~/.codex/sessions` and prints tables.
Nothing is uploaded, nothing is modified.

```bash
git clone https://github.com/relux-works/codex-rollout-audit
cd codex-rollout-audit
python3 codex_rollout_audit.py summary
python3 codex_rollout_audit.py limits
python3 codex_rollout_audit.py floatbug
python3 codex_rollout_audit.py session ~/.codex/sessions/2026/09/10/rollout-...jsonl --commands
```

Every subcommand accepts one or more directories or files; the default is
`~/.codex/sessions`. If you archive rollouts elsewhere, pass those paths.

## Start here

Run `summary` first. Every subcommand now opens with a `VERDICT` block in
plain words, followed by the data it was computed from. A typical result on a
subscription account looks like this:

```
VERDICT
  274 sessions, 9.43B input tokens, 98% of them cache hits.
  3 sessions (1.1%) had goal mode on and used 71% of all input tokens.
  20 goal turns did nothing but poll: 3 hours, 0.17B tokens,
  about 65M input tokens per hour of waiting.
  For scale: waiting on a notification costs roughly 0.5M per hour at the same context.
  This is the pattern from the post: a few goal sessions eating most of the budget.
  Float-argument rejections: 0. Expected for OpenAI models; see `floatbug`.
```

If you never turned goal mode on, the verdict says so and there is nothing
else to look at. `floatbug` reporting zero rejections is the normal result
for OpenAI's own models; the bug only affects some custom-provider models.
`limits` needs the `rate_limits` snapshots that ChatGPT-plan accounts get;
API-key accounts don't have them and the command says so.

## What each subcommand shows

**`summary`** walks every rollout and prints one row per session: input and
output tokens, cache share, how many turns were started by goal mode
(`goal`), how many of those did nothing but poll through the shell (`poll`),
how many had no tool call at all (`empty`), the hours spent in poll-only goal
turns and the input tokens per hour of that waiting (`M/h`). Add `--json` for
machine-readable output, `--top N` to change the table length.

A goal turn is a turn whose user message is the Codex goal continuation
prompt ("Continue working toward the active thread goal"). A poll-only goal
turn is a goal turn where every tool call is `exec_command`, `shell`,
`write_stdin`, `wait`, `exec` or `update_plan`.

**`limits`** reads the `rate_limits` snapshot Codex attaches to every
`token_count` event and reports how many times the weekly and 5-hour windows
reached 99%, how long each weekly window took to get there, and how many
"You've hit your usage limit" messages the rollouts contain, by day.

**`session FILE`** prints one line per turn: who started it (`user` or
`goal`), the gap in seconds between the end of the previous turn and this
one (the 0.03 s restart is visible here), duration, tool calls, input tokens
and the largest context sent. `--commands` adds the most repeated shell
command of each turn, which is where polling loops show up.

**`floatbug`** counts tool calls rejected with
`invalid type: floating point ..., expected u64` (or `i32`), grouped by model
and by tool. Models that send `60000.0` where Codex expects `60000` lose
every long-waiting primitive; see the post for why that matters.

## Reading the numbers

The cost model from the post is `waiting cost = number of checks × context
size`. `summary` gives you both factors per session: `poll` and `M/h` for the
checks, `ctx` for the context. If your goal sessions show hundreds of
poll-only turns at 200K+ context, you are paying for the loop.

Numbers are computed from the `last_token_usage` field of `token_count`
events, so they match what the Codex UI counts. Cached input tokens are
included in `input`; the post explains why that matters for subscriptions.

## Privacy

Rollouts contain your prompts, file contents and command output. The tool
never sends them anywhere and only prints aggregate counts, tool names, and
the first 60 characters of the most repeated shell command per turn when you
ask for `--commands`. Review any output before sharing it.

## Versions

Written against rollouts produced by Codex CLI 0.144 through 0.154. The
record shapes it relies on (`turn_context`, `token_count` with
`rate_limits`, `function_call` / `custom_tool_call` items) have been stable
across those versions. If a newer Codex changes them, please open an issue
with a redacted sample line.

## License

MIT.
