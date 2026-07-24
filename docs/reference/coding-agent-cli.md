# Jack coding agent — CLI guide

`jack` is the terminal client for Jack's **coding agent**: a local agent that plans,
edits, and runs code in your project. It is a thin client of a **headless coder
daemon** — the CLI renders the TUI and streams events; the daemon owns the LLM, the
tools, and the permission gate. The same daemon backs the chat drawer, so behaviour is
identical across surfaces.

- Engine internals: [`architecture/design-reference.md`](architecture/design-reference.md)
- Config lives in `~/.autobot/settings.json`; **API keys never touch disk** — they live
  in the OS keyring (see [Providers & API keys](#providers--api-keys)).

## Quick start (cloud, Anthropic)

```bash
# 1. Install deps (the Anthropic SDK is a base dep)
uv sync

# 2. Configure the coder from the CLI (see "Managing config" below)
jack config set-key anthropic          # paste the key at the hidden prompt → OS keyring
jack config set provider anthropic
jack config set model claude-sonnet-5

# 3. Run jack inside the project you want to work on
cd ~/code/my-project
jack                      # opens the interactive TUI
jack -p "add a docstring to foo.py"   # or run one request and print the reply
jack help                 # list every command (also jack --help / -h)
```

A new key or setting takes effect on the **next turn** — no daemon restart. `jack`
auto-spawns the coder daemon on port **8766** the first time and reuses it afterward.

> The coder jails to the directory you launch it from (its cwd). Run `jack` from the
> repo you want it to edit.

## Managing config from the CLI

`jack config` reads and updates `~/.autobot/settings.json` with validation, and stores API
keys in the OS keyring. Changes persist even with no daemon running; if one is running, it
picks them up on the next turn.

```bash
jack config                       # show current settings (secrets shown as set/unset)
jack config get provider
jack config set provider anthropic
jack config set model claude-sonnet-5     # provider-aware: writes anthropic_model here
jack config set autonomy auto
jack config set-key anthropic             # hidden prompt → OS keyring (blank input clears)
jack config edit                          # open settings.json in $EDITOR
jack config path                          # print the settings.json path
```

**Aliases:** `provider`→`llm_provider`, `autonomy`→`coding_autonomy`, and `model`→
`anthropic_model` (when the provider is anthropic) or `llm_model`. Raw dataclass keys
(e.g. `coder_llm_max_tokens`) work too.

**Where it's written (precedence).** Config layers low→high: **defaults → global
(`~/.autobot/settings.json`) → workspace (`<workspace>/.jack/settings.json`)**. `jack config
set …` writes the **workspace** file by default (so a setting applies to just this project);
`jack config set --global …` writes the global file. `jack config` shows the merged effective
values and notes which keys the workspace overrides; `jack config path` prints both targets.
API keys are always global (keyring), never per-workspace.

**Validation:** unknown keys are rejected with the full valid-key list; values are
type-checked (bool/int/list) and enum-checked (`provider`, `autonomy`); token budgets must
be positive. `jack config set` refuses to run if `settings.json` is malformed (so it can't
silently clobber your file — fix it or use `jack config edit`), and `jack config edit`
won't apply changes if you save invalid JSON.

## Providers & API keys

Jack is provider-agnostic. Pick one with `jack config set provider …`; the key (if any) is
read from the keyring under a fixed account name. Only these secret names are accepted:
`anthropic_api_key`, `openai_api_key`, `web_api_key`.

| Provider | Set it with | Keyring account | Notes |
|---|---|---|---|
| **Anthropic** (cloud) | `jack config set provider anthropic` + `jack config set model claude-sonnet-5` | `anthropic_api_key` | Sends conversation + tool schemas/results to Anthropic; actions still run locally. |
| **OpenAI-compatible** | `jack config set provider openai` + `jack config set openai_base_url <url>` + `jack config set model <id>` | `openai_api_key` | Any `chat.completions` endpoint (OpenAI, Groq, Together, Gemini's compat URL, local vLLM/LM Studio). Blank `openai_base_url` = OpenAI default. Local servers ignore the key. |
| **Ollama** (local, default) | `jack config set provider ollama` + `jack config set model qwen3:8b` | — | Fully on-device; Ollama must be running. |

Set a key with `jack config set-key <provider>`, or directly via the keyring:

```bash
keyring set autobot anthropic_api_key      # cross-platform (needs the venv active)
# macOS one-liner:
security add-generic-password -U -s autobot -a anthropic_api_key -w 'sk-ant-…'
```

The GUI Settings view can also store keys (it posts to the daemon's `/secret`
endpoint) — same keyring, same effect.

## Running

- `jack` — open the interactive TUI (an inline REPL with streaming replies, live tool
  activity, plan/permission cards, and a working-tree diff after each change).
- `jack -p "<request>"` — one-shot: run a single coding turn, print the reply, exit.
  (A bare word that isn't a subcommand — e.g. `jack sessions` — is treated as a mistyped
  command and points you at `jack help`, never silently run as a request.)
- `jack help` — list every subcommand with a one-line summary (also `jack --help` / `-h`);
  run `jack <command> help` for a command's own options.
- `jack --port <N>` — target a daemon on a non-default port.
- `jack --workspace <path>` — work in `<path>` instead of the launch directory.
- `jack restart` — stop the coder daemon (use it after upgrading Jack, or to switch it to
  a different workspace). It stops the daemon by its recorded PID, falling back to whatever
  process is listening on the coder port, so a daemon left over from an older version stops
  too.

### Workspace

The coder operates in **the directory you launch `jack` in** (its workspace), and prints
`workspace: <path>` at startup so it's always clear where changes land. All file tools are
jailed to that directory. Pass `--workspace <path>` to point elsewhere.

### Workspace trust

The first time you use a folder, Jack asks **"Trust this folder? Jack can read, write, and
run commands in it."** — you only get read/write/run access after you say yes, and the
decision is remembered (in `~/.autobot/trust.json`). This mirrors VS Code / Claude Code
workspace trust. Non-interactively (piped/CI), an untrusted folder is refused with a hint to
run `jack trust`. Trust a folder up front (or in a script) with:

```bash
jack trust            # trust the current directory
jack trust <path>     # trust a specific directory
```

Each workspace gets its **own coder daemon** on its own port (chosen by hashing the
workspace path, tracked in `~/.autobot/daemons.json`), so you can work in several projects
at once without them colliding. A daemon **shuts itself down after ~20 minutes idle**
(`coder_idle_timeout_s`) so they don't pile up. Manage them with:

```bash
jack daemons          # list running coder daemons (workspace → port, up/dead)
jack stop             # stop the daemon for the current workspace
jack stop --all       # stop every coder daemon
jack restart          # stop this workspace's daemon (next run re-spawns it)
```

## Autonomy modes

The `coding_autonomy` setting (`jack config set autonomy <value>`, or `/autonomy` in-session)
is a progressive-trust dial. **All three** refuse blocklisted commands, stay within the cwd
jail, and take a git checkpoint at the start of every turn (so nothing is unrecoverable).

| Mode | Behaviour |
|---|---|
| `plan` (default) | Propose a numbered plan; act only after you approve it, then carry out the whole plan without re-prompting per step. |
| `confirm` | No plan phase; act directly, but ask before each command not on the allowlist. |
| `auto` | A lightweight router classifies each request and runs it as `plan` (multi-step/risky) or `confirm` (simple/low-risk); on any router failure it falls back to `plan`. |

## Slash commands

Type `/` in the TUI for completion. Client-side commands are instant; daemon-backed ones
(`/diff`, `/undo`, `/model`, `/autonomy`, `/sessions`, `/new`) query the daemon.

| Command | Does |
|---|---|
| `/help` | Show the command list |
| `/clear` | Clear the transcript (scrollback) |
| `/diff` | Show the working-tree diff (`git diff HEAD`) |
| `/undo` | Revert the last change; `/undo list` shows checkpoints |
| `/model` | Show or switch the model: `/model <name>` (applies next turn) |
| `/autonomy` | Show or set autonomy: `/autonomy plan\|confirm\|auto` |
| `/sessions` | List sessions; `/sessions resume <id>` to continue one |
| `/new` | Start a fresh session |
| `/exit` | Quit jack |

## Safety model

- **Permission gate.** Every acting tool (write/delete/run/network) is risk-classified and
  gated; destructive actions are confirmed and every decision is written to a SQLite audit
  log. The LLM never executes side effects directly.
- **cwd jail.** File tools are path-jailed to the launch directory; the agent cannot read
  or write outside it.
- **Checkpoints & undo.** With `checkpoints: true` (default), each turn snapshots the
  workspace to a private git ref (`refs/jack/checkpoints/<n>`); `/undo` rewinds to it.
- **Command policy.** `run_command` is layered on the gate: a built-in dangerous-pattern
  baseline plus your `command_allowlist` / `command_blocklist`. Blocklisted commands are
  refused outright; allowlisted patterns (e.g. `"git *"`, `"pytest*"`) run with less
  friction.
- **Secret redaction & egress disclosure.** Secrets are redacted from transcripts and
  logs. Cloud mode prints an explicit disclosure banner: your requests + remembered
  profile go to the provider, but audio never does and actions always run locally.

## Sessions

Sessions are **per-workspace**: the coder stores JSONL transcripts under
`<workspace>/.jack/sessions/`, so `/sessions`, resume, and history are scoped to the project
you're in (git-ignored by the seeded `.jack/.gitignore`). Use `/sessions` to list,
`/sessions resume <id>` to continue, and `/new` to start fresh. Checkpoints live in the
workspace's own git repo, so they're isolated per project too.

## Skills

Jack uses **Agent Skills** — reusable `SKILL.md` playbooks that load on demand. Drop a
skill in a folder and Jack discovers it; when a task matches a skill's description, the
model loads its instructions (and any bundled reference files, via `read_skill_file`) and
follows them. It's the standard `SKILL.md` format, so skills authored for other agents work
in Jack and vice versa.

**Where skills live** (project overrides user; canonical over `.claude` compat):

- `<workspace>/.jack/skills/` — project-level (check into the repo to share)
- `~/.autobot/skills/` — user-level (also where fetched skills install)
- `<workspace>/.claude/skills/`, `~/.claude/skills/` — read-only compatibility, so skills
  you already have for other tools just work

**Manage skills from the terminal** (`jack skills …`, in-process — no daemon):

```bash
jack skills list                  # installed skills
jack skills show <name>           # print a skill's instructions
jack skills search <query>        # search the configured registries
jack skills add <name>            # install from a registry into ~/.autobot/skills
jack skills remove <name>         # delete an installed skill
```

**Fetching from registries (opt-in, disclosed egress).** `skill_registries` in
`settings.json` is a whitelist of trusted **git repos** Jack may fetch skills from (empty by
default). A fetch happens *only* on an explicit `jack skills search|add`, or when the model
calls the gated `find_skill` / `install_skill` tools — both marked off-device (the "↗ sends
data off-device" badge + audit egress note). Jack shallow-clones a whitelisted repo,
installs the matched skill's folder, and pins the source commit; a repo not on the whitelist
is refused, and nothing else leaves the machine.

**Author your own.** Ask Jack to "make a skill for X" and it writes a `SKILL.md` into
`~/.autobot/skills/`; it's discovered on the next turn. Turn the whole feature off with
`skills_enabled: false`.

## Workflows

Where a skill is *model-driven* guidance, a **workflow** is a *deterministic recipe*: an
ordered list of tool-steps that run the same way every time. A `WORKFLOW.md` has frontmatter
(`name`, `description`, `inputs`) and a fenced YAML `steps:` block:

````markdown
---
name: release-notes
description: Draft release notes from commits since a tag. Use when preparing a changelog.
inputs:
  - name: since_tag
    required: true
---

```yaml
steps:
  - tool: run_command
    args: { command: "git log {since_tag}..HEAD --oneline" }
    save_as: commits
  - tool: write_file
    args: { path: "NOTES.md", content: "{commits}" }
    when: "{commits}"
```
````

The model runs one with `run_workflow("release-notes", {"since_tag": "v1.2.0"})`. The runner
substitutes `{var}` from the inputs, threads a step's output forward via `save_as`, and skips
a step whose `when:` value is empty/`false`/`0`. **Every step executes through the same
permission gate** as a model-issued call — a workflow can do nothing you couldn't do by hand,
just deterministically. A failing step halts the workflow.

- Workflows live in `<workspace>/.jack/workflows/` (project) and `~/.autobot/workflows/` (user).
- `jack workflows list | show <name> | run <name>` from the terminal (`run` is executed from a
  Jack coding turn in v1).
- Turn the feature off with `workflows_enabled: false`.

## Configuration reference (coder-relevant)

Set these with `jack config set <key> <value>` (defaults come from `config.py`):

| Key | Default | Purpose |
|---|---|---|
| `llm_provider` | `"ollama"` | `ollama` \| `anthropic` \| `openai` |
| `anthropic_model` | (default cloud model) | Model id when provider is `anthropic` |
| `llm_model` | (default local model) | Model id for `ollama` / `openai` |
| `openai_base_url` | `""` | Endpoint for the OpenAI-compatible provider |
| `coding_autonomy` | `"plan"` | `plan` \| `confirm` \| `auto` |
| `checkpoints` | `true` | Per-turn git snapshot for `/undo` |
| `command_allowlist` | `[]` | Patterns trusted to run with less friction |
| `command_blocklist` | `[]` | Patterns refused outright |
| `coder_llm_max_tokens` | `4096` | Output-token budget for coder turns |
| `agent_session_dir` | `~/.autobot/agent_sessions` | Where session transcripts live |

## Files & logs

- Settings: `~/.autobot/settings.json`
- API keys: OS keyring, service `autobot` (never on disk)
- Debug log: `~/.autobot/logs/autobot.log` — filter the coder with
  `grep '\[coder\]' ~/.autobot/logs/autobot.log`
- Sessions: `~/.autobot/agent_sessions/`
- Checkpoints: git refs `refs/jack/checkpoints/<n>` in the working repo

## For contributors: E2E verification

A dev-only PTY harness drives the real `jack` TUI through real LLM turns and scores each
run (deterministic checks + an LLM judge):

```bash
uv sync --extra e2e
python -m autobot.e2e                       # run the whole scenario corpus
python -m autobot.e2e create-file --judge auto   # one scenario, auto-judge
```

Artifacts (report, screen, raw transcript, steps, judge verdict) are written under
`~/.autobot/e2e/`. Add one scenario per bug report in
`src/autobot/e2e/scenarios/__init__.py`.
