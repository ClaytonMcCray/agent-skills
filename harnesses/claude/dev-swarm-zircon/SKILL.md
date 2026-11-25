---
name: dev-swarm-zircon
description: >-
  Ports a unit of Zircon C++ to Rust as a fixed-role swarm over Claude Code's
  native Agent subagent tool—line by line, bug for bug, comment for comment,
  unsafe-heavy by design. Pairs Opus (xhigh) Researchers and Zircon Coder with
  Fable (xhigh) Planner, Critic, Parity Reviewer, Safety Reviewer, and optional
  clean-room commit roles plus babeldiff. Use for Phase 1 Zircon kernel or
  library C++-to-Rust ports; do not use for idiomatic Rust cleanup.
---

# Zircon C++-to-Rust Porting Swarm (`/dev-swarm-zircon` — Claude Code Adapter)

You (the main thread) are **`<LEAD>`**. Read [doctrine.md](./core/doctrine.md) first, then follow [algorithm.md](./core/algorithm.md) end-to-end using the concrete Claude Code bindings below.

## Paths, `$PROMPTS`, `run_babeldiff.py` & Playbooks (`<SKILL_DIR>`)

- Resolve `<SKILL_DIR>` dynamically by canonicalizing this loaded `SKILL.md` through symlinks first (`realpath` / `Path(skill_md).resolve().parent`), then take 3 parent levels up for `<SKILLS_ROOT>` (because `~/.claude/skills/dev-swarm-zircon` is a per-skill symlink into the repo). Dereference `./core` to canonical paths **`<CORE_ZIRCON_DIR> = <SKILLS_ROOT>/core/dev-swarm-zircon`** and **`<CORE_DEV_SWARM_DIR> = <SKILLS_ROOT>/core/dev-swarm`** (shared bindings in [../dev-swarm/SKILL.md](../dev-swarm/SKILL.md)). Pass canonical dereferenced paths in worker briefs (never `<SKILL_DIR>/core/../...`).
- **Session scratchpad (`$PROMPTS`), `STATE_LEDGER` & `DECISION_LEDGER`**: At Stage 0, create a private `0700` session directory via `mktemp -d /tmp/claude-zircon.XXXXXX` (`$PROMPTS`) for `STATE_LEDGER` (`$PROMPTS/state.md`), `DECISION_LEDGER` (`$PROMPTS/decisions.md`), `plan.md`, `impl.md`, `fixes.md`, and saved `babeldiff` artifacts (`babeldiff-summary.txt`, `babeldiff-stacked.txt`).
- **Playbooks & `run_babeldiff.py`**: Pass canonical paths to workers: [doctrine.md](./core/doctrine.md), [researcher.md](../dev-swarm/core/personas/researcher.md), [zircon-coder.md](./core/personas/zircon-coder.md), [zircon-parity-reviewer.md](./core/personas/zircon-parity-reviewer.md), [zircon-safety-reviewer.md](./core/personas/zircon-safety-reviewer.md), and (for optional Stage 6) [cleanroom-commit.md](../dev-swarm/core/personas/cleanroom-commit.md).
- **Preflight `babeldiff` bootstrap**: Run once per session inside the Fuchsia checkout (subsequent `tree`/`commit` runs stay offline):
  ```bash
  python3 "<CORE_ZIRCON_DIR>/scripts/run_babeldiff.py" --update --allow-issues tree --summary
  ```
  ([run_babeldiff.py](./core/scripts/run_babeldiff.py))
- **Out-of-tree sandbox staging**: If workspace permissions prevent workers from reading `<CORE_ZIRCON_DIR>` or `<CORE_DEV_SWARM_DIR>`, copy `doctrine.md`, `run_babeldiff.py`, `researcher.md`, and the three Zircon playbooks into `$PROMPTS/` once at Stage 0 (never staging Stage 6's `cleanroom-commit.md` inside `$PROMPTS/`—stage it only inside `<CLEANROOM_DIR>` when Stage 6 runs).

## `MODEL_MAP`, Capability Escalation & Quota Fallback

| Tier | `Agent` Parameters | Bound `algorithm.md` Personas |
| :--- | :--- | :--- |
| **Volume / Implementation** | `model: "opus"` (Opus 5.5), `effort: "xhigh"` | `Researcher` (x2), `Zircon-Coder` (default writer), optional `Critic-2` |
| **Judgment / Review / Escalation** | `model: "fable"` (Fable 5.1), `effort: "xhigh"` | `Planner`, `Critic-1`, `Zircon-Coder-Escalation`, `Zircon-Parity-Reviewer`, `Zircon-Safety-Reviewer`, optional Stage 6 `Commit-Reviewer` / `Commit-Writer` |

- **Cross-model review guarantee**: Pinning `Zircon-Parity-Reviewer`, `Zircon-Safety-Reviewer`, and optional Stage 6 `Commit-Reviewer` to `fable` guarantees every `opus`-authored port receives independent `fable` parity and safety eyes.
- **Coder escalation**: After a thin or failed attempt by `Zircon-Coder` (`opus`), discard it and spawn a fresh `Zircon-Coder-Escalation` `Agent` on **`model: "fable"`, `effort: "xhigh"`** with the original brief plus a concise failure summary (never repeat `opus` at the same effort on the same brief); disclose the shared-tier `fable` review in the final report.
- **Safety & judgment retries**: On safety disagreement, data races, raw-pointer aliasing, or FFI ownership transfer, spawn a fresh `Zircon-Safety-Reviewer` (`model: "fable"`, `effort: "max"`) narrowed specifically to the disputed sites.
- **Quota fallback (`SWITCH_MODEL`) & cheap lookups**: On quota exhaustion, cross-pin `fable <-> opus` (`effort: "xhigh"`) and record substitutions in the final report. **Never** downgrade core roles to `haiku`. Route narrow symbol/caller lookups to Claude Code's built-in `Explore` subagent (`subagent_type: "Explore"`).

## Per-Unit Naming (`-k`), Lifecycle, `SPAWN_WORKER` & `HANDOFF`

- **Spawn & Wait (`SPAWN_WORKER` / `WAIT_FOR_WORKERS`)**: Dispatch dependent producer->consumer stages as sequential waves while batching independent peers concurrently in a single assistant message per stage, recording active worker names and artifact paths in `STATE_LEDGER`:
  - **Stage 1 (`res-k-1`, `res-k-2`)**: Dual independent `opus` (`effort: "xhigh"`) inventory researchers writing `$PROMPTS/res-k-1.md` and `$PROMPTS/res-k-2.md`; resolve count or symbol discrepancies directly against the C++ lines before Stage 2.
  - **Stage 2 (`planner-k`, `critic-k-1`, optional `critic-k-2`)**: `<LEAD>` (or optional `fable` `Planner`) writes `$PROMPTS/plan.md`; then spawn `Critic-1` (`fable`, `effort: "xhigh"`, plus optional `Critic-2` on `opus` `effort: "xhigh"` for complex units) in parallel.
  - **Stage 3 (`zircon-coder-k` / escalation `zircon-coder-k-esc`)**: Single writer working **in-place** inside the Fuchsia git checkout or `.jiri_root/worktrees/<name>` so `out/` GN/Ninja caches stay intact. Run the mandatory git verification gate immediately after `[done]` (`git add -N -- <new-port-files>`, `git status -s`, `git diff --stat HEAD`).
  - **Stage 4 (`z-parity-k-p<r>`, `z-safety-k-p<r>`)**: `<LEAD>` generates `$PROMPTS/babeldiff-summary.txt` and `$PROMPTS/babeldiff-stacked.txt` via `run_babeldiff.py --allow-issues`, then spawns `Zircon-Parity-Reviewer` and `Zircon-Safety-Reviewer` (`fable`, `effort: "xhigh"`) in parallel writing to `$PROMPTS/z-{parity,safety}-k-p<r>.md`.
  - **Stage 5 (cap: 3 rounds)**: If verdicts are not `PARITY` + `SAME-SURFACE`, synthesize `$PROMPTS/fixes.md`, hand off via `HANDOFF` to resumed or fresh `Zircon-Coder`, re-run the git verification gate and `babeldiff`, and spawn fresh Stage 4 reviewers for pass `r + 1`.
- **Handoff & Artifacts (`HANDOFF` / `READ_ARTIFACT`)**: Exchange `$PROMPTS/*.md` file refs rather than pasting long bodies into `<LEAD>`'s context; workers return concise status tags (`[done]`, `[escalate]`) plus the written file path.
- **Ledgers & Cleanup (`KILL_WORKER`)**:
  - Update `STATE_LEDGER` (`$PROMPTS/state.md`) at both stage launch and stage completion (re-reading from disk after context compaction), tracking C++/Rust paths, pre-existing dirty files, verified build/clippy/test commands, `babeldiff` baselines, and discovered C++ bugs.
  - Log autonomous mapping tradeoffs in `DECISION_LEDGER` (`$PROMPTS/decisions.md`).
  - Foreground `Agent` calls exit automatically on return (stop any background worker at stage boundaries); remove `$PROMPTS` after delivering the consolidated final report.

## Optional Stage 6 `CLEANROOM_BARRIER` (Only When Local Commit Requested)

1. Leave verified edits uncommitted unless the user explicitly requested a local git commit. When committing, isolate Stage 0 pre-existing dirty paths, stage only the unit's intended files (`git add -- <files>`), and create a separate private `0700` clean-room directory via `mktemp -d /tmp/claude-cleanroom.XXXXXX` (`<CLEANROOM_DIR>`, strictly outside `$PROMPTS/`).
2. If workspace sandboxing blocks out-of-tree reads, copy `cleanroom-commit.md` into `<CLEANROOM_DIR>/cleanroom-commit.md` (never into `$PROMPTS/`).
3. Spawn `Commit-Reviewer` (`model: "fable"`, `effort: "xhigh"`) as a fresh cold-start `Agent` call first. Only **after** `Commit-Reviewer` returns cleanly and `<CLEANROOM_DIR>/report.md` exists, spawn `Commit-Writer` (`model: "fable"`, `effort: "xhigh"`) as a second fresh cold-start `Agent` call—strictly **never** resumed via `SendMessage`/`resume` or forked from `<LEAD>` or earlier workers, and never spawning `Commit-Writer` on `[hygiene-fix-needed]`.
4. Enforce strict input isolation:
   - Pass **`Commit-Reviewer`** **ONLY**: a fresh sanitized Problem Statement, `<REPO_ROOT>`, pre-existing Step 0 dirty paths to ignore, the `cleanroom-commit.md` path, and `<CLEANROOM_DIR>/report.md`.
   - Pass **`Commit-Writer`** **ONLY**: `<CLEANROOM_DIR>/report.md`, `<REPO_ROOT>`, and the `cleanroom-commit.md` path.
   - **Strictly forbid** both Stage 6 workers from reading `$PROMPTS/`, `state.md`, `decisions.md`, `plan.md`, `fixes.md`, `babeldiff-*.txt`, or running `git reflog`, `git log --all`, or `git branch -a`.
5. **On `[hygiene-fix-needed]`**: Remove `<CLEANROOM_DIR>`, fix and re-stage the changes, create a fresh `/tmp/claude-cleanroom.XXXXXX`, and cold-start a fresh `Commit-Reviewer` pass.
6. Once `Commit-Writer` confirms the local commit (`[done]`), remove `<CLEANROOM_DIR>` immediately before advancing to the next unit or delivering Final Handoff.
