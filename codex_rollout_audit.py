#!/usr/bin/env python3
"""Audit Codex CLI session rollouts for goal-mode token burn.

Everything runs locally on the rollout files under ~/.codex/sessions.
Nothing is uploaded. Python 3.9+, standard library only.

Subcommands:
  summary   per-session table: goal turns, poll-only goal turns, tokens
  limits    weekly / 5-hour limit exhaustion episodes from rate_limits snapshots
  session   per-turn timeline of one rollout file (goal continuations, gaps)
  floatbug  tool calls rejected with "invalid type: floating point"

Background: https://relux.works/en/blog/codex-goal-token-burn/
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import json
import os
import re
import sys

GOAL_MARKER = "Continue working toward the active thread goal"
SHELL_TOOLS = {"exec_command", "shell", "shell_command", "write_stdin", "wait", "exec"}
POLL_TOOLS = SHELL_TOOLS | {"update_plan"}
LIMIT_TEXT = "hit your usage limit"
FLOAT_ERR = re.compile(r"invalid type: floating point `?([0-9.]+)`?, expected (\w+)")


def ts(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def fmt_ts(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


def iter_records(path: str):
    with open(path, errors="replace") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def find_rollouts(roots, include_backups=False):
    """Yield rollout files. Codex writes `...-----backup.jsonl` copies next to
    some rollouts; they duplicate the main file and are skipped unless asked."""
    for root in roots:
        root = os.path.expanduser(root)
        if os.path.isfile(root):
            yield root
            continue
        for p in sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)):
            if "backup" in os.path.basename(p) and not include_backups:
                continue
            yield p


# ---------------------------------------------------------------- per-turn parse

def parse_turns(path: str):
    """Return (meta, turns). Each turn: dict(start, end, kind, calls, names, inp, cached, out, ctx, cmds)."""
    meta = {"path": path, "model": None, "provider": None, "cwd": None, "cli": None}
    turns = []
    cur = None
    calls = {}
    float_errors = []
    for r in iter_records(path):
        t = r.get("type")
        p = r.get("payload") or {}
        stamp = r.get("timestamp")
        if t == "session_meta":
            meta["provider"] = p.get("model_provider")
            meta["cwd"] = p.get("cwd")
            meta["cli"] = p.get("cli_version")
            continue
        if t == "turn_context":
            meta["model"] = p.get("model") or meta["model"]
            cur = {"start": stamp, "end": stamp, "kind": "user", "calls": 0,
                   "names": collections.Counter(), "inp": 0, "cached": 0, "out": 0,
                   "ctx": 0, "cmds": collections.Counter(), "model_calls": 0}
            turns.append(cur)
            continue
        if cur is None:
            continue
        if stamp and (t == "response_item" or (t == "event_msg" and p.get("type") == "token_count")):
            cur["end"] = stamp
        if t == "event_msg" and p.get("type") == "token_count":
            last = ((p.get("info") or {}).get("last_token_usage") or {})
            i = last.get("input_tokens") or 0
            if i:
                cur["model_calls"] += 1
            cur["inp"] += i
            cur["cached"] += last.get("cached_input_tokens") or 0
            cur["out"] += last.get("output_tokens") or 0
            cur["ctx"] = max(cur["ctx"], i)
        elif t == "response_item":
            pt = p.get("type")
            if pt == "message" and p.get("role") == "user":
                txt = json.dumps(p.get("content"))
                if GOAL_MARKER in txt:
                    cur["kind"] = "goal"
            elif pt in ("function_call", "custom_tool_call"):
                name = p.get("name")
                cur["calls"] += 1
                cur["names"][name] += 1
                calls[p.get("call_id")] = name
                if name in ("exec_command", "shell", "shell_command"):
                    try:
                        a = json.loads(p.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        a = {}
                    cmd = a.get("cmd") or a.get("command") or ""
                    cmd = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
                    cur["cmds"][re.sub(r"\s+", " ", cmd)[:60]] += 1
            elif pt in ("function_call_output", "custom_tool_call_output"):
                out = p.get("output")
                s = out if isinstance(out, str) else json.dumps(out)
                m = FLOAT_ERR.search(s or "")
                if m:
                    float_errors.append((calls.get(p.get("call_id")), m.group(1), m.group(2)))
    meta["float_errors"] = float_errors
    return meta, turns


def is_poll_only(turn) -> bool:
    return turn["calls"] > 0 and set(turn["names"]) <= POLL_TOOLS


def duration(turn) -> float:
    try:
        return ts(turn["end"]) - ts(turn["start"])
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
        poll = [t for t in goal if is_poll_only(t)]
        empty = [t for t in goal if t["calls"] == 0]
        rows.append({
            "file": os.path.basename(path),
            "model": meta["model"], "provider": meta["provider"],
            "turns": len(turns), "goal_turns": len(goal),
            "poll_only_goal_turns": len(poll), "empty_goal_turns": len(empty),
            "goal_wait_hours": round(sum(duration(t) for t in poll) / 3600, 1),
            "goal_wait_input": sum(t["inp"] for t in poll),
            "input": sum(t["inp"] for t in turns),
            "cached": sum(t["cached"] for t in turns),
            "output": sum(t["out"] for t in turns),
            "max_context": max(t["ctx"] for t in turns),
            "float_errors": len(meta["float_errors"]),
        })
    if args.json:
        json.dump(rows, sys.stdout, indent=1)
        return
    total_in = sum(r["input"] for r in rows) or 1
    goal_rows = [r for r in rows if r["goal_turns"]]
    goal_in = sum(r["input"] for r in goal_rows)
    print(f"sessions: {len(rows)}  input tokens: {total_in/1e9:.2f}B  cached: {sum(r['cached'] for r in rows)/total_in:.1%}")
    print(f"goal sessions: {len(goal_rows)} ({len(goal_rows)/max(1,len(rows)):.1%})  their input: {goal_in/1e9:.2f}B ({goal_in/total_in:.0%} of all)")
    wait_h = sum(r["goal_wait_hours"] for r in goal_rows)
    wait_in = sum(r["goal_wait_input"] for r in goal_rows)
    if wait_h:
        print(f"poll-only goal turns: {sum(r['poll_only_goal_turns'] for r in goal_rows)}  hours: {wait_h:.1f}  tokens: {wait_in/1e9:.2f}B  per hour: {wait_in/wait_h/1e6:.0f}M")
    print(f"empty goal turns: {sum(r['empty_goal_turns'] for r in goal_rows)}   float-arg rejections: {sum(r['float_errors'] for r in rows)}")
    print()
    print(f"{'input':>8} {'goal':>5} {'poll':>5} {'empty':>5} {'wait_h':>6} {'M/h':>5} {'ctx':>6}  model  file")
    for r in sorted(rows, key=lambda r: -r["input"])[: args.top]:
        mph = r["goal_wait_input"] / r["goal_wait_hours"] / 1e6 if r["goal_wait_hours"] else 0
        print(f"{r['input']/1e9:7.2f}B {r['goal_turns']:5} {r['poll_only_goal_turns']:5} {r['empty_goal_turns']:5} {r['goal_wait_hours']:6.1f} {mph:5.0f} {r['max_context']/1e3:5.0f}K  {r['model']}  {r['file'][8:27]}")


# ----------------------------------------------------------------------- limits

def cmd_limits(args):
    windows = collections.defaultdict(list)  # window_minutes -> [(t, used, resets_at)]
    limit_msgs = collections.Counter()
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
            if r.get("type") == "response_item" and p.get("type") == "message":
                if LIMIT_TEXT in json.dumps(p.get("content")):
                    limit_msgs[stamp[:10]] += 1
            elif r.get("type") == "event_msg" and isinstance(p.get("message"), str) and LIMIT_TEXT in p["message"]:
                limit_msgs[stamp[:10]] += 1
    print(f"'{LIMIT_TEXT}' messages: {sum(limit_msgs.values())} on {len(limit_msgs)} distinct days")
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
                print(f"median {hours[len(hours)//2]:.0f}h, fastest {hours[0]:.0f}h, slowest {hours[-1]:.0f}h")


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
            print(f"\ngap before goal continuations: median {gaps[len(gaps)//2]:.2f}s, min {gaps[0]:.2f}s, max {gaps[-1]:.2f}s")
        poll = [t for t in goal if is_poll_only(t)]
        print(f"goal turns {len(goal)}, poll-only {len(poll)}, empty {sum(1 for t in goal if t['calls']==0)}, input in goal turns {sum(t['inp'] for t in goal)/1e6:.0f}M")
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
        print("no 'invalid type: floating point' rejections found")
        return
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
