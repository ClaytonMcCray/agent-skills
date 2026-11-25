---
name: review
description: >-
  Run a right-sized, diff-scoped multi-agent code review over Codex CLI
  subagents, pinning GPT-6 Astra (xhigh) for correctness, conditional C++/unsafe
  Rust safety, and Small-diff sole adversarial review, and GPT-6 Sol (xhigh) for
  Standard/Large-diff adversarial review and optional large-diff verification,
  with optional cross-family Claude critique via claude-review. Use when the
  user invokes /review or asks to review code, diffs, commits, or branches.
---

# Right-Sized Multi-Agent Code Review (`/review` — Codex Adapter)

Deliver **high-signal, diff-scoped bug discovery with proportional fixes and disciplined quota/latency usage**. Follow [algorithm.md](./core/algorithm.md) end-to-end using the concrete Codex bindings below.

## Paths & Context Collector (`<SKILL_DIR>`)

- Resolve `<SKILL_DIR>` dynamically by canonicalizing this loaded `SKILL.md` through symlinks first (`realpath` / `Path(skill_md).resolve().parent`), then take 3 parent levels up for `<SKILLS_ROOT>` (because `~/.codex/skills/review` is a per-skill symlink into the repo) and dereference `./core` to canonical **`<CORE_REVIEW_DIR> = <SKILLS_ROOT>/core/review`**. Pass canonical dereferenced paths to spawned subagents (never `<SKILL_DIR>/core/../...`).
- Run the read-only context collector first:
  ```bash
  python3 "<CORE_REVIEW_DIR>/scripts/collect_review_context.py" [--repo <worktree>] [scope-args]
  ```
  ([collect_review_context.py](./core/scripts/collect_review_context.py))
- Pass each spawned subagent its canonical absolute playbook path (`<CORE_REVIEW_DIR>/playbooks/<name>.md`) as its mandatory first read:
  - Specialists: [correctness-style.md](./core/playbooks/correctness-style.md), [security-adversarial.md](./core/playbooks/security-adversarial.md), conditional [safety-cpp-rust.md](./core/playbooks/safety-cpp-rust.md)
  - Verification & Scope Filter (inline by `<CALLER>` by default): [verifier.md](./core/playbooks/verifier.md)
  - Optional cross-family second opinion: [../claude-review/SKILL.md](../claude-review/SKILL.md)

## `MODEL_MAP`, Escalation & Quota Fallback

| Tier | Codex Subagent Parameters | Bound `algorithm.md` Personas |
| :--- | :--- | :--- |
| **Judgment / Review** | `model = "gpt-6-astra"`, `reasoning_effort = "xhigh"`, `fork_context = false` | `Correctness` (`cor`), conditional `Safety` (`saf`), **and** `Adversarial` (`adv`) on **Small diffs** (`<= 3` files & `<= 120` lines, sole spawned specialist) |
| **Volume / Adversarial** | `model = "gpt-6-sol"`, `reasoning_effort = "xhigh"`, `fork_context = false` | `Adversarial` (`adv`) on **Standard & Large diffs** (paired in parallel with `gpt-6-astra` `Correctness`), plus optional large-diff `Verifier` (`ver`) |
| **Optional Cross-Family** | [../claude-review/SKILL.md](../claude-review/SKILL.md) (`--mode review --model fable --effort high` via `gpt-6-sol` `low` relay) | Independent Claude critique when explicitly requested or for high-risk changes whenever `claude --safe-mode auth status` (with `ANTHROPIC_*`/`CLAUDE_CODE_USE_*` unset) reports `authMethod == "claude.ai"` (fall back to Codex specialists if absent or on `auth_error`, `claude_error`, or `timeout`) |

- **Cross-model review guarantee**: Pinning `Correctness`, `Safety`, and Small-diff sole `Adversarial` to `gpt-6-astra` guarantees every diff reviewed by `/review` (including Small diffs written by a `gpt-6-sol` `Coder`) receives independent `gpt-6-astra` eyes.
- **`claude-review` relay contract**: Because `claude_review.py` runs Claude under `--cwd "<WORKTREE_PATH>" --restricted` (the target repo or worktree under review; no reads outside `--cwd`, no writes), `<LEAD>` passes `<WORKTREE_PATH>` and `<SUMMARY_DIR>` paths to the `gpt-6-sol` (`low`) relay; the **relay** reads and embeds `<SUMMARY_DIR>/summary.txt`, `changed_files.txt`, and `diff.patch` into `--prompt-file` (stating explicitly that external `/tmp` and `<CORE_REVIEW_DIR>` context is already embedded so Claude must not `Read` outside `--cwd`) before invoking `claude_review.py --cwd "<WORKTREE_PATH>"`, returning `status`, `resumable`, `findings_omitted`, `report_path`/`review_path`, and compact findings verbatim. Inspect `review_path` (`review.json`) whenever `findings_omitted > 0` before treating the finding set as complete, and pass `--resume <session_file>` on follow-ups only when the prior turn returned `resumable == true` (otherwise start fresh).
- **Capability escalation**: Never re-run a failed or thin `gpt-6-sol` specialist on the same brief at the same effort—close it and spawn a fresh `gpt-6-astra` (`reasoning_effort = "xhigh"`, `fork_context = false`) specialist with the original brief plus a concise failure summary. On failed `gpt-6-astra` specialists, narrow the brief to the disputed sites or invoke `claude-review` for independent diagnosis.
- **Quota fallback (`SWITCH_MODEL`)**: On quota exhaustion, inform the user (Standalone Mode) and cross-pin affected workers `gpt-6-astra <-> gpt-6-sol` (`reasoning_effort = "xhigh"`), logging the substitution in the final report. **Never** downgrade core reviewers to `low` or mini tiers (use `gpt-6-sol` `low` strictly for narrow lookups or `claude-review` transport relay).

## Invocation Modes (Standalone Lead vs. Embedded Reviewer)

1. **Standalone Lead Mode (Default — user invoked `/review` directly)**:
   - Run `collect_review_context.py` (re-running with `--branch-and-working` if `summary.txt` shows both working-tree edits and commits ahead of base, unless unstaged-only was requested).
   - Size Phase 1 workers strictly from `summary.txt` (`CHANGED_FILES_COUNT` and `(+A / -R)`):
     - **Small (`<= 3` files & `<= 120` lines)**: `1` `gpt-6-astra` `adv` worker (also reading `safety-cpp-rust.md` if triggered); `<LEAD>` runs `correctness-style.md` + `verifier.md` inline.
     - **Standard (default)**: `gpt-6-astra` `cor` + `gpt-6-sol` `adv` + conditional `gpt-6-astra` `saf` (`2–3` workers); `<LEAD>` verifies inline.
     - **Large (`> 20` files or `> 2,500` lines)**: shard `cor` (and `saf` if C++/Rust-heavy) across 2–3 subsystem slices (`<= 6` workers total), plus optional `gpt-6-sol` `ver` on complex cross-shard proofs.
   - Spawn Phase 1 subagents in parallel with `fork_context = false`, verify claims inline against [verifier.md](./core/playbooks/verifier.md), close all subagents, remove `<SUMMARY_DIR>` and `<REPORT_DIR>`, and present the concise review report.
2. **Embedded Reviewer Mode (Paired or Seam Reviewer inside `/dev-swarm`)**:
   - Run `collect_review_context.py` with `--repo "<WORKTREE_PATH>" --working [-- <slice-files>]` (adding `-- <slice-files>` on in-place runs with unrelated dirty paths, or `--repo "<REPO_ROOT>" --staged` on seam gates).
   - **Nested subagent fallback**: If `Reviewer` cannot spawn nested subagents directly, `<LEAD>` spawns the Pass 1 specialists (`fork_context = false`) in parallel writing into a dedicated per-pass `<REPORT_DIR>` (`$PROMPTS/rev-<k>[-<j>]-p1` or `mktemp -d /tmp/codex-review.XXXXXX`) and passes those report refs to `Reviewer` (`gpt-6-astra`) for inline `verifier.md` filtering (plus `correctness-style.md` inline on Small diffs), meta-comment audit, and test execution.
   - **Pass 1 Gate -> Pass 2+ Direct Delta Verification**:
     - *Pass 1*: Run the right-sized multi-agent `/review` gate once, verify zero leaked meta-comments, run build/lint/tests once, close all Pass 1 helpers, and approve immediately if zero in-scope `[MUST-FIX]` issues exist.
     - *Pass 2+ (`[fixed]`)*: Verify the fix delta and tests directly without re-spawning the Pass 1 ensemble (spawn at most 1 targeted `saf` [`gpt-6-astra`] or `adv` [`gpt-6-astra` on Small diffs / `gpt-6-sol` otherwise] helper only if the fix rewrote `> 150` lines or added new `unsafe`/C++/FFI/crypto code).
     - *Scope discipline*: Never demand fixes to untouched pre-existing code or raise new `MEDIUM`/`LOW`/`STYLE` demands on unchanged Pass 1 code.

## `SPAWN_WORKER`, `WAIT_FOR_WORKERS`, `HANDOFF`, `READ_ARTIFACT` & Cleanup

- **Spawn & Wait (`SPAWN_WORKER` / `WAIT_FOR_WORKERS`)**: Spawn every read-only specialist with **`fork_context = false`**, explicit `model`, `reasoning_effort = "xhigh"`, `<SUMMARY_DIR>` paths, and canonical playbook path, launching all Phase 1 specialists in parallel and awaiting completion. Enforce strict read-only execution (specialists never edit files or run competing test builds).
- **Handoff & Artifacts (`HANDOFF` / `READ_ARTIFACT`)**: Allocate a dedicated private `0700` per-pass `<REPORT_DIR>` via `mktemp -d /tmp/codex-review.XXXXXX` (or a dedicated `$PROMPTS/rev-<k>[-<j>]-p<pass>` subdirectory in Embedded Mode—never alias `<REPORT_DIR>` to bare `$PROMPTS`). Instruct each specialist to write `<REPORT_DIR>/<role>.md` and return `[done]` with the path. On Large diffs with an optional `gpt-6-sol` `Verifier` (`ver`), pass Phase 1 file paths rather than pasting report bodies.
- **Cleanup (`KILL_WORKER`)**: Close every spawned subagent on completion and run `rm -rf "<SUMMARY_DIR>" "<REPORT_DIR>"` (removing only that pass's dedicated `<REPORT_DIR>`, never `$PROMPTS`).
