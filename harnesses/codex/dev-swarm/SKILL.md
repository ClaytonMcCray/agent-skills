---
name: dev-swarm
description: >-
  Autonomous local development swarm orchestrated by the lead agent over Codex
  CLI subagents. Runs an exterior task loop and interior pipeline of Researcher
  (GPT-6 Sol xhigh / Astra xhigh), Planner (GPT-6 Astra xhigh) <-> parallel
  Critic (GPT-6 Astra xhigh + optional Claude Fable via claude-review), Coder
  (GPT-6 Sol xhigh, escalating to GPT-6 Astra xhigh) <-> paired Reviewer (GPT-6
  Astra xhigh driving /review), and an air-gapped Commit Reviewer -> Commit
  Writer stage (GPT-6 Astra xhigh). Use when the user invokes /dev-swarm or
  asks to run the full multi-agent development swarm.
---

# Autonomous Development Swarm (`/dev-swarm` — Codex Adapter)

You (the main thread) are **`<LEAD>`**. Follow [algorithm.md](./core/algorithm.md) end-to-end using the concrete Codex bindings below.

## Paths, Worktree Helper & Playbooks (`<SKILL_DIR>`)

- Resolve `<SKILL_DIR>` dynamically by canonicalizing this loaded `SKILL.md` through symlinks first (`realpath` / `Path(skill_md).resolve().parent`), then take 3 parent levels up for `<SKILLS_ROOT>` (because `~/.codex/skills/<name>` is a per-skill symlink into the repo). Dereference `./core` to canonical paths **`<CORE_DEV_SWARM_DIR> = <SKILLS_ROOT>/core/dev-swarm`**, **`<CORE_REVIEW_DIR> = <SKILLS_ROOT>/core/review`**, **`<REVIEW_SKILL_PATH> = <SKILLS_ROOT>/harnesses/codex/review/SKILL.md`** ([../review/SKILL.md](../review/SKILL.md)), and **`<CLAUDE_REVIEW_SKILL_PATH> = <SKILLS_ROOT>/harnesses/codex/claude-review/SKILL.md`** ([../claude-review/SKILL.md](../claude-review/SKILL.md)). Pass canonical dereferenced paths in worker briefs (never `<SKILL_DIR>/core/../...`).
- Manage isolated worktrees with `python3 "<CORE_DEV_SWARM_DIR>/scripts/manage_worktree.py" --repo "<REPO_ROOT>" {create|status|apply|cleanup} ...` ([manage_worktree.py](./core/scripts/manage_worktree.py)); on single-stream repos needing large in-tree build caches with clean target files, `<WORKTREE_PATH> = <REPO_ROOT>` may run in-place (skipping `manage_worktree.py`).
- Pass workers their canonical playbook path (`<CORE_DEV_SWARM_DIR>/personas/<name>.md`) as their mandatory first read: [researcher.md](./core/personas/researcher.md), [planner-critic.md](./core/personas/planner-critic.md), [coder-reviewer.md](./core/personas/coder-reviewer.md), [cleanroom-commit.md](./core/personas/cleanroom-commit.md).

## `MODEL_MAP`, Cross-Family Critique, Escalation & Quota Fallback

| Tier | Codex Subagent Parameters | Bound `algorithm.md` Personas |
| :--- | :--- | :--- |
| **Volume / Implementation** | `model = "gpt-6-sol"`, `reasoning_effort = "xhigh"`, `fork_context = false` | `Researcher-Broad`, `Coder` |
| **Judgment / Architecture / Review** | `model = "gpt-6-astra"`, `reasoning_effort = "xhigh"`, `fork_context = false` | `Researcher-Deep`, `Planner`, primary `Critic`, `Reviewer`, `Commit-Reviewer`, `Commit-Writer` |
| **Optional Cross-Family `Critic`** | [../claude-review/SKILL.md](../claude-review/SKILL.md) (`--mode plan --model fable --effort high` via `gpt-6-sol` `low` relay) | Parallel second `Critic` in Stage 2 whenever `claude --safe-mode auth status` (with `ANTHROPIC_*`/`CLAUDE_CODE_USE_*` unset) reports `authMethod == "claude.ai"`, falling back to solo `gpt-6-astra` if absent or on `auth_error`, `claude_error`, or `timeout` |

- **Cross-model review guarantee**: Pinning `Reviewer` and `Commit-Reviewer` to `gpt-6-astra` (`xhigh`) guarantees every `gpt-6-sol` diff receives independent `gpt-6-astra` review eyes.
- **`claude-review` relay contract**: Because `claude_review.py` runs Claude under `--cwd "<REPO_ROOT>" --restricted` (no reads outside `<REPO_ROOT>`, no writes), `<LEAD>` passes `/tmp` artifact paths (`$PROMPTS/plan-k-r<r>.md`, `$PROMPTS/res-k*.md`) to the `gpt-6-sol` (`low`) relay; the **relay** reads and embeds `$PROMPTS/plan-k-r<r>.md` and `$PROMPTS/res-k*.md` into `--prompt-file` (stating explicitly that external `/tmp` and `<CORE_DEV_SWARM_DIR>` files are already embedded so Claude must not `Read` outside `--cwd`) before invoking `claude_review.py --cwd "<REPO_ROOT>"`—passing `--resume <session_file>` on Stage 2 rounds 2–3 **only** when the prior turn returned `resumable == true` (otherwise starting fresh)—and returns `status`, `resumable`, `findings_omitted`, `report_path`/`review_path`, and compact findings verbatim (or saves them to `$PROMPTS/critic-claude-k-r<r>.md`). Inspect `review_path` (`review.json`) whenever `findings_omitted > 0` before evaluating all findings; map `status == "ok"` with zero `open` `critical`/`high`/`medium` findings across the complete set to `[APPROVED]`, fall back to solo `gpt-6-astra` on `auth_error`, `claude_error`, or `timeout` transport/quota failures (recording the fallback), and treat remaining open findings or `incomplete`/`protocol_error` coverage gaps as `[BLOCKING]`.
- **Capability escalation**: Never re-run a failed or thin `gpt-6-sol` worker at the same effort on the same brief—close it, reset an isolated `/tmp` `<WORKTREE_PATH>` (or document prior partial edits in-place), and spawn a fresh **`gpt-6-astra` (`reasoning_effort = "xhigh"`, `fork_context = false`)** worker with the original brief plus a concise failure summary. If `Coder` escalates to `gpt-6-astra`, add an optional `claude-review` check if available or disclose shared-tier review in the final report. On failed `gpt-6-astra` workers, retry with a narrowed brief focused on the disputed sites (or invoke `claude-review` for independent diagnosis).
- **Quota fallback (`SWITCH_MODEL`) & cheap lookups**: On quota exhaustion, cross-pin `gpt-6-astra <-> gpt-6-sol` (`reasoning_effort = "xhigh"`) and log the substitution in the final report. **Never** downgrade core roles to `low` or mini tiers; use `gpt-6-sol` `low` strictly for narrow search-shaped file lookups or `claude-review` transport relay.

## Per-Iteration Naming (`-k`) & File-Backed `$PROMPTS` Layout

At Step 0, create a private `0700` session scratchpad via `mktemp -d /tmp/codex-swarm.XXXXXX` (`$PROMPTS`) storing numbered briefs/reports plus both ledgers:
- **`STATE_LEDGER` (`$PROMPTS/state.md`)**: Updated at **both stage launch** (recording worktrees, active subagents, and stage bounds) **and stage completion** (recording artifact refs); re-read from disk after any context compaction.
- **`DECISION_LEDGER` (`$PROMPTS/decisions.md`)**: Autonomous engineering tradeoffs distilled into the final handoff summary.
- **Stage 1**: `res-k` (or topic-sharded `res-k-1..m`), optional `res-deep-k` (escalation retries `res-k-r<r>`), writing to `$PROMPTS/res-k*.md`.
- **Stage 2**: `planner-k`, `critic-astra-k`, and optional `critic-claude-k` (`gpt-6-sol` `low` relay), exchanging `$PROMPTS/plan-k-r<r>.md` and critic reports.
- **Stage 3**: `coder-k[-j]` / `reviewer-k[-j]` (escalation retries `coder-k[-j]-r<r>`), plus multi-coder seam reviewer `rev-k-seam` and optional `coder-k-fix`.
- **Stage 4**: `commit-rev-k`, `commit-writer-k` (hygiene retries `commit-rev-k-p<r>`, `commit-writer-k-p<r>`), strictly outside `$PROMPTS/`.

## Lifecycle, `SPAWN_WORKER`, `HANDOFF` & Embedded `/review`

- **Spawn & Wait (`SPAWN_WORKER` / `WAIT_FOR_WORKERS`)**: Spawn every worker with **`fork_context = false`** (fresh cold-start context, never inheriting `<LEAD>`'s transcript!), explicit `model` and `reasoning_effort`. Because Codex subagents execute immediately without a board-sleep primitive, dispatch dependent producer->consumer pairs as **sequential waves** (`Planner -> parallel Critics -> Planner`, `parallel slice Coders -> parallel paired Reviewers`, `Commit-Reviewer -> Commit-Writer`), launching only independent peers concurrently within each wave (sharded researchers, parallel Stage 2 Critics, parallel coder slices) and waiting until the wave finishes.
- **Handoff & Artifacts (`HANDOFF` / `READ_ARTIFACT`)**: Exchange `$PROMPTS/<role>-<k>-<round>.md` path refs rather than pasting large bodies into `<LEAD>`'s context. In Stage 2 with parallel Critics (`gpt-6-astra` + `claude-review` relay), await both verdicts per draft before revising or escalating (3-round cap) and lock `PLAN_REF` only when both approve the same draft. In Stage 3, drive `Coder <-> Reviewer` waves (4-cycle cap) via updated artifact refs (`send_input` or fresh `fork_context = false` workers).
- **Embedded `/review` & Multi-Coder Seam Gate**:
  - In Stage 3 Pass 1, if `Reviewer` cannot spawn nested subagents, `<LEAD>` runs `collect_review_context.py` (`--repo "<WORKTREE_PATH>" --working [-- <slice-files>]`) and spawns Pass 1 `/review` specialist subagents (`fork_context = false`) in parallel writing into a dedicated per-pass subdir `$PROMPTS/rev-<k>[-<j>]-p1/<role>.md`, then hands those report refs to `Reviewer` (`gpt-6-astra`) for `verifier.md` filtering (plus `correctness-style.md` inline on Small diffs), meta-comment audit, test execution, cleanup of only `<SUMMARY_DIR>` and that pass subdir, and verdict.
  - On Pass 2+ (`[fixed]`), `Reviewer` verifies the fix delta and re-runs tests directly without re-spawning the Pass 1 specialist ensemble.
  - When `> 1` parallel slices merge via `manage_worktree.py apply`, run the full test suite and a `gpt-6-astra` seam `Reviewer` on `--repo "<REPO_ROOT>" --staged` (spawning a fix `Coder` editing `<REPO_ROOT>` directly on `[changes-needed]`).
- **Cleanup (`KILL_WORKER`)**: Close completed subagents at every stage boundary so the active roster stays lean; run `manage_worktree.py cleanup` after Stage 3 and remove `$PROMPTS` after delivering Final Handoff.

## Stage 4 `CLEANROOM_BARRIER` (Air-Gap Mechanics)

1. Verify Stage 3 is complete, stage only the iteration's intended files (`git add -- <files>`), isolate any Step 0 pre-existing unrelated dirty paths, and create a separate private `0700` clean-room directory via `mktemp -d /tmp/codex-cleanroom.XXXXXX` (`<CLEANROOM_DIR>`, strictly outside `$PROMPTS/`).
2. Spawn `Commit-Reviewer` (`model = "gpt-6-astra"`, `reasoning_effort = "xhigh"`, `fork_context = false`) first. Only **after** `Commit-Reviewer` completes cleanly and `<CLEANROOM_DIR>/report.md` exists, spawn `Commit-Writer` (`model = "gpt-6-astra"`, `reasoning_effort = "xhigh"`, `fork_context = false`) as a fresh cold-start subagent—never spawning `Commit-Writer` on `[hygiene-fix-needed]`.
3. Enforce strict input isolation:
   - Pass **`Commit-Reviewer`** **ONLY**: a fresh sanitized Problem Statement, `<REPO_ROOT>`, any pre-existing Step 0 dirty paths to ignore, `<CORE_DEV_SWARM_DIR>/personas/cleanroom-commit.md`, and `<CLEANROOM_DIR>/report.md`.
   - Pass **`Commit-Writer`** **ONLY**: `<CLEANROOM_DIR>/report.md`, `<REPO_ROOT>`, and `<CORE_DEV_SWARM_DIR>/personas/cleanroom-commit.md`.
   - **Strictly forbidden** for both Stage 4 workers: reading `$PROMPTS/`, `state.md`, `decisions.md`, plan/review transcripts, or running `git reflog`, `git log --all`, or `git branch -a`.
4. **On `[hygiene-fix-needed]`**: Close `Commit-Reviewer`, remove `<CLEANROOM_DIR>`, fix and re-stage the changes, create a fresh `/tmp/codex-cleanroom.XXXXXX`, and cold-start a fresh `Commit-Reviewer` pass.
5. Once `Commit-Writer` confirms the local commit (`[done]`), close both Stage 4 subagents and remove `<CLEANROOM_DIR>` immediately.
