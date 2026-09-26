#!/usr/bin/env python3
"""Audit Codex CLI session rollouts for goal-mode token burn.

Everything runs locally on the rollout files under ~/.codex/sessions.
Nothing is uploaded. Python 3.9+, standard library only.

Subcommands:
  summary   per-session table: goal turns, recognized waits, observed tokens
  limits    weekly / 5-hour limit exhaustion episodes from rate_limits snapshots
  session   per-turn timeline of one rollout file (goal continuations, gaps)
  floatbug  tool calls rejected with "invalid type: floating point"

Background: https://relux.works/en/blog/codex-goal-token-burn/
"""
import argparse
import collections
import datetime as dt
import glob
import json
import os
import re
import statistics
import sys

GOAL_MARKER = "Continue working toward the active thread goal"
TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens")
LIMIT_TEXT = "hit your usage limit"
FLOAT_ERR = re.compile(r"invalid type: floating point `?([0-9.]+)`?, expected (\w+)")


def ts(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def fmt_ts(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


def iter_records(path: str):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def find_rollouts(roots, include_backups=False):
    """Yield rollout files. Codex writes `...-----backup.jsonl` copies next to
    some rollouts; they duplicate the main file and are skipped unless asked."""
    seen = set()
    for root in roots:
        root = os.path.expanduser(root)
        paths = [root] if os.path.isfile(root) else sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True))
        for p in paths:
            if "backup" in os.path.basename(p) and not include_backups:
                continue
            identity = os.path.realpath(p)
            if identity in seen:
                continue
            seen.add(identity)
            yield p


# ---------------------------------------------------------------- per-turn parse

def new_turn(stamp, turn_id):
    return {"id": turn_id, "start": stamp, "end": stamp, "kind": "user", "calls": 0,
            "names": collections.Counter(), "inp": 0, "cached": 0, "out": 0,
            "ctx": 0, "cmds": collections.Counter(), "wait_calls": 0,
            "has_text": False, "complete": False, "context_seen": False}


def token_values(value):
    if not isinstance(value, dict) or "input_tokens" not in value or "output_tokens" not in value:
        return None
    values = tuple(value.get(key, 0) for key in TOKEN_FIELDS)
    return values if all(type(n) is int and n >= 0 for n in values) else None


def waiting_call(name, args):
    """Recognize explicit waiting attempts, never infer command intent from a shell tool name."""
    if not isinstance(args, dict):
        return False
    if name.startswith("functions."):
        name = name[len("functions."):]
    if name == "clock.sleep":
        return type(args.get("duration_ms")) is int and args["duration_ms"] > 0
    if name in ("wait_agent", "collaboration.wait_agent"):
        return True
    if name == "wait":
        return bool(args.get("cell_id")) and args.get("terminate", False) is False
    if name == "write_stdin":
        return "session_id" in args and args.get("chars", "") == ""
    if name in ("exec_command", "shell", "shell_command"):
        command = args.get("cmd", args.get("command", ""))
        if isinstance(command, list):
            if len(command) != 2 or not all(isinstance(part, str) for part in command):
                return False
            command = " ".join(command)
        return isinstance(command, str) and re.fullmatch(r"\s*(?:/bin/)?sleep\s+\d+(?:\.\d+)?\s*", command) is not None
    return False


def parse_turns(path: str):
    """Read observed turn usage; cumulative snapshots are not individual model requests."""
    meta = {"path": path, "model": None, "provider": None, "cwd": None, "cli": None,
            "usage_warnings": collections.Counter()}
    turns = []
    cur = None
    calls = {}
    float_errors = []
    previous_usage = None
    for r in iter_records(path):
        t = r.get("type")
        p = r.get("payload") or {}
        stamp = r.get("timestamp")
        if t == "session_meta":
            meta["provider"] = p.get("model_provider")
            meta["cwd"] = p.get("cwd")
            meta["cli"] = p.get("cli_version")
            continue
        if t == "turn_context" or (t == "event_msg" and p.get("type") == "task_started"):
            turn_id = p.get("turn_id")
            if (cur is None or cur["complete"] or (turn_id and cur["id"] and turn_id != cur["id"])
                    or (t == "turn_context" and not turn_id and cur["context_seen"])):
                cur = new_turn(stamp, turn_id)
                turns.append(cur)
            elif t == "event_msg":
                cur["start"] = stamp
            cur["id"] = turn_id or cur["id"]
            meta["model"] = p.get("model") or meta["model"]
            cur["context_seen"] |= t == "turn_context"
            continue
        if t == "event_msg" and p.get("type") == "token_count":
            info = p.get("info") or {}
            total = token_values(info.get("total_token_usage"))
            last = token_values(info.get("last_token_usage"))
            if total is None:
                if info:
                    meta["usage_warnings"]["missing_totals"] += 1
                continue
            reset = previous_usage is not None and any(a < b for a, b in zip(total, previous_usage))
            if previous_usage is None or reset:
                delta = tuple(min(a, b) for a, b in zip(total, last)) if last and cur is not None else (0, 0, 0)
                meta["usage_warnings"]["baseline_input_excluded"] += total[0] - delta[0]
                if reset:
                    meta["usage_warnings"]["counter_resets"] += 1
                if last is None:
                    meta["usage_warnings"]["missing_initial_usage"] += 1
            else:
                delta = tuple(a - b for a, b in zip(total, previous_usage))
            previous_usage = total
            if cur is not None:
                cur["inp"] += delta[0]
                cur["cached"] += delta[1]
                cur["out"] += delta[2]
                if delta[0] and last:
                    cur["ctx"] = max(cur["ctx"], last[0])
                if stamp and not cur["complete"]:
                    cur["end"] = stamp
            continue
        if cur is None:
            continue
        if t == "event_msg" and p.get("type") in ("task_complete", "turn_aborted"):
            if p.get("turn_id") in (None, cur["id"]):
                cur["end"] = stamp
                cur["complete"] = True
                cur["has_text"] |= bool(p.get("last_agent_message"))
            continue
        if stamp and t == "response_item" and not cur["complete"]:
            cur["end"] = stamp
        if t == "response_item":
            pt = p.get("type")
            if pt == "message":
                txt = "\n".join(part.get("text", "") for part in (p.get("content") or []) if isinstance(part, dict))
                goal_text = re.sub(r'^(?:<goal_context>|<codex_internal_context source="goal">)\s*', '', txt.strip())
                if p.get("role") == "user" and goal_text.startswith(GOAL_MARKER):
                    cur["kind"] = "goal"
                if p.get("role") == "assistant":
                    cur["has_text"] |= bool(txt.strip())
            elif pt in ("function_call", "custom_tool_call"):
                name = p.get("name") or "unknown"
                if p.get("namespace"):
                    name = f"{p['namespace']}.{name}"
                cur["calls"] += 1
                cur["names"][name] += 1
                calls[p.get("call_id")] = name
                try:
                    a = json.loads(p.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    a = {}
                cur["wait_calls"] += waiting_call(name, a)
                if name.removeprefix("functions.") in ("exec_command", "shell", "shell_command") and isinstance(a, dict):
                    cmd = a.get("cmd") or a.get("command") or ""
                    cmd = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
                    cur["cmds"][re.sub(r"\s+", " ", cmd)[:60]] += 1
            elif isinstance(pt, str) and pt.endswith("_call"):
                # Native calls (web search, computer, image generation, etc.) are work too.
                cur["calls"] += 1
                cur["names"][pt] += 1
            elif pt in ("function_call_output", "custom_tool_call_output"):
                out = p.get("output")
                s = out if isinstance(out, str) else json.dumps(out)
                m = FLOAT_ERR.search(s or "")
                if m:
                    float_errors.append((calls.get(p.get("call_id")), m.group(1), m.group(2)))
    meta["float_errors"] = float_errors
    return meta, turns


def is_wait_only(turn) -> bool:
    return turn["calls"] > 0 and turn["calls"] == turn["wait_calls"]


def duration(turn) -> float:
    try:
        return max(0, ts(turn["end"]) - ts(turn["start"]))
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------- summary

def cmd_summary(args):
    rows = []
    for path in find_rollouts(args.roots, args.include_backups):
        meta, turns = parse_turns(path)
        if not turns:
            continue
        goal = [t for t in turns if t["kind"] == "goal"]
        waiting = [t for t in goal if is_wait_only(t)]
        no_tools = [t for t in goal if t["calls"] == 0]
        rows.append({
            "schema_version": 2,
            "file": os.path.basename(path),
            "model": meta["model"], "provider": meta["provider"],
            "turns": len(turns), "goal_turns": len(goal),
            "wait_only_goal_turns": len(waiting), "no_tool_goal_turns": len(no_tools),
            "unclassified_goal_turns": len(goal) - len(waiting) - len(no_tools),
            "wait_only_goal_hours": sum(duration(t) for t in waiting) / 3600,
            "wait_only_goal_input": sum(t["inp"] for t in waiting),
            "no_tool_goal_input": sum(t["inp"] for t in no_tools),
            "goal_input": sum(t["inp"] for t in goal),
            "input": sum(t["inp"] for t in turns),
            "cached": sum(t["cached"] for t in turns),
            "output": sum(t["out"] for t in turns),
            "max_context": max(t["ctx"] for t in turns),
            "float_errors": len(meta["float_errors"]),
            "usage_warnings": dict(meta["usage_warnings"]),
        })
    if args.json:
        json.dump(rows, sys.stdout, indent=1)
        return
    if not rows:
        print("no rollouts found. Codex keeps them under ~/.codex/sessions/YYYY/MM/DD/*.jsonl;")
        print("pass that directory (or wherever you archive them) as an argument.")
        return
    total_in = sum(r["input"] for r in rows)
    goal_rows = [r for r in rows if r["goal_turns"]]
    goal_session_in = sum(r["input"] for r in goal_rows)
    goal_in = sum(r["goal_input"] for r in goal_rows)
    wait_h = sum(r["wait_only_goal_hours"] for r in goal_rows)
    wait_in = sum(r["wait_only_goal_input"] for r in goal_rows)
    wait_turns = sum(r["wait_only_goal_turns"] for r in goal_rows)
    no_tool_turns = sum(r["no_tool_goal_turns"] for r in goal_rows)

    print("VERDICT")
    print(f"  {len(rows)} rollout files, {total_in/1e9:.2f}B observed input tokens, {sum(r['cached'] for r in rows)/max(total_in,1):.0%} cached.")
    if not goal_rows:
        print("  No goal continuation prompts recognized in these rollouts.")
    else:
        print(f"  {len(goal_rows)} files contain goal continuations; their entire sessions used {goal_session_in/max(total_in,1):.0%} of observed input.")
        print(f"  Goal continuation turns themselves used {goal_in/1e9:.2f}B input tokens.")
        if wait_turns:
            print(f"  {wait_turns} goal turns used only recognized waiting calls: {wait_h:.3f} turn-hours, {wait_in/1e9:.3f}B input tokens.")
            if wait_h:
                print(f"  {wait_in/wait_h/1e6:.1f}M input tokens per hour across those turns.")
        print(f"  {no_tool_turns} goal turns made no tool calls; this alone does not establish lack of progress.")
    print("  Waiting calls may return useful results or errors. These totals are not a measurement of waste or subscription charges.")
    warnings = collections.Counter()
    for row in rows:
        warnings.update(row["usage_warnings"])
    if any(warnings.values()):
        print(f"  Usage accounting caveats: {dict(warnings)}. See README for coverage limits.")
    fe = sum(r["float_errors"] for r in rows)
    print(f"  Float-argument rejections: {fe}.")
    print()
    print("Columns: wait = recognized wait-only goal turns; none = no-tool goal turns;")
    print("other = unclassified goal turns; hours = whole wait-only turn durations, not measured sleep time;")
    print("M/h = input per such hour; ctx = largest reported last input. Top files by input:")
    print()
    print(f"{'input':>8} {'goal':>5} {'wait':>5} {'none':>5} {'other':>5} {'hours':>7} {'M/h':>7} {'ctx':>6}  model  file")
    for r in sorted(rows, key=lambda r: -r["input"])[: args.top]:
        mph = r["wait_only_goal_input"] / r["wait_only_goal_hours"] / 1e6 if r["wait_only_goal_hours"] else 0
        print(f"{r['input']/1e9:7.2f}B {r['goal_turns']:5} {r['wait_only_goal_turns']:5} {r['no_tool_goal_turns']:5} {r['unclassified_goal_turns']:5} {r['wait_only_goal_hours']:7.3f} {mph:7.1f} {r['max_context']/1e3:5.0f}K  {r['model']}  {r['file'][8:27]}")


# ----------------------------------------------------------------------- limits

def cmd_limits(args):
    windows = collections.defaultdict(list)  # window_minutes -> [(t, used, resets_at)]
    limit_hits = set()
    for path in find_rollouts(args.roots, args.include_backups):
        for r in iter_records(path):
            p = r.get("payload") or {}
            stamp = r.get("timestamp")
            if not stamp:
                continue
            if r.get("type") == "event_msg":
                rl = p.get("rate_limits")
                if rl and (rl.get("limit_id") in (None, "codex", "codex_bengalfox")):
                    for slot in ("primary", "secondary"):
                        w = rl.get(slot) or {}
                        if w.get("window_minutes") in (300, 10080) and w.get("used_percent") is not None:
                            windows[w["window_minutes"]].append((ts(stamp), w["used_percent"], w.get("resets_at")))
            # The limit text is echoed by several record kinds for one event
            # (tool output, item_completed, compaction summary), so count
            # distinct minutes per file rather than raw records.
            if r.get("type") in ("response_item", "event_msg") and LIMIT_TEXT in json.dumps(p):
                limit_hits.add((path, stamp[:16]))
    days = {k[1][:10] for k in limit_hits}
    if not windows:
        print("VERDICT")
        print("  No rate_limits snapshots in these rollouts. They are attached to token_count events")
        print("  for ChatGPT-plan accounts; API-key accounts don't get them, so this check does not")
        print("  apply. `summary` still works for you.")
        return
    print(f"'{LIMIT_TEXT}' events: {len(limit_hits)} on {len(days)} distinct days")
    for wm, label in ((10080, "weekly"), (300, "5-hour")):
        pts = sorted(windows[wm])
        if not pts:
            print(f"\n{label}: no rate_limits samples")
            continue
        per_min = {}
        for t, u, rs in pts:
            k = int(t // 60)
            if k not in per_min or u > per_min[k][1]:
                per_min[k] = (t, u, rs)
        seq = [per_min[k] for k in sorted(per_min)]
        episodes, inep = [], False
        for t, u, rs in seq:
            if u >= 99 and not inep:
                inep = True
                episodes.append([t, t])
            elif inep and u >= 80:
                episodes[-1][1] = t
            elif inep and u < 80:
                inep = False
        print(f"\n{label} window: {len(pts)} samples, {fmt_ts(seq[0][0])} .. {fmt_ts(seq[-1][0])}")
        print(f"exhaustion episodes (>=99%, exit <80%): {len(episodes)}")
        for a, b in episodes:
            print(f"  {fmt_ts(a)} .. {fmt_ts(b)}")
        if wm == 10080:
            byw = collections.defaultdict(list)
            for t, u, rs in seq:
                if rs:
                    byw[rs].append((t, u))
            hours = []
            print("time from window start to 99% (windows that reached it):")
            for rs, wpts in sorted(byw.items()):
                start = rs - wm * 60
                f99 = next((t for t, u in wpts if u >= 99), None)
                if f99:
                    h = (f99 - start) / 3600
                    hours.append(h)
                    print(f"  window {fmt_ts(start)}: {h:.1f}h")
            if hours:
                hours.sort()
                print(f"median {statistics.median(hours):.0f}h, fastest {hours[0]:.0f}h, slowest {hours[-1]:.0f}h")
            print()
            print("VERDICT")
            if episodes:
                print(f"  Your weekly window reached 99% {len(episodes)} time(s) in this archive" +
                      (f", typically {statistics.median(hours):.0f} hours after the window started." if hours else "."))
                print("  A 7-day window emptied in about a day is the post's symptom; check `summary`")
                print("  for goal sessions around those dates.")
            else:
                print("  Your weekly window never reached 99% in this archive.")


# ---------------------------------------------------------------------- session

def cmd_session(args):
    meta, turns = parse_turns(args.file)
    print(f"model={meta['model']} provider={meta['provider']} cli={meta['cli']} turns={len(turns)} goal={sum(1 for t in turns if t['kind']=='goal')}")
    prev_end = None
    print(f"{'#':>3} {'start':16} {'kind':4} {'gap_s':>6} {'dur_s':>6} {'calls':>5} {'in_M':>6} {'ctx_K':>6}  top tools / commands")
    for i, t in enumerate(turns):
        gap = ts(t["start"]) - ts(prev_end) if prev_end and t["start"] else 0
        tools = ", ".join(f"{n}×{c}" for n, c in t["names"].most_common(2))
        cmd = t["cmds"].most_common(1)
        extra = f"  | {cmd[0][1]}× {cmd[0][0]}" if cmd and args.commands else ""
        print(f"{i:3} {t['start'][:16] if t['start'] else '':16} {t['kind']:4} {gap:6.2f} {duration(t):6.0f} {t['calls']:5} {t['inp']/1e6:6.1f} {t['ctx']/1e3:6.0f}  {tools}{extra}")
        prev_end = t["end"]
    goal = [t for t in turns if t["kind"] == "goal"]
    if goal:
        gaps = []
        prev_end = None
        for t in turns:
            if t["kind"] == "goal" and prev_end:
                gaps.append(ts(t["start"]) - ts(prev_end))
            prev_end = t["end"]
        if gaps:
            gaps.sort()
            print(f"\ngap before goal continuations: median {statistics.median(gaps):.2f}s, min {gaps[0]:.2f}s, max {gaps[-1]:.2f}s")
        waiting = [t for t in goal if is_wait_only(t)]
        print(f"goal turns {len(goal)}, recognized wait-only {len(waiting)}, no tools {sum(1 for t in goal if t['calls']==0)}, observed input in goal turns {sum(t['inp'] for t in goal)/1e6:.0f}M")
    if any(meta["usage_warnings"].values()):
        print("usage accounting caveats:", dict(meta["usage_warnings"]))
    if meta["float_errors"]:
        c = collections.Counter((n, e) for n, _, e in meta["float_errors"])
        print("float-arg rejections:", dict(c))


# --------------------------------------------------------------------- floatbug

def cmd_floatbug(args):
    by_model = collections.Counter()
    by_tool = collections.Counter()
    for path in find_rollouts(args.roots, args.include_backups):
        meta, _ = parse_turns(path)
        for name, val, expected in meta["float_errors"]:
            by_model[meta["model"]] += 1
            by_tool[(name, expected)] += 1
    if not by_model:
        print("VERDICT")
        print("  No 'invalid type: floating point' rejections found in the selected rollouts.")
        return
    print("VERDICT")
    print(f"  {sum(by_model.values())} tool calls were rejected because the model sent a float where Codex")
    print("  expects an integer. This count does not establish that other calls or all waiting tools failed.")
    print()
    print("rejections by model:")
    for m, n in by_model.most_common():
        print(f"  {n:5} {m}")
    print("rejections by tool / expected type:")
    for (name, exp), n in by_tool.most_common():
        print(f"  {n:5} {name} expected {exp}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    default_root = os.path.expanduser("~/.codex/sessions")
    for name, fn in (("summary", cmd_summary), ("limits", cmd_limits), ("floatbug", cmd_floatbug)):
        s = sub.add_parser(name)
        s.add_argument("roots", nargs="*", default=[default_root], help="rollout directories or files (default ~/.codex/sessions)")
        s.add_argument("--include-backups", action="store_true", help="also read the *-----backup.jsonl copies")
        if name == "summary":
            s.add_argument("--top", type=int, default=15)
            s.add_argument("--json", action="store_true")
        s.set_defaults(fn=fn)
    s = sub.add_parser("session")
    s.add_argument("file")
    s.add_argument("--commands", action="store_true", help="show the most repeated shell command per turn")
    s.set_defaults(fn=cmd_session)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
