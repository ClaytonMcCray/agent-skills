---
name: claude-review
description: >-
  Obtain independent adversarial code reviews or planning critiques from Claude
  Code through a thin GPT-6 Sol relay using the user's Claude subscription. Use
  when a Codex-led task benefits from a cross-model second opinion or the user
  asks for Claude, Fable, or Opus review.
---

# Claude Review

Codex leads the work. A Sol subagent forwards a focused request to Claude Code,
returns Claude's compact findings unchanged, and links its complete report. The
lead evaluates the findings and decides what to change. Use this for substantive
independent critique, not every routine edit.

Resolve `<SKILL_DIR>` dynamically from the directory containing this loaded
`SKILL.md` so helper and reference paths stay portable across machines
([claude_review.py](./scripts/claude_review.py),
[claude-review.toml](./references/claude-review.toml)).

## Prepare an independent request

Give the relay the absolute repository/workspace path, a self-contained task,
relevant files or a diff, intended behavior, constraints, and known test results.
For a planning critique, include the proposal and the decision to challenge.
Prefer requirements and evidence over the lead's conclusions; label hypotheses
as hypotheses. Each distinct review starts fresh and does not inherit the Codex
conversation, `CLAUDE.md`, memory, hooks, plugins, or MCP connections. Supply
relevant project conventions explicitly. Never include credentials in the review
packet.

Continue discussion of the same review with its returned `session_id` or
`session_file`. Send only new evidence, changed file paths/diff, the revision,
and the question or finding IDs being challenged. Claude retains its own review
conversation; the lead and a fresh Sol relay need not reload that transcript.
Use a new review for an unrelated task or when a fresh independent assessment is
more valuable than continuity. Reusing a session does not make Claude's own
context or usage free.

For changing swarm workspaces, provide a stable snapshot or captured diff and
identify the reviewed revision. Claude has Read/Glob/Grep, with reads confined to
its workspace by restricted mode. It has no shell, write, or test-execution tools;
include command outputs and additional context files inside the review workspace
when needed. A packet-only planning request can use a temporary workspace.

## Choose the reviewer

- New-review default: `fable`, effort `high`. Resumed reviews inherit their last
  model, effort, and review/plan mode unless the lead explicitly overrides them.
- The lead can choose `opus` or an exact Claude model ID per task. Choose the
  model deliberately; do not silently replace an unavailable model.
- Prefer `high`; choose `xhigh` or `max` for difficult architecture, subtle
  failure analysis, or high-impact changes when supported. Lower effort is
  available for small checks. `haiku` / `low` is useful for plumbing tests.
- The Sol relay uses `gpt-6-sol` / `low`; its job is transport, not the review.

## Delegate

Use the `claude-review` custom agent when the client's spawn tool exposes custom
agent selection (reference definition in
[claude-review.toml](./references/claude-review.toml), optionally installed at
`~/.codex/agents/claude-review.toml`). Pass the request, cwd, Claude model,
effort, and mode (`review` or `plan`). For follow-ups, also pass `session_file`
or `session_id`. Keep its context narrow rather than forking the entire
conversation.

If custom-agent selection is unavailable, spawn `gpt-6-sol` with low reasoning
and no history fork (`fork_context = false`); include the
`developer_instructions` from
[claude-review.toml](./references/claude-review.toml) and the review request in
its task, passing `<SKILL_DIR>` so the relay invokes the exact helper path. This
is an explicit delegation workflow; Sol must invoke Claude rather than answer
itself.

The relay writes the request to a unique temporary UTF-8 file and runs:

```sh
python3 "<SKILL_DIR>/scripts/claude_review.py" \
  --cwd /absolute/project --model fable --effort high --mode review \
  --prompt-file /absolute/request.txt
```

Resume using the returned metadata path (this also finds a custom output root):

```sh
python3 "<SKILL_DIR>/scripts/claude_review.py" \
  --resume /absolute/path/to/session.json \
  --revision revised-sha --prompt-file /absolute/follow-up.txt
```

Alternatively use `--resume <session_id>` with the original `--output-root`, if
one was specified. Do not use Claude's `--continue`, which could pick another
swarm task. The wrapper requires its own session metadata, keeps the same cwd,
checks Claude's returned session ID, and rejects concurrent turns on one review.
Supply `--revision` when reviewing a named commit, snapshot, or revised diff;
revision labels are supplied by the lead, not inferred from the working tree.
Ordinary later file changes are allowed in the same workspace; tell Claude what
changed so it re-reads the relevant files.

The helper uses existing Claude subscription OAuth and refuses API-key auth. It
strips ambient Anthropic API/provider overrides from the child environment and
does not alter the user's login or settings. It never uses `--bare` or permission
bypass. Requests still follow Codex's normal sandbox/approval rules. If network
access is known to require escalation, request it for this specific invocation;
otherwise try normally and escalate on an actual access failure. Do not change
sandbox settings in an attempt to suppress approvals.

Poll running execution at intervals of at most 60 seconds and report progress.
High-effort reviews can take minutes; a silent CLI alone is not a failure. The
helper emits a heartbeat every 30 seconds, times out after 900 seconds by default
(`--timeout` is configurable), and terminates its child on cancellation. Confirm
an old attempt stopped before retrying. No automatic retry or model fallback.
On Linux, Claude also receives `SIGTERM` if the launcher is killed. Each
process's PID/PGID is recorded in the artifact directory for diagnosis; verify
live process identity before using old PID records, since operating systems
reuse PIDs. Timeouts, cancellation, hard kills, and failed Claude calls can
leave unfinished native history. The wrapper marks those sessions non-resumable;
start a fresh review from the saved evidence rather than silently continuing an
uncertain turn. A missing/deleted native transcript is an error, never a
fallback to a fresh conversation. Earlier one-shot runs made with no persistence
cannot be resumed.

## Return and assess

The helper asks Claude for structured output: `summary`, finding cards,
`limitations`, and `full_report`. Claude writes both the concise and detailed
versions; Sol does not summarize or reinterpret them.

Stdout is compact JSON with `status`, `session_id`, `session_file`, `resumable`,
requested/actual models, the summary, finding cards, limitations, and report
paths. Finding cards contain stable IDs, severity, open/resolved/disputed
status, location, the concrete issue, and a validation step. The default preview
includes five cards; `findings_total` and `findings_omitted` expose any
additional findings. Use `--max-findings` to change the preview or read the
referenced `review.json`. Never treat a truncated preview as the whole review.

Argument/input errors before a run is created use stderr and a nonzero exit code.
The wrapper stores each review under
`~/.local/state/claude-review/sessions/<id>/`, with `session.json` and a
separate `turn-*` directory for every attempt. Each turn preserves the request,
raw Claude result and usage in `claude.json`, stderr, structured `review.json`,
the exact `full_report` in `report.md`, and compact `result.json`. Earlier
reports are never overwritten. Claude also stores its native conversation under
its usual config directory, normally `~/.claude`; resumption requires that
history. These files may contain source code. `--output-root` changes the
wrapper artifacts, not Claude's native transcript location. CLI cost estimates
can include previous turns in the conversation and do not establish additional
subscription charges. Reported model IDs come from CLI usage metadata and can
include models used on earlier turns; the current requested model is recorded
separately in `request.model`.

The relay returns the compact JSON or its finding fields verbatim, including
session identity, omitted-count, and report paths. Do not read or paste the full
report into the lead's context unless requested or needed to assess a specific
finding. Do not improve, soften, or invent findings. Report nonzero exit status,
`incomplete`, denied-tool count, timeout, and missing evidence.

The lead checks the findings against the code and responds to disagreements on
their merits. An independent model offers different failure modes, not guaranteed
correctness. Challenge a disputed finding in the same session with focused new
evidence. Keep Claude's `R1`/`R2` identifiers when discussing fixes; Claude
should mark earlier findings resolved or disputed rather than renumbering them.
