#!/usr/bin/env python3
"""muse-to-code bridge — file-drop relay between a personal AI agent and local Claude Code.

The agent drops task files into INBOX; this bridge runs each through
`claude -p` (print mode: non-interactive, one response, then exit)
and writes the result to OUTBOX for the agent to pick up.

Poll-based, stdlib only. Intended to run under launchd on macOS — see
muse-to-code.plist.

Threat model (read before installing): whoever can write to INBOX can
drive Claude Code with YOUR identity and YOUR permissions. The bridge
forces `--permission-mode default` on the command line (so it beats
whatever `defaultMode` is in your settings.json), but that flag only
controls how operations OUTSIDE your rules are handled: the allow-rules
in your own `permissions.allow` and any MCP servers you configured still
auto-approve in `-p` mode. In other words, the effective permission is
"your full whitelist", not "default mode". The bridge also sandboxes the
working directory (see WORKSPACE / MUSE_TO_CODE_ALLOWED_ROOTS), but it is
not a sandbox for untrusted task authors. Only let agents you trust write
to INBOX. If you want real isolation, see the `--restricted` note in the
README.

Safety properties (do not weaken without thinking):
- `claude -p` always runs with `--permission-mode default`, hardcoded in
  argv (command-line flags beat settings.json). The bridge NEVER passes
  --dangerously-skip-permissions or any permission-bypass flag.
- Prompts go via stdin with shell=False: no shell interpolation, ever.
- Task filenames are strictly validated (no path traversal).
- The `cwd:` directive only accepts directories under
  MUSE_TO_CODE_ALLOWED_ROOTS (default: the bridge's own workspace dir).
- Tasks are claimed into `processing/` BEFORE claude runs, so a crash or
  a failed result-write can never re-execute a task (and its side effects).
- Single-task size cap, per-task timeout, every invocation is logged.
"""

import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()


def _abs(p):
    p = Path(os.path.expanduser(p))
    return p if p.is_absolute() else HOME / p


BASE = _abs(os.environ.get("MUSE_TO_CODE_DIR", "~/.muse-to-code"))
INBOX = BASE / "inbox"
OUTBOX = BASE / "outbox"
DONE = BASE / "done"
REJECTED = BASE / "rejected"
PROCESSING = BASE / "processing"
WORKSPACE = BASE / "workspace"
LOG = BASE / "bridge.log"

_raw_roots = os.environ.get("MUSE_TO_CODE_ALLOWED_ROOTS", "")
ALLOWED_ROOTS = [_abs(p).resolve() for p in _raw_roots.split(os.pathsep)
                 if p.strip()]
if not ALLOWED_ROOTS:
    ALLOWED_ROOTS = [WORKSPACE.resolve()]

POLL_SECONDS = 15
MAX_TASK_BYTES = 32 * 1024   # 32 KB prompt cap
TASK_TIMEOUT = 600           # 10 minutes per task
TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.task\.md$")
CWD_RE = re.compile(r"\s*<!--\s*cwd:\s*(\S+)\s*-->\s*")


def log(msg):
    line = f"{datetime.now(timezone.utc).isoformat()} {msg}\n"
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def find_claude():
    exe = shutil.which("claude")
    if exe:
        return exe
    for p in (HOME / ".local" / "bin" / "claude",
              Path("/opt/homebrew/bin/claude"),
              Path("/usr/local/bin/claude")):
        if p.exists():
            return str(p)
    return None


def _under_roots(cand):
    try:
        resolved = cand.resolve()
    except OSError:
        return False
    return any(resolved == r or r in resolved.parents
               for r in ALLOWED_ROOTS)


def parse_task(text):
    """Optional first-line directive: <!-- cwd: /abs/path -->
    Sets the working directory claude runs in. Accepted only when the
    directory exists and sits under MUSE_TO_CODE_ALLOWED_ROOTS.
    Relative paths resolve against WORKSPACE."""
    cwd = str(WORKSPACE)
    head, _, rest = text.partition("\n")
    m = CWD_RE.fullmatch(head)
    if m:
        raw = m.group(1)
        cand = Path(os.path.expanduser(raw))
        if not cand.is_absolute():
            cand = WORKSPACE / cand
        if cand.is_dir() and _under_roots(cand):
            return str(cand.resolve()), rest
        log(f"cwd directive rejected (outside allowed roots): {raw}")
    return cwd, text


_seen_reject_failures = set()


def _reject(task_path, reason):
    dest = REJECTED / task_path.name
    if dest.exists():
        stem = task_path.name[: -len(".task.md")]
        dest = REJECTED / f"{stem}-{int(time.time())}.task.md"
    try:
        task_path.rename(dest)
    except OSError:
        # If we can't move it, don't spam the log every poll round.
        if task_path.name not in _seen_reject_failures:
            _seen_reject_failures.add(task_path.name)
            log(f"rejected {task_path.name}: {reason} (move failed, will retry quietly)")
        return
    log(f"rejected {task_path.name}: {reason}")


def run_task(task_path, task_id=None):
    """Execute one task with `claude -p` and write the result to outbox.
    task_id is the ORIGINAL inbox filename (before _claim), so the result
    filename stays predictable for whoever submitted the task."""
    task_id = task_id or task_path.name[: -len(".task.md")]
    started = datetime.now(timezone.utc)
    out = {"id": task_id, "started": started.isoformat(),
           "finished": None, "exit": None,
           "timed_out": False, "error": None, "output": ""}
    try:
        prompt = task_path.read_text(encoding="utf-8")
        cwd, prompt = parse_task(prompt)
        claude = find_claude()
        if not claude:
            out["error"] = "claude CLI not found in PATH"
        else:
            proc = subprocess.run(
                [claude, "-p", "--permission-mode", "default"],
                input=prompt, capture_output=True, text=True,
                timeout=TASK_TIMEOUT, cwd=cwd,
            )
            out["exit"] = proc.returncode
            out["output"] = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        out["timed_out"] = True
        out["error"] = f"timed out after {TASK_TIMEOUT}s"
    except Exception as e:  # noqa: BLE001 - log and continue the loop
        out["error"] = f"{type(e).__name__}: {e}"
    out["finished"] = datetime.now(timezone.utc).isoformat()

    body = (
        f"# result \u00b7 {task_id}\n"
        f"- started: {out['started']}\n"
        f"- finished: {out['finished']}\n"
        f"- exit: {out['exit']}\n"
        f"- timed_out: {out['timed_out']}\n"
        + (f"- error: {out['error']}\n" if out["error"] else "")
        + "\n---\n\n" + out["output"]
    )
    (OUTBOX / f"{task_id}.result.md").write_text(body, encoding="utf-8")
    task_path.rename(DONE / task_path.name)
    log(f"task={task_id} exit={out['exit']} "
        f"timed_out={out['timed_out']} error={out['error']}")


def _claim(task_path):
    """Move a task into processing/ BEFORE running it, so a crash or a
    failed result-write can never cause a re-execution (and repeated
    side effects)."""
    dest = PROCESSING / task_path.name
    if dest.exists():
        stem = task_path.name[: -len(".task.md")]
        dest = PROCESSING / f"{stem}-{int(time.time())}.task.md"
    task_path.rename(dest)
    return dest


def main():
    for d in (INBOX, OUTBOX, DONE, REJECTED, PROCESSING, WORKSPACE):
        d.mkdir(parents=True, exist_ok=True)
    log("bridge started")
    while True:
        try:
            for p in sorted(INBOX.glob("*.task.md")):
                if p.is_symlink():
                    _reject(p, "symlink")
                    continue
                if not TASK_RE.match(p.name):
                    _reject(p, "bad filename")
                    continue
                if p.stat().st_size > MAX_TASK_BYTES:
                    _reject(p, "oversize")
                    continue
                try:
                    claimed = _claim(p)
                except OSError as e:
                    log(f"claim failed for {p.name}: {e}")
                    continue
                log(f"pickup task: {p.name}")
                run_task(claimed, task_id=p.name[: -len(".task.md")])
        except Exception as e:  # noqa: BLE001 - never kill the loop
            log(f"loop error: {type(e).__name__}: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
