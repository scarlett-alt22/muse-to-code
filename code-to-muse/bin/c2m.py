#!/usr/bin/env python3
"""code-to-muse: a file-drop channel for Claude Code to hand tasks TO a
personal AI agent (e.g. Meta's Muse). The reverse of muse-to-code.

Directory protocol (the agent's side is spelled out in AGENT.md):

    inbox/<id>.task.md      written by Claude Code (temp file + rename, so the
                            agent never reads half a task)
    working/<id>.task.md    the agent claims a task by renaming it here
                            (rename is atomic: a task cannot be claimed twice)
    outbox/<id>.result.md   the agent's result (temp file + rename as well)
    done/<id>.task.md       the agent moves the task here after writing the result
    reviewed/<id>.*         Claude Code moves both here once it has checked
                            the result; anything not in reviewed/ is unverified

Two design rules:
1. A self-reported result is not a result. `status` checks that a result is
   well-formed (and that its Evidence section is not empty); it never claims
   the work is correct. Checking the actual output is the reviewer's job.
2. Stalls must be visible. Tasks nobody claims, and claims that never produce
   a result, are named by `status` instead of waiting for someone to notice.

Root directory: $C2M_ROOT if set, otherwise the parent of this script's
directory (so <root>/bin/c2m.py works wherever <root> lives).

Usage:
    c2m.py send --title "check X" < body.md   # queue a task, prints its id
    c2m.py status                             # where each task is; exit 1 on any problem
    c2m.py show <id>                          # print task and result
    c2m.py reviewed <id>                      # archive after checking the result
"""
import argparse
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = os.environ.get("C2M_ROOT") or str(Path(__file__).resolve().parent.parent)
DIRS = ("inbox", "working", "outbox", "done", "reviewed")
STALE_UNCLAIMED_H = 3   # queued longer than this with no claim -> flagged
STALE_WORKING_H = 6     # claimed longer than this with no result -> flagged
STATUSES = {"done", "partial", "failed", "refused"}
REQUIRED_SECTIONS = ("## What I did", "## Evidence", "## Not done")


def p(*parts):
    return os.path.join(ROOT, *parts)


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_write(path, text):
    tmp = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.rename(tmp, path)


def slug(title):
    s = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower()[:30]
    return s or "task"


def cmd_send(a):
    body = sys.stdin.read().strip()
    if not body:
        sys.exit("empty task body (read from stdin)")
    for d in DIRS:
        os.makedirs(p(d), exist_ok=True)
    tid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + slug(a.title)
    for d in DIRS:  # the id must not collide with anything in any state
        if any(f.startswith(tid + ".") for f in os.listdir(p(d))):
            sys.exit(f"id collision: {tid}")
    text = (
        f"---\nid: {tid}\ntitle: {a.title}\ncreated: {now_iso()}\nfrom: claude-code\n---\n\n"
        f"{body}\n"
    )
    atomic_write(p("inbox", tid + ".task.md"), text)
    print(tid)


def front(text):
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def result_problems(text, tid):
    probs = []
    fm = front(text)
    if not fm:
        probs.append("no front matter")
    if fm.get("id") not in (None, tid):
        probs.append(f"id mismatch (says {fm.get('id')})")
    if fm and fm.get("status") not in STATUSES:
        probs.append(f"invalid status: {fm.get('status')!r}")
    for sec in REQUIRED_SECTIONS:
        if sec not in text:
            probs.append(f"missing section '{sec[3:]}'")
    ev = re.search(r"## Evidence\n(.*?)(\n## |\Z)", text, re.S)
    if ev and not ev.group(1).strip():
        probs.append("Evidence section is empty")
    return probs


def ids_in(d, suffix):
    if not os.path.isdir(p(d)):
        return []
    return sorted(f[: -len(suffix)] for f in os.listdir(p(d))
                  if f.endswith(suffix) and not f.startswith("."))


def age_h(path):
    return (time.time() - os.path.getmtime(path)) / 3600


def cmd_status(a):
    rows, alarms = [], 0
    for tid in ids_in("inbox", ".task.md"):
        h = age_h(p("inbox", tid + ".task.md"))
        flag = f"!! unclaimed for {h:.1f}h - is the agent's schedule running?" if h > STALE_UNCLAIMED_H else ""
        alarms += bool(flag)
        rows.append(("queued", tid, f"{h:.1f}h", flag))
    for tid in ids_in("working", ".task.md"):
        h = age_h(p("working", tid + ".task.md"))
        flag = f"!! claimed {h:.1f}h ago, no result yet" if h > STALE_WORKING_H else ""
        alarms += bool(flag)
        rows.append(("in progress", tid, f"{h:.1f}h", flag))
    known = set(ids_in("inbox", ".task.md")) | set(ids_in("working", ".task.md")) | set(ids_in("done", ".task.md"))
    for tid in ids_in("outbox", ".result.md"):
        if tid not in known:
            continue  # reported below as an orphan, not as "well-formed"
        text = open(p("outbox", tid + ".result.md"), encoding="utf-8").read()
        probs = result_problems(text, tid)
        st = front(text).get("status", "?")
        flag = ("!! " + "; ".join(probs)) if probs else "well-formed - output still needs checking"
        alarms += bool(probs)
        rows.append((f"to review({st})", tid, "", flag))
    results = set(ids_in("outbox", ".result.md")) | set(ids_in("reviewed", ".result.md"))
    for tid in sorted(set(ids_in("done", ".task.md")) - results):
        alarms += 1
        rows.append(("anomaly", tid, "", "!! task is in done/ but has no result file"))
    for tid in sorted(set(ids_in("outbox", ".result.md")) - known - set(ids_in("reviewed", ".task.md"))):
        alarms += 1
        rows.append(("anomaly", tid, "", "!! result with no matching task (not sent by us?)"))
    if not rows:
        print("empty: nothing in flight, nothing to review")
    for r in rows:
        print(f"{r[0]:<16} {r[1]:<40} {r[2]:>6}  {r[3]}")
    print(f"\nreviewed: {len(ids_in('reviewed', '.task.md'))}")
    return 1 if alarms else 0


def cmd_show(a):
    for d in DIRS:
        for suf in (".task.md", ".result.md"):
            f = p(d, a.id + suf)
            if os.path.exists(f):
                print(f"===== {d}/{a.id}{suf}")
                print(open(f, encoding="utf-8").read())


def cmd_reviewed(a):
    res, task = p("outbox", a.id + ".result.md"), p("done", a.id + ".task.md")
    if not (os.path.exists(res) and os.path.exists(task)):
        sys.exit("result not in outbox/ or task not in done/ - cannot archive")
    os.rename(res, p("reviewed", a.id + ".result.md"))
    os.rename(task, p("reviewed", a.id + ".task.md"))
    print("archived", a.id)


def main():
    ap = argparse.ArgumentParser(description="code-to-muse: hand tasks to a personal AI agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send"); s.add_argument("--title", required=True); s.set_defaults(f=cmd_send)
    sub.add_parser("status").set_defaults(f=cmd_status)
    s = sub.add_parser("show"); s.add_argument("id"); s.set_defaults(f=cmd_show)
    s = sub.add_parser("reviewed"); s.add_argument("id"); s.set_defaults(f=cmd_reviewed)
    a = ap.parse_args()
    sys.exit(a.f(a) or 0)


if __name__ == "__main__":
    main()
