---
name: review
description: >-
  Run a right-sized, diff-scoped multi-agent code review using specialist
  reviewers pinned across Carbon (High) and Neon (xhigh) for correctness,
  caller contracts, DRY/code health, calibrated security/adversarial edge cases,
  and conditional C++/unsafe Rust soundness. Use when the user invokes /review
  or asks to review code, changes, diffs, commits, branches, or pull requests.
---

# Right-Sized Multi-Agent Code Review (`/review` — Antigravity Adapter)

Deliver **high-signal, diff-scoped bug discovery with proportional fixes and disciplined quota/latency usage**. Follow [algorithm.md](./core/algorithm.md) end-to-end using the concrete Antigravity bindings below.

> **Explicit Tool Authorization**: This skill explicitly instructs you to call the `swarm` tool (`models`, `title`, `add`, `status`, `model`, `kill`) and board tools (`post_message`, `read_board`, `swarm_wait`) so specialist workers are pinned across **Carbon (High)** and **Neon (xhigh)**.

## Paths & Context Collector (`<SKILL_DIR>`)

- Resolve `<SKILL_DIR>` dynamically from the directory containing this loaded `SKILL.md` (`<CORE_REVIEW_DIR> = <SKILL_DIR>/core`).
- Run the collector in parallel with model resolution:
  ```bash
  python3 "<SKILL_DIR>/core/scripts/collect_review_context.py" [--repo <worktree>] [scope-args]
  ```
  *(Sandbox fallback: if executing outside the workspace is blocked, run `mktemp -d /tmp/agy-collect.XXXXXX` to obtain a private `0700` `<TMP_COLLECT>` directory, read [collect_review_context.py](./core/scripts/collect_review_context.py) in paginated `read_file` slices—`StartLine=1, EndLine=800`, `801..1600`, `1601..2400` until EOF—writing sequentially with `write_file(..., Append=True)` into `<TMP_COLLECT>/collect_review_context.py`, run `python3 "<TMP_COLLECT>/collect_review_context.py" [--repo <worktree>] [scope-args]`, and remove `<TMP_COLLECT>` during Step 4 cleanup.)*
- Pass each spawned worker its resolved absolute playbook path (`<SKILL_DIR>/core/playbooks/<name>.md`) as its mandatory first `read_file` action:
  - Specialists: [correctness-style.md](./core/playbooks/correctness-style.md), [security-adversarial.md](./core/playbooks/security-adversarial.md), conditional [safety-cpp-rust.md](./core/playbooks/safety-cpp-rust.md)
  - Verification & Scope Filter (inline by `<CALLER>` by default): [verifier.md](./core/playbooks/verifier.md)

## `MODEL_MAP` & Fallback Bindings

Call `swarm{action: "models", all: true}` to resolve exact frontier model names:

| Tier Variable | Match Rule (Case-Insensitive) | Bound `algorithm.md` Personas |
| :--- | :--- | :--- |
| **`CARBON_HIGH`** | `Carbon` + `High` (e.g. `Gemini 4 Carbon (High)`) | `Correctness` (`cor`) |
| **`NEON_XHIGH`** | `Neon` + `xhigh` / `XHigh` / `Extra High` | `Adversarial` (`adv`), conditional `Safety` (`saf`), optional large-diff `Verifier` (`ver`) |

- **Quota fallback (`SWITCH_MODEL`)**: If one frontier tier is completely out of quota (and not covered by AI credits), inform the user (Standalone Mode) and cross-pin affected workers `CARBON_HIGH <-> NEON_XHIGH`. On a mid-run quota hit, switch in place via `swarm{action: "model", name: "<worker>", model: "<other_pin>"}` rather than killing. **Never** silently downgrade reviewers to `flash` or `flash_lite`.
- **Disabled `swarm` plugin**: Tell the user per-agent pinning requires [swarm](customization://swarm) and ask whether to enable it first or fall back to `invoke_subagent(..., Model="pro")`.

## Invocation Modes (Standalone Coordinator vs. Embedded Swarm Worker)

1. **Standalone Coordinator Mode (Default — user invoked `/review` directly)**:
   - Call `swarm{action: "title", title: "Code Review: <concise target>"}` (if an unrelated swarm is active, prefix worker names `rev-` and use `#review` channel if supported).
   - Set `<CALLER>` to `@coordinator` so every helper posts `@coordinator [done]`.
   - Execute the diff-scale tier in [algorithm.md](./core/algorithm.md) (`1` worker on Small diffs `<= 3` files & `<= 120` lines, `2–3` workers on Standard diffs, `<= 6` sharded workers on Large diffs `> 20` files or `> 2,500` lines), verify claims inline against [verifier.md](./core/playbooks/verifier.md), post `[done] Review complete` (no `@`) if a monitor is active, kill all helpers, remove `<SUMMARY_DIR>` (and any `<TMP_COLLECT>`), and present the report (writing to `<artifactDir>` only if `> 8` verified findings exist).
2. **Embedded Swarm Worker Mode (e.g. `reviewer-1` or `rev-1-seam` inside `/dev-swarm`)**:
   - **Never** call `swarm{action: "title"}` (coordinator-only).
   - Pass `--repo "<WORKTREE_PATH>" --working` (or `--repo "<REPO_ROOT>" --staged` on seam gates) to the collector.
   - Namespace helpers `<prefix>-p<pass>-{cor,adv,saf,ver}` (`<= 24` chars total, e.g. `r1-1-p1-adv` or `r1s-p1-cor`; place in `#rev-<your-member-name>` channel if supported).
   - Set `<CALLER>` to `@<your-member-name>`. **Spurious wakeup guard**: never write literal `@` immediately before `coordinator` in helper `brief`s or intermediate posts (say `"the swarm coordinator"` in prose).
   - **Pass 1 Gate -> Pass 2+ Direct Delta Verification**:
     - *Pass 1*: Run the right-sized multi-agent `/review` gate once, verify zero leaked meta-comments, run build/lint/tests once yourself, and `kill` all helpers immediately. Approve right away in Pass 1 if zero in-scope `[MUST-FIX]` issues exist.
     - *Pass 2+ (`[fixed]`)*: Verify the fix delta and tests **directly yourself without re-spawning helpers** (spawning at most 1 targeted `NEON_XHIGH` `saf` or `adv` helper only if a fix rewrote `> 150` lines or added new `unsafe`/C++/FFI/crypto code).
     - *Scope discipline*: Never demand out-of-scope fixes to untouched pre-existing code or move goalposts onto unchanged Pass 1 code.

## `SPAWN_WORKER`, `HANDOFF`, `READ_ARTIFACT` & Cleanup Bindings

- **Spawn (`SPAWN_WORKER`)**: Call `swarm{action: "add", name: "<role>", model: "<pin>", workspace: "inherit", brief: "<brief>"}`. Only on Large diffs (`> 20` changed source files or `> 2,500` lines across subsystems), shard `cor` (and `saf` if C++/Rust-heavy) into 2–3 slices via `tasks: ["Subsystem A: <files>", "Subsystem B: <files>"]` (`<= 6` workers total).
- **Handoff (`HANDOFF`)**: Deliver finished reports via `post_message` starting with `<CALLER> [done] <report>` (or `<peer> [question] ...` on optional large-diff verification).
- **First `swarm add` turn rule**: End your turn immediately after the first `swarm add` batch in a conversation so `post_message`/`read_board`/`swarm_wait` unlock next turn (in Standalone Mode, output a brief status note listing spawned roles, model pins, and `open_board` on its own line if returned).
- **Wait & Inline Verification (`WAIT_FOR_WORKERS` & `READ_ARTIFACT`)**: Loop `swarm_wait{since: <last_seq>}` + `read_board{since: <last_seq>}` until spawned helpers post `[done]`. Verify claims directly inline via `read_file` against [verifier.md](./core/playbooks/verifier.md) (if spawning an optional `ver` worker on a `> 2,500`-line sharded diff, pass Phase 1 `#seq` numbers for `read_board{seqs: [...]}`—never copy-pasting Phase 1 reports).
- **Cleanup (`KILL_WORKER`)**: Call `swarm{action: "kill", name: "<worker>"}` for every helper you spawned and run `rm -rf "<SUMMARY_DIR>"` (plus any `<TMP_COLLECT>` dir).
