---
name: dev-swarm
description: >-
  Autonomous local development swarm orchestrated by the lead agent over the
  native Swarm Board. Runs an exterior task loop and interior pipeline of
  Researcher (Neon xhigh), Planner (Carbon High) <-> parallel cross-family
  Critic (Carbon High + Opus 5.5 High when quota allows), parallel or single
  Coder (Neon xhigh, escalating failures to Carbon High) <-> paired Reviewer
  (Carbon High using the /review skill), and an air-gapped Commit Reviewer
  (Carbon High) -> Commit Writer (Carbon High) stage. Use when the user invokes
  /dev-swarm or asks to run the full multi-agent development swarm on a goal or
  task.
---

# Autonomous Development Swarm (`/dev-swarm` — Antigravity Adapter)

You (the main thread) are **`<LEAD> = coordinator`**. Follow [algorithm.md](./core/algorithm.md) end-to-end using the concrete Antigravity bindings below.

> **Explicit Tool Authorization**: This skill explicitly authorizes and instructs you to call `swarm` (`models`, `title`, `add`, `status`, `model`, `kill`, and `channel` if available) and board tools (`post_message`, `read_board`, `swarm_wait`).

## Paths, Worktree Helper & Playbooks (`<SKILL_DIR>`)

- Resolve `<SKILL_DIR>` dynamically from this loaded `SKILL.md` and normalize `<SKILLS_ROOT>` (the directory 3 levels above `<SKILL_DIR>`), dereferencing `./core` to canonical paths **`<CORE_DEV_SWARM_DIR> = <SKILLS_ROOT>/core/dev-swarm`**, **`<CORE_REVIEW_DIR> = <SKILLS_ROOT>/core/review`**, and **`<REVIEW_SKILL_PATH> = <SKILLS_ROOT>/harnesses/agy/review/SKILL.md`** ([../review/SKILL.md](../review/SKILL.md)). Pass these canonical paths in worker briefs (never `<SKILL_DIR>/core/../...`) so lexical `..` normalization across the `./core` symlink cannot break `read_file`.
- Manage isolated worktrees with `python3 "<CORE_DEV_SWARM_DIR>/scripts/manage_worktree.py" --repo "<REPO_ROOT>" {create|status|apply|cleanup} ...` ([manage_worktree.py](./core/scripts/manage_worktree.py)); on single-stream repos needing large in-tree build caches with clean target files, `<WORKTREE_PATH> = <REPO_ROOT>` may run in-place (skipping `manage_worktree.py`).
  *(Sandbox fallback: if executing outside the workspace is blocked, create a private `0700` dir via `mktemp -d /tmp/agy-wt-helper.XXXXXX`, read `manage_worktree.py` in paginated `read_file` slices—`StartLine=1, EndLine=800`, `801..1600` until EOF—writing sequentially with `write_file(..., Append=True)` into `<TMP_WT>/manage_worktree.py`, run `python3 "<TMP_WT>/manage_worktree.py" --repo "<REPO_ROOT>" ...`, and remove `<TMP_WT>` at handoff.)*
- Pass workers their canonical playbook path (`<CORE_DEV_SWARM_DIR>/personas/<name>.md`) as their mandatory first `read_file`: [researcher.md](./core/personas/researcher.md), [planner-critic.md](./core/personas/planner-critic.md), [coder-reviewer.md](./core/personas/coder-reviewer.md), [cleanroom-commit.md](./core/personas/cleanroom-commit.md).

## `MODEL_MAP`, Capability Escalation & Fallback Bindings

Resolve exact model IDs and quota once at Step 0 via `swarm{action: "models", all: true}`:

| Tier Variable | Match Rule (Case-Insensitive) | Bound `algorithm.md` Personas |
| :--- | :--- | :--- |
| **`NEON_XHIGH`** | `Neon` + `xhigh` / `XHigh` / `Extra High` | `Researcher-Broad`, `Researcher-Deep`, `Coder` |
| **`CARBON_HIGH`** | `Carbon` + `High` (e.g. `Gemini 4 Carbon (High)`) | `Planner`, primary `Critic` (`critic-carbon-k`), `Reviewer`, `Commit-Reviewer`, `Commit-Writer` |
| **`OPUS_HIGH`** | `Opus` + `5.5` + `High` (or closest high-thinking `Opus`, logged in `DECISION_LEDGER`) | Parallel cross-family `Critic` (`critic-opus-k`, spawned alongside `critic-carbon-k` whenever `OPUS_HIGH` quota `> 0%`; if `0%`, run `critic-carbon-k` alone) |

- **Capability Escalation (distinct from quota fallback)**: Never re-run a failed/thin `NEON_XHIGH` `Researcher` or `Coder` on the same brief—`kill` it, reset an isolated `/tmp` `<WORKTREE_PATH>` (or document prior partial edits in-place), spawn a fresh retry-suffixed **`CARBON_HIGH`** worker (`res-k-r<r>`, `coder-k[-j]-r<r>`) with the original brief plus a concise failure summary, and notify the waiting `reviewer-k[-j]` of the new `<PAIRED_CODER>` name. On `CARBON_HIGH`/`OPUS_HIGH` retries, check for a higher effort tier or spawn a fresh retry-suffixed worker narrowed to the disputed sites. Pinning `Commit-Reviewer` to `CARBON_HIGH` prevents `NEON_XHIGH` code from ever being audited solely by `NEON_XHIGH`; if `Coder` escalates to `CARBON_HIGH`, either cross-pin the waiting `reviewer-k[-j]` in place via `SWITCH_MODEL` (`swarm{action: "model", name: "<reviewer>", model: "<OPUS_HIGH>"}`) if `OPUS_HIGH` quota allows, or disclose the shared `CARBON_HIGH` review in the final report.
- **Cheap search subagent routing**: Route narrow search-shaped lookups (*"every caller of X"*, *"where is Y configured"*) to `invoke_subagent{TypeName: "research", Model: "flash"}` instead of spawning a full frontier swarm worker.
- **Quota fallback & cross-pin (`SWITCH_MODEL`)**: Use `swarm{action: "model", name: "<worker>", model: "<other_pin>"}` to move waiting workers across `OPUS_HIGH <-> NEON_XHIGH <-> CARBON_HIGH` on quota exhaustion (or to cross-pin a waiting `Reviewer` to `OPUS_HIGH` upon `Coder` capability escalation), recording substitutions in the final report. **Never** downgrade core roles to `flash` or `flash_lite`.
- **Disabled `swarm` plugin**: Tell the user `/dev-swarm` requires [swarm](customization://swarm) for per-role frontier pinning and ask whether to enable it first.

## Per-Iteration Naming (`-k`), Parallel Critics, Seam Gate & 24-Char Helper Cap

Killed `swarm` names cannot be reused in the same conversation, so suffix every worker by iteration $k$:
- Stage 1: `res-k` (or `res-k-1..m` via `tasks: [...]`), optional `res-deep-k` (escalation retries: `res-k-r<r>`, `res-deep-k-r<r>`).
- Stage 2: `planner-k`, `critic-carbon-k`, and (when `OPUS_HIGH` quota `> 0%`) `critic-opus-k`. `planner-k` hands drafts via `@critic-carbon-k @critic-opus-k` (or `@critic-carbon-k` alone at `0%` `OPUS_HIGH` quota). When both run, each Critic sends every per-draft verdict (`[BLOCKING]` or `[APPROVED] #plan_seq`) to `@planner-k`; `planner-k` waits for **both** replies per draft, revises until zero `[BLOCKING]` remain, and posts `@coordinator [done] [APPROVED]` only after both approve the **same latest `#plan_seq`** (citing both approval `#seq`s), or escalates to the swarm coordinator on factual contradiction or after 3 rounds. (When `critic-carbon-k` runs alone, it posts `@coordinator [done] [APPROVED]` directly.)
- Stage 3: `coder-k` / `reviewer-k` (or `coder-k-1..n` / `reviewer-k-1..n`; escalation retries `coder-k[-j]-r<r>`), plus multi-coder seam reviewer **`rev-k-seam`** and optional `coder-k-fix` (reused across seam rounds or suffixed `coder-k-fix-<r>`).
- Stage 4: `commit-rev-k`, `commit-writer-k` (on hygiene retry pass `r`: `commit-rev-k-p<r>`, `commit-writer-k-p<r>`).
- **Seam Gate execution**: `rev-k-seam` starts immediately against `--repo "<REPO_ROOT>" --staged` without waiting for a paired coder and reports `[changes-needed]` to `@coordinator`; coordinator spawns `coder-k-fix` to edit seam files directly in `<REPO_ROOT>` (not via `manage_worktree.py create`), run full tests, `git add -- <seam-files>`, and send `@rev-k-seam [fixed]` directly to `rev-k-seam` for pass `p<N>`. Upon seam `[APPROVED]`, coordinator kills both `rev-k-seam` and `coder-k-fix` before Stage 4.
- **Embedded `/review` budget (`<= 24` chars)**: Use deterministic short helper prefixes **`r<k>-<j>-p<pass>-<role>`** for slice reviewers (e.g. `r1-2-p1-adv` = 11 chars, `<role>` in `{cor,adv,saf,ver}`) and **`r<k>s-p<pass>-<role>`** for `rev-k-seam` (e.g. `r1s-p1-cor` = 10 chars), passing `--repo "<WORKTREE_PATH>" --working` on slice worktrees (adding `-- <slice-files>` on single-stream in-place `<REPO_ROOT>` runs with Step 0 unrelated dirty paths) and `--repo "<REPO_ROOT>" --staged` on `rev-k-seam`. Run the multi-agent `/review` gate once on Pass 1 and verify Pass 2+ fix deltas directly without re-spawning the full helper set.

## Board Setup, `SPAWN_WORKER`, `HANDOFF` & Session Ledgers

- **Iteration 1 init (no unsolicited monitor/narrator)**: In the first `swarm` batch, call `swarm{action: "title", title: "Dev Swarm: <short goal>"}` **and** spawn Stage 1's `res-1` (plus optional `res-deep-1`) before ending your turn so board tools unlock next turn (output a short kickoff with `open_board` on its own line if returned). Do **not** spawn `monitor` (even if `swarm_monitor` is in available skills) or `narrator` workers unless the user explicitly asks for them (if explicitly requested, keep `monitor` strictly isolated from pre-Stage-4 `#seq`/plans during Stage 4 and post `[done] All dev-swarm tasks committed` without `@` before Final Handoff).
- **Spawn (`SPAWN_WORKER`)**: `swarm{action: "add", name: "<role-k>", model: "<pin>", workspace: "inherit", brief: "<brief>"}` (use `tasks: [...]` for sharded `res-k`).
- **Wait & cleanup (`WAIT_FOR_WORKERS` / `KILL_WORKER`)**: Loop `swarm_wait{since: <last_seq>}` + `read_board{since: <last_seq>}`; `swarm{action: "kill", name: "<worker>"}` completed workers at every stage boundary.
- **Handoff & artifacts (`HANDOFF` / `READ_ARTIFACT`)**: Use `post_message` starting with `@<recipient> [status] ...` and cite `#seq` numbers for `read_board{seqs: [...]}` (never copy-pasting long posts). **Spurious `@`-wakeup guard**: never write literal `@` immediately before `coordinator` in peer-to-peer briefs or drafts (`planner-k <-> critic-*-k`, `coder-k <-> reviewer-k`, embedded `*-p*` helpers, `commit-rev-k -> commit-writer-k`); refer to "the swarm coordinator" in prose.
- **`DECISION_LEDGER` & `STATE_LEDGER`**: Create and maintain `<artifactDir>/dev_swarm_decisions.md` (`DECISION_LEDGER`) and `<artifactDir>/dev_swarm_state.md` (`STATE_LEDGER`, updated at **both stage launch**—immediately after creating worktrees/`#commit-k` channels, recording `LAUNCH_SEQ`, and spawning workers—**and stage completion**, tracking macro queue, iteration/stage, active workers, worktree paths/branches, `#seq` artifact refs, Step 0 dirty paths, and last `seq` cursor; re-read from disk after context compaction or session resume) starting at Step 0.

## Stage 4 `CLEANROOM_BARRIER` (Air-Gap Mechanics)

1. **Before spawning Stage 4 workers**, verify Stage 3 is finished and clean, record `LAUNCH_SEQ` (current board head `seq`), and—if `swarm` supports `channel`—create `#commit-k` first.
2. Spawn `commit-rev-k` (`CARBON_HIGH`) and `commit-writer-k` (`CARBON_HIGH`) strictly **without `fork:`** (cold start), subscribed only to `#commit-k` when channels are supported.
3. Pass `MIN_ALLOWED_SEQ = <LAUNCH_SEQ>` in both briefs:
   - **Positive Stage 4 `READ_ARTIFACT` binding**: On Turn 1, `commit-writer-k` calls `read_board{since: <MIN_ALLOWED_SEQ>}` once, sets `last_seen_seq = max(returned seqs, own brief #seq)` (`> MIN_ALLOWED_SEQ`, preventing self-wake on its own brief), and loops `swarm_wait{since: <last_seen_seq>}` + `read_board{since: <last_seen_seq>}` until `commit-rev-k`'s Implementation Report arrives.
   - **Strictly forbidden**: calling `read_board` with `since` omitted, `since < MIN_ALLOWED_SEQ`, or `seqs` containing `<= MIN_ALLOWED_SEQ`; passing `since < MIN_ALLOWED_SEQ` to `swarm_wait`; reading `main` (when in `#commit-k`); reading `<artifactDir>` (`dev_swarm_decisions.md`, `dev_swarm_state.md`), `.system_generated/**` (`board.jsonl`, `transcript*.jsonl`); or inspecting `git reflog`, `git log --all`, or `git branch -a`.
4. **On `[hygiene-fix-needed]`**: `kill` both Stage 4 workers (and archive `#commit-k` if used), fix and re-stage, record a fresh `LAUNCH_SEQ`, create `#commit-k-p<r>` if supported, and cold-start fresh `commit-rev-k-p<r>` / `commit-writer-k-p<r>`.
5. Once `commit-writer-k` posts `@coordinator [done]`, `kill` both Stage 4 workers and **archive the `commit-k` channel** if used.
