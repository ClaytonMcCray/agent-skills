---
name: review
description: >-
  Run an uncompromising, thorough multi-agent code review using a swarm of
  specialist reviewers pinned across Carbon (High) and Neon (xhigh): security,
  adversarial red-teaming, memory/concurrency safety (with deep C++ and unsafe
  Rust soundness analysis), functional correctness, style/de-duplication (DRY),
  and cross-model claim verification. Use when the user invokes /review or asks
  to review code, changes, diffs, commits, branches, or pull requests.
---

# Deep Multi-Agent Code Review (`/review` — Antigravity Adapter)

Prioritize **depth, soundness, and concrete bug discovery over quota preservation**. Follow [algorithm.md](./core/algorithm.md) end-to-end using the concrete Antigravity bindings below.

> **Explicit Tool Authorization**: This skill explicitly instructs you to call the `swarm` tool (`models`, `title`, `add`, `status`, `model`, `kill`) and board tools (`post_message`, `read_board`, `swarm_wait`) so each reviewer subagent is pinned to **Carbon (High)** or **Neon (xhigh)**.

## Paths & Context Collector (`<SKILL_DIR>`)

- Resolve `<SKILL_DIR>` dynamically from the directory containing this loaded `SKILL.md` (`<CORE_REVIEW_DIR> = <SKILL_DIR>/core`).
- Run the collector in parallel with model resolution:
  ```bash
  python3 "<SKILL_DIR>/core/scripts/collect_review_context.py" [--repo <worktree>] [scope-args]
  ```
  *(Sandbox fallback: if executing outside the workspace is blocked, run `mktemp -d /tmp/agy-collect.XXXXXX` to obtain a private `0700` `<TMP_COLLECT>` directory, read [collect_review_context.py](./core/scripts/collect_review_context.py) in paginated `read_file` slices—`StartLine=1, EndLine=800`, `801..1600`, `1601..2400` until EOF—writing sequentially with `write_file(..., Append=True)` into `<TMP_COLLECT>/collect_review_context.py`, run `python3 "<TMP_COLLECT>/collect_review_context.py" [--repo <worktree>] [scope-args]`, and remove `<TMP_COLLECT>` during Step 4 cleanup.)*
- Pass each worker its resolved absolute playbook path (`<SKILL_DIR>/core/playbooks/<name>.md`) as its mandatory first `read_file` action:
  - Phase 1: [security.md](./core/playbooks/security.md), [adversarial.md](./core/playbooks/adversarial.md), [safety-cpp-rust.md](./core/playbooks/safety-cpp-rust.md), [correctness.md](./core/playbooks/correctness.md), [style-dedup.md](./core/playbooks/style-dedup.md)
  - Phase 2: [verifier.md](./core/playbooks/verifier.md)

## `MODEL_MAP` & Fallback Bindings

Call `swarm{action: "models", all: true}` to resolve exact frontier model names:

| Tier Variable | Match Rule (Case-Insensitive) | Bound `algorithm.md` Personas |
| :--- | :--- | :--- |
| **`CARBON_HIGH`** | `Carbon` + `High` (e.g. `Gemini 4 Carbon (High)`) | `Security`, `Safety-Holistic`, `Correctness`, `Style` |
| **`NEON_XHIGH`** | `Neon` + `xhigh` / `XHigh` / `Extra High` | `Adversarial`, `Safety`, `Verifier` |

- **Quota fallback (`SWITCH_MODEL`)**: If one frontier tier is completely out of quota (and not covered by AI credits), inform the user (Standalone Mode) and cross-pin affected workers `CARBON_HIGH <-> NEON_XHIGH`. On a mid-run quota hit, switch in place via `swarm{action: "model", name: "<worker>", model: "<other_pin>"}` rather than killing. **Never** silently downgrade reviewers to `flash` or `flash_lite`.
- **Disabled `swarm` plugin**: Tell the user per-agent pinning requires [swarm](customization://swarm) and ask whether to enable it first or fall back to `invoke_subagent(..., Model="pro")`.

## Invocation Modes (Standalone Coordinator vs. Embedded Swarm Worker)

1. **Standalone Coordinator Mode (Default — user invoked `/review` directly)**:
   - Run the **Full Review Ensemble** (never Fast-Track standalone).
   - Call `swarm{action: "title", title: "Code Review: <concise target>"}` (if an unrelated swarm is already active, prefix worker names `rev-` and use `#review` channel if supported).
   - Set `<CALLER>` to `@coordinator` so every helper posts `@coordinator [done]`.
   - Before cleanup, post `[done] Review complete` (no `@`) if a monitor is active; kill all helpers, remove `<SUMMARY_DIR>` (and any `<TMP_COLLECT>`), and write reports with `>8` findings or complex C++/Rust proofs to `<artifactDir>` with a concise chat summary (otherwise present in chat).
2. **Embedded Swarm Worker Mode (e.g. `reviewer-1` inside `/dev-swarm`)**:
   - **Never** call `swarm{action: "title"}` (coordinator-only).
   - Pass `--repo "<WORKTREE_PATH>"` (with `--working` on uncommitted edits) to the collector.
   - Namespace every helper `<your-member-name>-p<pass>-{sec,adv,saf,safmod,cor,sty,ver}` (killed names cannot be reused; abbreviate `<your-member-name>`—e.g. `rev1s-p1-...`—if `<member>-p<pass>-safmod` or `-<k>` shards would exceed 24 chars; place in `#rev-<your-member-name>` channel if supported).
   - Set `<CALLER>` to `@<your-member-name>` so every helper posts `@<your-member-name> [done]`. **Spurious wakeup guard**: never write the literal `@` immediately before `coordinator` anywhere in embedded helper `brief`s or intermediate board posts (refer to "the swarm coordinator" in prose).
   - **Two-Speed Gate**: Optional Fast-Track pass on early mid-coding drafts (reading playbooks directly + optional 1–2 `NEON_XHIGH` `saf`/`adv` helpers), but **MUST** run the complete Full Review Ensemble before final `[APPROVED]` sign-off, verify zero leaked process/review meta-comments, run tests independently, `kill` all helpers first, and deliver the distilled `[MUST-FIX]`/`[OPTIONAL]` feedback to the paired `@<coder>` (or `[APPROVED]` upward).

## `SPAWN_WORKER`, `HANDOFF`, `READ_ARTIFACT` & Cleanup Bindings

- **Spawn (`SPAWN_WORKER`)**: Call `swarm{action: "add", name: "<role>", model: "<pin>", workspace: "inherit", brief: "<brief>"}`. For diffs `>15` changed source files or `>1,200` lines spanning subsystems, shard `correctness` (and `safety` if C++/Rust-heavy) via `tasks: ["Subsystem A: <files>", "Subsystem B: <files>", ...]`.
- **Handoff (`HANDOFF`)**: Deliver finished reports via `post_message` starting with `<CALLER> [done] <summary/report>` (or `<peer> [question] ...` only for targeted Phase 2 clarification).
- **First `swarm add` turn rule**: End your turn immediately after the first `swarm add` batch in a conversation so `post_message`/`read_board`/`swarm_wait` unlock next turn (in Standalone Mode, output a brief status note listing spawned roles, model pins, and `open_board` on its own line if returned).
- **Phase 1 wait (`WAIT_FOR_WORKERS`)**: Loop `swarm_wait{since: <last_seq>}` + `read_board{since: <last_seq>}` until every Phase 1 helper posts `[done]`.
- **Phase 2 `Verifier` (`READ_ARTIFACT` & Peer Query)**: Spawn `verifier` (or `<your-member-name>-p<pass>-ver`) on `NEON_XHIGH`, passing Phase 1 `#seq` numbers (`#4`, `#5`, ...)—**never** copy-pasting or summarizing Phase 1 reports—for `verifier` to load via `read_board{seqs: [...]}` or `read_board{since: 0}`. Instruct `verifier` to query a waiting Phase 1 peer (`@<role>` or `@<your-member-name>-p<pass>-<shortrole>`) only on genuine unresolvable code contradictions, then loop `swarm_wait{since}` + `read_board{since}` until `verifier` posts `[done]`.
- **Cleanup (`KILL_WORKER`)**: Call `swarm{action: "kill", name: "<worker>"}` for every helper you spawned and run `rm -rf "<SUMMARY_DIR>"` (plus any `<TMP_COLLECT>` dir).
