---
name: dev-swarm
description: >-
  Autonomous local development swarm orchestrated by the lead agent over Claude
  Code's native Agent subagent tool. Runs an exterior task loop and interior
  pipeline of Researcher (Opus xhigh), Planner <-> Critic (Fable xhigh),
  parallel or single Coder (Opus xhigh, escalating failures to Fable xhigh) <->
  paired Reviewer (Fable xhigh driving /review), and an air-gapped clean-room
  Commit Reviewer -> Commit Writer stage (Fable xhigh). Use when the user
  invokes /dev-swarm or asks to run the full multi-agent development swarm.
---

# Autonomous Development Swarm (`/dev-swarm` — Claude Code Adapter)

You (the main thread) are **`<LEAD>`**. Follow [algorithm.md](./core/algorithm.md) end-to-end using the concrete Claude Code bindings below.

## Paths, Worktree Helper & Playbooks (`<SKILL_DIR>`)

- Resolve `<SKILL_DIR>` dynamically by canonicalizing this loaded `SKILL.md` through symlinks first (`realpath` / `Path(skill_md).resolve().parent`), then take 3 parent levels up for `<SKILLS_ROOT>` (because `~/.claude/skills/<name>` is a per-skill symlink into the repo). Dereference `./core` to canonical paths **`<CORE_DEV_SWARM_DIR> = <SKILLS_ROOT>/core/dev-swarm`**, **`<CORE_REVIEW_DIR> = <SKILLS_ROOT>/core/review`**, and **`<REVIEW_SKILL_PATH> = <SKILLS_ROOT>/harnesses/claude/review/SKILL.md`** ([../review/SKILL.md](../review/SKILL.md)). Pass canonical dereferenced paths in worker briefs (never `<SKILL_DIR>/core/../...`) so lexical `..` traversal across `./core` symlinks cannot break file reads.
- Manage isolated worktrees with `python3 "<CORE_DEV_SWARM_DIR>/scripts/manage_worktree.py" --repo "<REPO_ROOT>" {create|status|apply|cleanup} ...` ([manage_worktree.py](./core/scripts/manage_worktree.py)); on single-stream repos needing large in-tree build caches with clean target files, `<WORKTREE_PATH> = <REPO_ROOT>` may run in-place (skipping `manage_worktree.py`).
- Pass every worker its canonical playbook path (`<CORE_DEV_SWARM_DIR>/personas/<name>.md`) as its mandatory first read: [researcher.md](./core/personas/researcher.md), [planner-critic.md](./core/personas/planner-critic.md), [coder-reviewer.md](./core/personas/coder-reviewer.md), [cleanroom-commit.md](./core/personas/cleanroom-commit.md).

## `MODEL_MAP`, Capability Escalation & Quota Fallback

| Tier | `Agent` Parameters | Bound `algorithm.md` Personas |
| :--- | :--- | :--- |
| **Volume / Implementation** | `model: "opus"` (Opus 5.5), `effort: "xhigh"` | `Researcher-Broad`, `Researcher-Deep`, `Coder` |
| **Judgment / Critique / Review** | `model: "fable"` (Fable 5.1), `effort: "xhigh"` | `Planner`, `Critic`, `Reviewer`, `Commit-Reviewer`, `Commit-Writer` |

- **Cross-model review guarantee**: Pinning `Reviewer` and `Commit-Reviewer` to `fable` guarantees every `opus`-authored diff receives independent `fable` review eyes.
- **Capability Escalation (distinct from quota fallback)**: Never re-run a failed or thin `opus` `Researcher` or `Coder` at the same effort on the same brief. Discard the failed worker, reset an isolated `/tmp` `<WORKTREE_PATH>` (or document prior partial edits in-place), and spawn a fresh retry `Agent` on **`model: "fable"`, `effort: "xhigh"`** with the original brief plus a concise failure summary. If `Coder` escalates to `fable`, disclose the shared-tier `fable` review in the final handoff summary. On failed `fable` workers, retry with `effort: "max"` or a narrowed brief focused on the disputed sites.
- **Cheap search subagent routing**: Route narrow search-shaped lookups (*"every caller of X"*, *"where is Y configured"*) to Claude Code's built-in `Explore` subagent (`subagent_type: "Explore"`) rather than spawning a frontier swarm worker.
- **Quota fallback (`SWITCH_MODEL`)**: On quota exhaustion or capacity limits, cross-pin the affected worker between `fable <-> opus` (`effort: "xhigh"`) pointing at the same artifact files and log the substitution in the final report. **Never** downgrade core roles to `haiku`.

## Per-Iteration Naming (`-k`) & File-Backed `$PROMPTS` Layout

At Step 0, create a private `0700` session scratchpad via `mktemp -d /tmp/claude-swarm.XXXXXX` (`$PROMPTS`) storing numbered briefs/reports plus both ledgers:
- **`STATE_LEDGER` (`$PROMPTS/state.md`)**: Updated at **both stage launch** (recording worktrees, active workers, and stage bounds) **and stage completion** (recording produced artifact paths); re-read from disk after any context compaction.
- **`DECISION_LEDGER` (`$PROMPTS/decisions.md`)**: Autonomous engineering tradeoffs distilled into the final handoff summary.
- **Stage 1**: `res-k` (or topic-sharded `res-k-1..m`), optional `res-deep-k` (escalation retries `res-k-r<r>`), writing to `$PROMPTS/res-k*.md`.
- **Stage 2**: `planner-k` and `critic-k` writing numbered draft/critique files `$PROMPTS/plan-k-r<r>.md` and `$PROMPTS/critic-k-r<r>.md` (3-round cap).
- **Stage 3**: `coder-k[-j]` / `reviewer-k[-j]` (escalation retries `coder-k[-j]-r<r>`), plus multi-coder seam reviewer `rev-k-seam` and optional `coder-k-fix`.
- **Stage 4**: `commit-rev-k`, `commit-writer-k` (hygiene retries `commit-rev-k-p<r>`, `commit-writer-k-p<r>`), strictly outside `$PROMPTS/`.

## Lifecycle, `SPAWN_WORKER`, `HANDOFF` & Embedded `/review`

- **Spawn & Wait (`SPAWN_WORKER` / `WAIT_FOR_WORKERS`)**: Invoke Claude Code's native `Agent` tool with explicit `model`, `effort`, concise `description` (`<role>-<k>`), and cold-start `prompt` (never passing raw transcript dumps). Because `Agent` calls execute immediately without a sleep/wait board, dispatch dependent producer->consumer pairs as **sequential waves** (`Planner -> Critic -> Planner`, `parallel slice Coders -> parallel paired Reviewers`, `Commit-Reviewer -> Commit-Writer`), batching only independent peers within each wave (sharded researchers, parallel coder slices, parallel `/review` specialists) in a single assistant message and awaiting all returned handles.
- **Handoff & Artifacts (`HANDOFF` / `READ_ARTIFACT`)**: Instruct each worker to write its full report to its assigned `$PROMPTS/<role>-<k>-<round>.md` path and return a concise status tag (`[done]`, `[BLOCKING]`, `[APPROVED]`, `[ready]`, `[fixed]`, `[changes-needed]`, `[escalate]`) plus the file path. Pass file path refs between workers rather than pasting long bodies into `<LEAD>`'s context; `<LEAD>` drives Stage 2 (`Planner <-> Critic`, 3-round cap) and Stage 3 (`Coder <-> Reviewer`, 4-cycle cap) waves by passing the latest artifact path to fresh or resumed-within-stage `Agent` turns.
- **Embedded `/review` & Multi-Coder Seam Gate**:
  - In Stage 3 Pass 1, if `Reviewer` has nested `Agent` access it invokes [../review/SKILL.md](../review/SKILL.md) directly; if nested `Agent` dispatch is restricted by tool permissions, `<LEAD>` runs `collect_review_context.py` (`--repo "<WORKTREE_PATH>" --working [-- <slice-files>]`) and dispatches the Pass 1 `/review` specialist `Agent`s in parallel in one message batch writing into a dedicated per-pass subdir `$PROMPTS/rev-<k>[-<j>]-p1/<role>.md`, then hands those report refs to `Reviewer` (`fable`) to verify claims against `verifier.md` (plus `correctness-style.md` inline on Small diffs), audit for leaked meta-comments, run build/lint/tests once, remove only `<SUMMARY_DIR>` and that pass subdir, and emit `[APPROVED]` or `[changes-needed]`.
  - On Pass 2+ (`[fixed]`), `Reviewer` verifies the fix delta and re-runs tests directly without re-spawning the Pass 1 specialist ensemble.
  - When `> 1` parallel coder slices merge via `manage_worktree.py apply`, run the full test suite and a `fable` seam `Reviewer` against `--repo "<REPO_ROOT>" --staged` (using `<LEAD>` to spawn Pass 1 seam specialists in parallel if nested `Agent` calls are restricted). On `[changes-needed]`, spawn a fix `Coder` editing `<REPO_ROOT>` directly, re-test, `git add -- <seam-files>`, and re-verify the delta.
- **Cleanup (`KILL_WORKER`)**: Foreground `Agent` calls terminate automatically on return; stop any background agent before advancing stages. Remove temporary worktrees (`manage_worktree.py cleanup`) after Stage 3 and delete `$PROMPTS` after delivering the final handoff summary.

## Stage 4 `CLEANROOM_BARRIER` (Air-Gap Mechanics)

1. Verify Stage 3 is complete, stage only the iteration's intended files (`git add -- <files>`), isolate any Step 0 pre-existing unrelated dirty paths, and create a separate private `0700` clean-room directory via `mktemp -d /tmp/claude-cleanroom.XXXXXX` (`<CLEANROOM_DIR>`, strictly outside `$PROMPTS/`).
2. Spawn `Commit-Reviewer` (`model: "fable"`, `effort: "xhigh"`) as a fresh cold-start `Agent` call first. Only **after** `Commit-Reviewer` returns cleanly and `<CLEANROOM_DIR>/report.md` exists, spawn `Commit-Writer` (`model: "fable"`, `effort: "xhigh"`) as a second fresh cold-start `Agent` call—strictly **never** resumed via `SendMessage`/`resume` or forked from `<LEAD>` or any earlier worker, and never spawning `Commit-Writer` on `[hygiene-fix-needed]`.
3. Enforce strict input isolation:
   - Pass **`Commit-Reviewer`** **ONLY**: a fresh sanitized Problem Statement, `<REPO_ROOT>`, any pre-existing Step 0 dirty paths to ignore, `<CORE_DEV_SWARM_DIR>/personas/cleanroom-commit.md`, and the output report path `<CLEANROOM_DIR>/report.md`.
   - Pass **`Commit-Writer`** **ONLY**: `<CLEANROOM_DIR>/report.md`, `<REPO_ROOT>`, and `<CORE_DEV_SWARM_DIR>/personas/cleanroom-commit.md`.
   - **Strictly forbidden** for both Stage 4 workers: reading `$PROMPTS/`, `state.md`, `decisions.md`, plan/review transcripts, or running `git reflog`, `git log --all`, or `git branch -a`.
4. **On `[hygiene-fix-needed]`**: Remove `<CLEANROOM_DIR>`, fix and re-stage the changes, create a fresh `/tmp/claude-cleanroom.XXXXXX`, and cold-start a fresh `Commit-Reviewer` pass.
5. Once `Commit-Writer` confirms the local commit (`[done]`), remove `<CLEANROOM_DIR>` immediately before advancing or delivering Final Handoff.
