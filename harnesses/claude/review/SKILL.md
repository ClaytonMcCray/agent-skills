---
name: review
description: >-
  Run a right-sized, diff-scoped multi-agent code review over Claude Code's
  native Agent subagent tool, pinning Fable (xhigh) for correctness, conditional
  C++/unsafe Rust safety, and Small-diff sole adversarial review, and Opus
  (xhigh) for Standard/Large-diff adversarial review and optional large-diff
  verification. Use when the user invokes /review or asks to review code,
  changes, diffs, commits, branches, or pull requests.
---

# Right-Sized Multi-Agent Code Review (`/review` — Claude Code Adapter)

Deliver **high-signal, diff-scoped bug discovery with proportional fixes and disciplined quota/latency usage**. Follow [algorithm.md](./core/algorithm.md) end-to-end using the concrete Claude Code bindings below.

## Paths & Context Collector (`<SKILL_DIR>`)

- Resolve `<SKILL_DIR>` dynamically by canonicalizing this loaded `SKILL.md` through symlinks first (`realpath` / `Path(skill_md).resolve().parent`), then take 3 parent levels up for `<SKILLS_ROOT>` (because `~/.claude/skills/review` is a per-skill symlink into the repo) and dereference `./core` to canonical **`<CORE_REVIEW_DIR> = <SKILLS_ROOT>/core/review`**. Pass canonical dereferenced paths to spawned `Agent` workers (never `<SKILL_DIR>/core/../...`).
- Run the read-only context collector first:
  ```bash
  python3 "<CORE_REVIEW_DIR>/scripts/collect_review_context.py" [--repo <worktree>] [scope-args]
  ```
  ([collect_review_context.py](./core/scripts/collect_review_context.py))
- Pass each spawned `Agent` worker its canonical absolute playbook path (`<CORE_REVIEW_DIR>/playbooks/<name>.md`) as its mandatory first read:
  - Specialists: [correctness-style.md](./core/playbooks/correctness-style.md), [security-adversarial.md](./core/playbooks/security-adversarial.md), conditional [safety-cpp-rust.md](./core/playbooks/safety-cpp-rust.md)
  - Verification & Scope Filter (inline by `<CALLER>` by default): [verifier.md](./core/playbooks/verifier.md)

## `MODEL_MAP`, Escalation & Quota Fallback

| Tier | `Agent` Parameters | Bound `algorithm.md` Personas |
| :--- | :--- | :--- |
| **Judgment / Review** | `model: "fable"` (Fable 5.1), `effort: "xhigh"` | `Correctness` (`cor`), conditional `Safety` (`saf`), **and** `Adversarial` (`adv`) on **Small diffs** (`<= 3` files & `<= 120` lines, sole spawned specialist) |
| **Volume / Adversarial** | `model: "opus"` (Opus 5.5), `effort: "xhigh"` | `Adversarial` (`adv`) on **Standard & Large diffs** (paired in parallel with `fable` `Correctness`), plus optional large-diff `Verifier` (`ver`) |

- **Cross-model review guarantee**: Pinning `Correctness`, `Safety`, and Small-diff sole `Adversarial` to `fable` guarantees every diff reviewed by `/review` (including Small diffs written by an `opus` `Coder`) receives independent `fable` eyes.
- **Capability escalation**: Never re-run a failed or thin `opus` specialist on the same brief at the same effort—re-dispatch it fresh on `model: "fable"`, `effort: "xhigh"` with the original brief plus a concise failure summary. Retry a failed `fable` specialist with `effort: "max"` or narrowed to the disputed files. Route narrow symbol/caller lookups to Claude Code's built-in `Explore` subagent (`subagent_type: "Explore"`).
- **Quota fallback (`SWITCH_MODEL`)**: On quota exhaustion or capacity limits, inform the user (Standalone Mode) and cross-pin affected workers `fable <-> opus` (`effort: "xhigh"`), logging the substitution in the final report. **Never** downgrade reviewers to `haiku`.

## Invocation Modes (Standalone Lead vs. Embedded Reviewer)

1. **Standalone Lead Mode (Default — user invoked `/review` directly)**:
   - Run `collect_review_context.py` (re-running with `--branch-and-working` if `summary.txt` shows both working-tree edits and commits ahead of base, unless unstaged-only was requested).
   - Size Phase 1 workers strictly from `summary.txt` (`CHANGED_FILES_COUNT` and `(+A / -R)`):
     - **Small (`<= 3` files & `<= 120` lines)**: `1` `fable` `adv` worker (also reading `safety-cpp-rust.md` if triggered); `<LEAD>` runs `correctness-style.md` + `verifier.md` inline.
     - **Standard (default)**: `fable` `cor` + `opus` `adv` + conditional `fable` `saf` (`2–3` workers); `<LEAD>` verifies inline.
     - **Large (`> 20` files or `> 2,500` lines)**: shard `cor` (and `saf` if C++/Rust-heavy) across 2–3 subsystem slices (`<= 6` workers total), plus optional `opus` `ver` on complex cross-shard proofs.
   - Dispatch all Phase 1 `Agent` workers in parallel in a single assistant message, verify every candidate finding against actual source files per [verifier.md](./core/playbooks/verifier.md), remove `<SUMMARY_DIR>` (and any temp report dir), and present the concise review report.
2. **Embedded Reviewer Mode (Paired or Seam Reviewer inside `/dev-swarm`)**:
   - Run `collect_review_context.py` with `--repo "<WORKTREE_PATH>" --working [-- <slice-files>]` (adding `-- <slice-files>` on in-place runs with unrelated dirty paths, or `--repo "<REPO_ROOT>" --staged` on seam gates).
   - **Nested `Agent` fallback**: If `Reviewer` has `Agent` tool access, it spawns the Pass 1 specialist `Agent`s directly; if subagents cannot spawn nested `Agent`s under active tool permissions, `<LEAD>` spawns the Pass 1 specialist `Agent`s in parallel in one message batch writing into a dedicated per-pass `<REPORT_DIR>` (`$PROMPTS/rev-<k>[-<j>]-p1` or `mktemp -d /tmp/claude-review.XXXXXX`) and passes those report refs to `Reviewer` (`fable`) to run inline `verifier.md` filtering (plus `correctness-style.md` inline on Small diffs), meta-comment audit, and test execution.
   - **Pass 1 Gate -> Pass 2+ Direct Delta Verification**:
     - *Pass 1*: Run the right-sized multi-agent `/review` gate once, verify zero leaked meta-comments, run build/lint/tests once, and approve immediately if zero in-scope `[MUST-FIX]` issues exist.
     - *Pass 2+ (`[fixed]`)*: Verify the fix delta and tests directly without re-spawning the Pass 1 specialist ensemble (spawn at most 1 targeted `saf` [`fable`] or `adv` [`fable` on Small diffs / `opus` otherwise] helper only if the fix rewrote `> 150` lines or added new `unsafe`/C++/FFI/crypto code).
     - *Scope discipline*: Never demand fixes to untouched pre-existing code or raise new `MEDIUM`/`LOW`/`STYLE` demands on unchanged Pass 1 code.

## `SPAWN_WORKER`, `WAIT_FOR_WORKERS`, `HANDOFF`, `READ_ARTIFACT` & Cleanup

- **Spawn & Wait (`SPAWN_WORKER` / `WAIT_FOR_WORKERS`)**: Dispatch all independent Phase 1 specialists as parallel cold-start `Agent` tool calls (`model`, `effort`, concise `description`, read-only brief citing `<SUMMARY_DIR>` and canonical playbook path) in a single message batch, and wait for all calls to return. Enforce strict read-only execution (specialists never edit files or run competing test builds).
- **Handoff & Artifacts (`HANDOFF` / `READ_ARTIFACT`)**: Allocate a dedicated private `0700` per-pass `<REPORT_DIR>` via `mktemp -d /tmp/claude-review.XXXXXX` (or a dedicated `$PROMPTS/rev-<k>[-<j>]-p<pass>` subdirectory in Embedded Mode—never alias `<REPORT_DIR>` to bare `$PROMPTS`). Have each specialist write `<REPORT_DIR>/<role>.md` and return `[done]` with the file path and finding count. On Large diffs with an optional `opus` `Verifier` (`ver`), pass Phase 1 report file paths rather than pasting full reports into its prompt.
- **Cleanup (`KILL_WORKER`)**: Foreground `Agent` calls exit automatically on return (stop any background agent before presenting results). Always run `rm -rf "<SUMMARY_DIR>" "<REPORT_DIR>"` during Step 4 cleanup (removing only that pass's dedicated `<REPORT_DIR>`, never `$PROMPTS`).
