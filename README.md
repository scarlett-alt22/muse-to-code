# muse-to-code

A tiny file-drop bridge between a personal AI agent (e.g. Meta's Muse) and a
local [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI.

The agent writes a task file into `inbox/`; the bridge runs it through
`claude -p` (non-interactive print mode: one response, then exit) and writes
the result back to `outbox/` for the agent to pick up. No servers, no ports,
no dependencies beyond Python 3's standard library.

```
agent  -- <id>.task.md -->  inbox/  -->  bridge.py  -->  claude -p
                                             |
                                             v
agent  <-- <id>.result.md <-- outbox/ <-- done/<id>.task.md
```

## Threat model

Whoever can write to `inbox/` can drive Claude Code **with your identity and
your permissions**. The bridge forces `--permission-mode default` on the
command line (so it beats whatever `defaultMode` is in your settings.json;
newer `claude --help` lists this mode as `manual`, and both values work),
but that flag only controls how operations *outside* your rules are handled:
the allow-rules in your own `permissions.allow` still auto-approve in `-p`
mode, and MCP servers in your configs (including a project's `.mcp.json`)
are connected without a trust or per-server approval prompt, so any MCP
tool your allow-rules cover runs too. Anything that would need a prompt is
denied. In other words, the effective permission is "your full whitelist",
not "default mode". The bridge also
sandboxes the working directory (tasks run in `workspace/` unless you
whitelist more via `MUSE_TO_CODE_ALLOWED_ROOTS`), but it is not a sandbox
for untrusted task authors. Only let agents you trust write to `inbox/`.

Want real isolation instead? Edit the `claude` argv in `bridge.py` to add
`--restricted --strict-mcp-config` (see "Ideas not built"): `--restricted`
ignores the user/project/local settings files (managed settings and
`--settings` still apply) and drops code-execution tools like Bash and
WebFetch (unless re-added via `--tools`), confining file access to the
working directory; add `--strict-mcp-config` to also skip MCP servers.
The price: Claude can no longer run tests or builds.

## Install (macOS)

0. Create the working directory first (launchd will not create it for you):

   ```sh
   mkdir -p /Users/YOUR_NAME/muse-to-code/work
   ```

1. Copy the launchd plist and edit it. There are **4 places** to change, all
   must be absolute paths (launchd does not expand `~` or `$HOME`):

   - `ProgramArguments` → path to `bridge.py`
   - `MUSE_TO_CODE_DIR` → your working directory
   - `StandardOutPath` / `StandardErrorPath` → log files inside it

   ```sh
   cp muse-to-code.plist ~/Library/LaunchAgents/
   # then edit: replace every /Users/YOUR_NAME/... with your real paths
   ```

2. Load it once (it restarts on login afterwards; or just log out and back in):

   ```sh
   launchctl load ~/Library/LaunchAgents/muse-to-code.plist
   ```

3. Verify: drop any `hello.task.md` into the inbox dir (default
   `~/.muse-to-code/inbox/`). It will be picked up within ~15 seconds
   (plus however long `claude` takes); `outbox/hello.result.md` appears,
   with an entry in `bridge.log`.

To use a different working directory, set `MUSE_TO_CODE_DIR` (supports `~`).
To let tasks run outside the default `workspace/` dir, whitelist roots:

```sh
export MUSE_TO_CODE_ALLOWED_ROOTS="$HOME/projects:$HOME/notes"
```

## Task file protocol

- **Write atomically.** The bridge polls every 15 seconds and may pick up a
  half-written file. Always write to `<id>.task.md.tmp` first, then `rename`
  it to `<id>.task.md`. (The bridge only matches `*.task.md`, so `.tmp`
  files are never picked up early.)
- Filename: `<id>.task.md` — id allows letters, digits, `-`, `_` (max 64
  chars). Anything else is moved to `rejected/` (no path traversal, ever).
- Body: the prompt, Markdown, max 32 KB. Oversize files go to `rejected/`.
- Optional first-line directive: `<!-- cwd: /absolute/path -->` — sets the
  working directory `claude` runs in. Accepted only if the directory exists
  **and** sits under `MUSE_TO_CODE_ALLOWED_ROOTS` (default: the bridge's own
  `workspace/`). The `cwd` path may not contain spaces.
- After processing, the task file is moved to `done/`; the result lands at
  `outbox/<id>.result.md` with a small header
  (`started` / `finished` / `exit` / `timed_out` / `error`). Re-running the
  same id overwrites the previous result.
- If a result never arrives, look in `processing/`: tasks are claimed there
  before execution and never re-run, so a failed result-write leaves the
  task waiting there instead of retrying.

## Safety rails (baked into `bridge.py` — think twice before changing)

- `claude -p` always runs with `--permission-mode default`, hardcoded in
  argv (a command-line flag beats whatever is in `settings.json`). Note:
  this only governs operations *outside* your rules — your own
  `permissions.allow` entries still auto-approve, including any MCP tools
  they cover (see Threat model above). The bridge **never** passes
  `--dangerously-skip-permissions` or any permission-bypass flag.
- Prompts go via stdin with `shell=False`: no shell interpolation, ever.
- Symlinks in `inbox/` are rejected, not followed.
- Tasks are claimed into `processing/` before execution, so a crash or a
  failed result-write can never re-run a task (and its side effects).
- One task at a time, 10-minute timeout per task, every invocation logged.

## Reverse direction: code-to-muse

[`code-to-muse/`](code-to-muse/) is the other half: Claude Code hands tasks
**to** the agent. There is no daemon on this side. Claude Code queues a task
file; the agent, on its own schedule, claims it, does it, and writes a
result back.

```
claude code -- c2m.py send -->  inbox/  -- rename -->  working/  (agent claims)
claude code <-- c2m.py status -- outbox/<id>.result.md + done/<id>.task.md
```

- **`code-to-muse/bin/c2m.py`** (stdlib only) — `send --title T < body.md`
  queues a task; `status` shows where every task is and exits 1 if anything
  is stuck (unclaimed > 3 h, claimed > 6 h with no result) or malformed
  (bad front matter, wrong id, invalid status, missing or empty `Evidence`,
  a task in `done/` with no result, a result with no task); `show <id>`;
  `reviewed <id>` archives after checking. The root folder is the parent of
  `bin/` unless `C2M_ROOT` is set.
- **`code-to-muse/AGENT.md`** — instructions to give the agent: the claim /
  write / move protocol, the result format, and the rules. Fill in its
  "Your red lines" section before handing it over.

**`status` checks form, not truth.** A well-formed result is still only the
agent's claim; the reviewer has to check the actual output. That is why the
result format requires verbatim evidence.

Threat model for this direction:

- **Task text leaves your machine.** A hosted agent (Muse runs in the vendor's
  cloud and reaches the Mac through its device bridge) reads the task and any
  files it opens on the provider's servers. Put only what the task needs in it.
- **The agent's reach is whatever you granted it**, which can be much wider
  than this folder (mail, messages, screen, input control). The rules in
  `AGENT.md` are instructions, not enforcement.
- Anything that can write to `inbox/` can queue work for the agent. Tasks
  carry `from: claude-code` and the agent is told to refuse anything else,
  but that is a convention, not authentication.

## Uninstall

```sh
launchctl unload ~/Library/LaunchAgents/muse-to-code.plist
rm ~/Library/LaunchAgents/muse-to-code.plist
```

## Ideas not built (yet)

- `--restricted --strict-mcp-config` mode: ignore the user/project/local
  settings layers, drop code-execution tools, confine file access to the
  working directory, and hold back MCP servers. Stronger isolation, but
  Claude can no longer run tests or builds.
- Task dedup / priorities; `--output-format json` for structured results.

## License

MIT — see [LICENSE](LICENSE).
