---
name: dev-swarm-zircon
description: >-
  Ports a unit of Zircon C++ to Rust as a fixed-role swarm over Codex CLI
  subagents—line by line, bug for bug, comment for comment, unsafe-heavy by
  design. Combines GPT-6 Sol (xhigh) and GPT-6 Astra (xhigh) research, planning,
  optional cross-family Claude critique via claude-review, Zircon Coder, Parity
  Reviewer, Safety Reviewer, and babeldiff. Use for Phase 1 Zircon kernel or
  library C++-to-Rust ports; do not use for idiomatic Rust cleanup.
---

# Zircon C++-to-Rust Porting Swarm (`/dev-swarm-zircon` — Codex Adapter)

You (the main thread) are **`<LEAD>`**. Read [doctrine.md](./core/doctrine.md) first, then follow [algorithm.md](./core/algorithm.md) end-to-end using the concrete Codex bindings below.

## Paths, `$PROMPTS`, `run_babeldiff.py` & Playbooks (`<SKILL_DIR>`)

- Resolve `<SKILL_DIR>` dynamically by canonicalizing this loaded `SKILL.md` through symlinks first (`realpath` / `Path(skill_md).resolve().parent`), then take 3 parent levels up for `<SKILLS_ROOT>` (because `~/.codex/skills/dev-swarm-zircon` is a per-skill symlink into the repo). Dereference `./core` to canonical paths **`<CORE_ZIRCON_DIR> = <SKILLS_ROOT>/core/dev-swarm-zircon`**, **`<CORE_DEV_SWARM_DIR> = <SKILLS_ROOT>/core/dev-swarm`** (shared bindings in [../dev-swarm/SKILL.md](../dev-swarm/SKILL.md)), and **`<CLAUDE_REVIEW_SKILL_PATH> = <SKILLS_ROOT>/harnesses/codex/claude-review/SKILL.md`** ([../claude-review/SKILL.md](../claude-review/SKILL.md)). Pass canonical dereferenced paths in worker briefs (never `<SKILL_DIR>/core/../...`).
- **Session scratchpad (`$PROMPTS`), `STATE_LEDGER` & `DECISION_LEDGER`**: At Stage 0, create a private `0700` session directory via `mktemp -d /tmp/codex-zircon.XXXXXX` (`$PROMPTS`) for `STATE_LEDGER` (`$PROMPTS/state.md`), `DECISION_LEDGER` (`$PROMPTS/decisions.md`), `plan.md`, `impl.md`, `fixes.md`, and saved `babeldiff` artifacts (`babeldiff-summary.txt`, `babeldiff-stacked.txt`).
- **Playbooks & `run_babeldiff.py`**: Pass canonical paths to workers: [doctrine.md](./core/doctrine.md), [researcher.md](../dev-swarm/core/personas/researcher.md), [zircon-coder.md](./core/personas/zircon-coder.md), [zircon-parity-reviewer.md](./core/personas/zircon-parity-reviewer.md), [zircon-safety-reviewer.md](./core/personas/zircon-safety-reviewer.md), and (for optional Stage 6) [cleanroom-commit.md](../dev-swarm/core/personas/cleanroom-commit.md).
- **Preflight `babeldiff` bootstrap**: Run once per session inside the Fuchsia checkout (subsequent `tree`/`commit` runs stay offline):
  ```bash
  python3 "<CORE_ZIRCON_DIR>/scripts/run_babeldiff.py" --update --allow-issues tree --summary
  ```
  ([run_babeldiff.py](./core/scripts/run_babeldiff.py))
- **Out-of-tree sandbox staging**: If workspace permissions prevent subagents from reading `<CORE_ZIRCON_DIR>` or `<CORE_DEV_SWARM_DIR>`, copy `doctrine.md`, `run_babeldiff.py`, `researcher.md`, and the three Zircon playbooks into `$PROMPTS/` once at Stage 0 (never staging Stage 6's `cleanroom-commit.md` inside `$PROMPTS/`—stage it only inside `<CLEANROOM_DIR>` when Stage 6 runs).

## `MODEL_MAP`, Cross-Family Critique, Escalation & Quota Fallback

| Tier | Codex Subagent Parameters | Bound `algorithm.md` Personas |
| :--- | :--- | :--- |
| **Volume / Broad Inventory** | `model = "gpt-6-sol"`, `reasoning_effort = "xhigh"`, `fork_context = false` | `Researcher` (`res-k-1` full C++ inventory) |
| **Judgment / Systems Porting & Review** | `model = "gpt-6-astra"`, `reasoning_effort = "xhigh"`, `fork_context = false` | `Researcher` (`res-k-2` full C++ inventory + deep UB/lock probe), `Planner`, `Critic-1`, `Zircon-Coder`, `Zircon-Coder-Escalation`, `Zircon-Parity-Reviewer`, `Zircon-Safety-Reviewer`, optional Stage 6 `Commit-Reviewer` / `Commit-Writer` |
| **Optional Cross-Family Critique** | [../claude-review/SKILL.md](../claude-review/SKILL.md) (`--model fable --effort high` via `gpt-6-sol` `low` relay) | Parallel Stage 2 `Critic-2` (`--mode plan`) whenever `claude --safe-mode auth status` (with `ANTHROPIC_*`/`CLAUDE_CODE_USE_*` unset) reports `authMethod == "claude.ai"` (`Critic-1` solo if absent or on `auth_error`, `claude_error`, or `timeout`), plus optional Stage 4 second opinion (`--mode review`) |

- **Independent review & `claude-review` relay contract**: `Zircon-Parity-Reviewer` and `Zircon-Safety-Reviewer` always run in fresh `fork_context = false` subagents. When invoking `claude-review`, `<LEAD>` passes external paths (`<CORE_ZIRCON_DIR>/doctrine.md`, `$PROMPTS/plan.md`, `$PROMPTS/res-k-{1,2}.md`, and `babeldiff-*.txt` in Stage 4) to the `gpt-6-sol` (`low`) relay; the **relay** reads and embeds those files into `--prompt-file` (stating explicitly that external `/tmp` and `<CORE_ZIRCON_DIR>` files are already embedded so Claude must not `Read` outside `--cwd`) before running `claude_review.py --cwd "<REPO_ROOT>"`—passing `--resume <session_file>` on follow-up rounds **only** when the prior turn returned `resumable == true` (otherwise starting fresh)—and returns `status`, `resumable`, `findings_omitted`, `report_path`/`review_path`, and compact findings verbatim (or saves them to `$PROMPTS/critic-claude-k-r<r>.md`). Inspect `review_path` (`review.json`) whenever `findings_omitted > 0` before evaluating the full finding set; map `status == "ok"` with zero `open` `critical`/`high`/`medium` findings across the complete set to `[APPROVED]`, fall back to solo `gpt-6-astra` on `auth_error`, `claude_error`, or `timeout` transport/quota failures (recording the fallback), and treat remaining open findings or `incomplete`/`protocol_error` coverage gaps as `[BLOCKING]`. When `gpt-6-astra` both writes and reviews without Claude, disclose the fresh-context shared-tier review explicitly in the final report.
- **Escalation**: Never repeat a failed or thin `gpt-6-sol` `res-k-1` attempt at the same effort—close it and escalate to a fresh `gpt-6-astra` (`xhigh`, `fork_context = false`) worker with the failure summary. On `Zircon-Coder` failure or safety disagreement (data races, raw-pointer aliasing, FFI ownership transfer), spawn a fresh `gpt-6-astra` (`xhigh`, `fork_context = false`) retry narrowed to the failing functions or disputed sites, or invoke `claude-review` for independent diagnosis.
- **Quota fallback (`SWITCH_MODEL`) & cheap lookups**: On quota exhaustion, cross-pin `gpt-6-astra <-> gpt-6-sol` (`reasoning_effort = "xhigh"`) and record substitutions in the final report. **Never** downgrade core roles to `low` or mini tiers (use `gpt-6-sol` `low` strictly for narrow lookups or `claude-review` transport relay).

## Per-Unit Naming (`-k`), Lifecycle, `SPAWN_WORKER` & `HANDOFF`

- **Spawn & Wait (`SPAWN_WORKER` / `WAIT_FOR_WORKERS`)**: Spawn every worker with **`fork_context = false`**, dispatching dependent producer->consumer stages as sequential waves while launching independent peers concurrently within each wave and recording active workers in `STATE_LEDGER`:
  - **Stage 1 (`res-k-1`, `res-k-2`)**: Dual independent researchers (`gpt-6-sol` `xhigh` + `gpt-6-astra` `xhigh`), both producing the full C++ inventory (`res-k-2` also probing UB/locks) in `$PROMPTS/res-k-{1,2}.md`; resolve count or symbol discrepancies directly against the C++ lines before Stage 2.
  - **Stage 2 (`planner-k`, `critic-astra-k`, optional `critic-claude-k`)**: `<LEAD>` (or optional `gpt-6-astra` `Planner`) writes `$PROMPTS/plan.md`; then spawn `Critic-1` (`gpt-6-astra` `xhigh`) + optional `Critic-2` (`claude-review` relay) in parallel.
  - **Stage 3 (`zircon-coder-k`)**: Single writer (`gpt-6-astra` `xhigh`) working **in-place** inside the Fuchsia git checkout or `.jiri_root/worktrees/<name>` so `out/` GN/Ninja caches stay intact (kept open across Stages 3–5 until Stage 6). Run the mandatory git verification gate immediately after `[done]` (`git add -N -- <new-port-files>`, `git status -s`, `git diff --stat HEAD`).
  - **Stage 4 (`z-parity-k-p<r>`, `z-safety-k-p<r>`)**: `<LEAD>` generates `$PROMPTS/babeldiff-summary.txt` and `$PROMPTS/babeldiff-stacked.txt` via `run_babeldiff.py --allow-issues`, then spawns `Zircon-Parity-Reviewer` and `Zircon-Safety-Reviewer` (`gpt-6-astra` `xhigh`) in parallel writing to `$PROMPTS/z-{parity,safety}-k-p<r>.md`.
  - **Stage 5 (cap: 3 rounds)**: If verdicts are not `PARITY` + `SAME-SURFACE`, synthesize `$PROMPTS/fixes.md`, hand off via `HANDOFF` to the open `zircon-coder-k`, re-run the git verification gate and `babeldiff`, and spawn fresh Stage 4 reviewers for pass `r + 1`.
- **Handoff & Artifacts (`HANDOFF` / `READ_ARTIFACT`)**: Exchange `$PROMPTS/*.md` paths rather than pasting long bodies into `<LEAD>`'s context; workers return concise status tags (`[done]`, `[escalate]`) plus the written file path.
- **Ledgers & Cleanup (`KILL_WORKER`)**:
  - Update `STATE_LEDGER` (`$PROMPTS/state.md`) at both stage launch and stage completion (re-reading from disk after context compaction), tracking C++/Rust paths, pre-existing dirty files, verified build/clippy/test commands, `babeldiff` baselines, and discovered C++ bugs.
  - Log autonomous mapping tradeoffs in `DECISION_LEDGER` (`$PROMPTS/decisions.md`).
  - Close finished stage subagents at every stage boundary (keeping `zircon-coder-k` open across Stages 3–5 and closing it at Stage 6), and remove `$PROMPTS` after delivering the consolidated final report.

## Optional Stage 6 `CLEANROOM_BARRIER` (Only When Local Commit Requested)

1. Leave verified edits uncommitted unless the user explicitly requested a local git commit. When committing, isolate Stage 0 pre-existing dirty paths, stage only the unit's intended files (`git add -- <files>`), and create a separate private `0700` clean-room directory via `mktemp -d /tmp/codex-cleanroom.XXXXXX` (`<CLEANROOM_DIR>`, strictly outside `$PROMPTS/`).
2. If workspace sandboxing blocks out-of-tree reads, copy `cleanroom-commit.md` into `<CLEANROOM_DIR>/cleanroom-commit.md` (never into `$PROMPTS/`).
3. Spawn `Commit-Reviewer` (`model = "gpt-6-astra"`, `reasoning_effort = "xhigh"`, `fork_context = false`) first. Only **after** `Commit-Reviewer` returns cleanly and `<CLEANROOM_DIR>/report.md` exists, spawn `Commit-Writer` (`model = "gpt-6-astra"`, `reasoning_effort = "xhigh"`, `fork_context = false`) as a fresh cold-start subagent—never spawning `Commit-Writer` on `[hygiene-fix-needed]`.
4. Enforce strict input isolation:
   - Pass **`Commit-Reviewer`** **ONLY**: a fresh sanitized Problem Statement, `<REPO_ROOT>`, pre-existing Step 0 dirty paths to ignore, the `cleanroom-commit.md` path, and `<CLEANROOM_DIR>/report.md`.
   - Pass **`Commit-Writer`** **ONLY**: `<CLEANROOM_DIR>/report.md`, `<REPO_ROOT>`, and the `cleanroom-commit.md` path.
   - **Strictly forbid** both Stage 6 workers from reading `$PROMPTS/`, `state.md`, `decisions.md`, `plan.md`, `fixes.md`, `babeldiff-*.txt`, or running `git reflog`, `git log --all`, or `git branch -a`.
5. **On `[hygiene-fix-needed]`**: Close `Commit-Reviewer`, remove `<CLEANROOM_DIR>`, fix and re-stage the changes, create a fresh `/tmp/codex-cleanroom.XXXXXX`, and cold-start a fresh `Commit-Reviewer` pass.
6. Once `Commit-Writer` confirms the local commit (`[done]`), close both Stage 6 subagents and remove `<CLEANROOM_DIR>` immediately before advancing or delivering Final Handoff.
