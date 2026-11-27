---
name: dev-swarm-zircon
description: >-
  Ports a unit of Zircon C++ to Rust as a fixed-role swarm—line by line, bug
  for bug, comment for comment, unsafe-heavy by design. Combines dev-swarm's
  research, planning, and parallel cross-family critic roles with specialized
  Zircon Coder, Parity Reviewer, and Safety Reviewer personas, the in-tree
  porting rubric, dispatcher/fbl guides, and babeldiff. Use for any Phase 1
  Zircon kernel or library C++-to-Rust port; do not use for idiomatic Rust
  work or the later cleanup phase.
---

# Zircon C++-to-Rust Porting Swarm (`/dev-swarm-zircon` — Antigravity Adapter)

You (the main thread) are **`<LEAD> = coordinator`**. Read [doctrine.md](./core/doctrine.md) first, then follow [algorithm.md](./core/algorithm.md) end-to-end using the concrete Antigravity bindings below.

> **Explicit Tool Authorization**: This skill explicitly authorizes and instructs you to call `swarm` (`models`, `title`, `add`, `status`, `model`, `kill`, and `channel` if available) and board tools (`post_message`, `read_board`, `swarm_wait`).

## Paths, `$PROMPTS`, `run_babeldiff.py` & Playbooks (`<SKILL_DIR>`)

- Resolve `<SKILL_DIR>` dynamically from this loaded `SKILL.md` and normalize `<SKILLS_ROOT>` (the directory 3 levels above `<SKILL_DIR>`), dereferencing `./core` to canonical paths **`<CORE_ZIRCON_DIR> = <SKILLS_ROOT>/core/dev-swarm-zircon`** and **`<CORE_DEV_SWARM_DIR> = <SKILLS_ROOT>/core/dev-swarm`** (shared bindings in [../dev-swarm/SKILL.md](../dev-swarm/SKILL.md)). Pass canonical paths in worker briefs (never `<SKILL_DIR>/core/../...`).
- **Session brief/artifact directory (`$PROMPTS`)**: Define **`$PROMPTS = <appDataDir>/brain/<conversation-id>/scratch/swarm`** unconditionally at Stage 0 for `state.md`, `plan.md`, `impl.md`, `fixes.md`, and saved `babeldiff` artifacts across Stages 0–5.
- **Stage 0 workspace sandbox staging**: If the active Fuchsia workspace sandbox blocks workers from reading outside the Fuchsia tree, copy [doctrine.md](./core/doctrine.md), [run_babeldiff.py](./core/scripts/run_babeldiff.py), [researcher.md](../dev-swarm/core/personas/researcher.md), and the three Zircon playbooks ([zircon-coder.md](./core/personas/zircon-coder.md), [zircon-parity-reviewer.md](./core/personas/zircon-parity-reviewer.md), [zircon-safety-reviewer.md](./core/personas/zircon-safety-reviewer.md)) into `$PROMPTS/` once at Stage 0 and reference those staged paths in Stage 1–5 worker briefs.
- **Stage 6 clean-room staging (sandboxed workspaces only)**: When optional Stage 6 runs under out-of-tree read restrictions, copy [cleanroom-commit.md](../dev-swarm/core/personas/cleanroom-commit.md) into a separate private `0700` directory created via `mktemp -d /tmp/agy-cleanroom.XXXXXX` (**never** into `$PROMPTS/` or `<artifactDir>`, which `CLEANROOM_BARRIER` forbids Stage 6 workers from reading), pass that path to `commit-rev-k` and `commit-writer-k`, and remove the temporary directory after Stage 6 finishes.
- Run `babeldiff` via `python3 "<CORE_ZIRCON_DIR>/scripts/run_babeldiff.py" --allow-issues ...` (using `--update` once per session; only initial clone or `--update` network pull needs network access outside an offline sandbox).

## `MODEL_MAP` & Fallback Bindings

Resolve exact model IDs once at Step 0 via `swarm{action: "models", all: true}`:

| Tier Variable | Match Rule (Case-Insensitive) | Bound `algorithm.md` Personas |
| :--- | :--- | :--- |
| **`CARBON_HIGH`** | `Carbon` + `High` (e.g. `Gemini 4 Carbon (High)`) | `Researcher` (`res-k-1` broad Rust port conventions + full C++ inventory), `Planner`, `Critic-1` (`critic-carbon-k`), `Zircon-Coder-Escalation`, `Zircon-Parity-Reviewer`, `Zircon-Safety-Reviewer`, and optional Stage 6 `Commit-Reviewer` / `Commit-Writer` |
| **`NEON_XHIGH`** | `Neon` + `xhigh` / `XHigh` / `Extra High` | `Researcher` (`res-k-2` narrow exhaustive C++ function/comment/lock/UB inventory), `Zircon-Coder` (default writer) |
| **`OPUS_HIGH`** | `Opus` + `5.5` + `High` (or closest high-thinking `Opus`) | Parallel `Critic-2` (`critic-opus-k`, spawned alongside `critic-carbon-k` when quota `> 0%`) |

- **Cross-model review guarantee**: Pinning `Zircon-Parity-Reviewer`, `Zircon-Safety-Reviewer`, and optional Stage 6 `Commit-Reviewer` to `CARBON_HIGH` prevents `NEON_XHIGH` code from ever being audited solely by `NEON_XHIGH`.
- **Coder & inventory escalation**: After a thin or failed attempt by a `NEON_XHIGH` worker (`res-k-2` or `Zircon-Coder`), `kill` it and spawn a fresh escalation worker pinned to `CARBON_HIGH` with the same brief plus what failed.
- **Safety escalation**: On safety disagreement, data races, raw-pointer aliasing, or FFI ownership transfer, spawn a fresh `Zircon-Safety-Reviewer` on `CARBON_HIGH` (or higher Carbon effort tier if listed by `models`) narrowed to the disputed sites.
- **Quota fallback (`SWITCH_MODEL`)**: Move waiting workers in place via `swarm{action: "model", name: "<worker>", model: "<other_pin>"}` across `OPUS_HIGH <-> NEON_XHIGH <-> CARBON_HIGH` and record substitutions in the final report. **Never** downgrade core roles to `flash` or `flash_lite`.
- **Disabled `swarm` plugin**: Tell the user `/dev-swarm-zircon` requires [swarm](customization://swarm) for per-role frontier pinning and ask whether to enable it first.

## Per-Unit Naming (`-k`), Lean Roster & Board Mechanics

Killed `swarm` names cannot be reused in the same conversation, so suffix workers by unit $k$ and review round $r$ (all names `<= 24` chars):
- Stage 1: `res-k-1` (`CARBON_HIGH`) and `res-k-2` (`NEON_XHIGH`) in parallel for cross-model inventory verification.
- Stage 2: optional `planner-k` (`CARBON_HIGH`), `critic-carbon-k` (`CARBON_HIGH`), parallel `critic-opus-k` (`OPUS_HIGH` when quota `> 0%`).
- Stage 3: `zircon-coder-k` (`NEON_XHIGH`; escalation `zircon-coder-k-cb` on `CARBON_HIGH`).
- Stage 4/5: `z-parity-k-p<r>`, `z-safety-k-p<r>` (safety escalation `z-safety-k-p<r>-esc`).
- Stage 6 (only when a local git commit is requested): `commit-rev-k` (`CARBON_HIGH`) and `commit-writer-k` (`CARBON_HIGH`) (on hygiene retry pass `r`: `commit-rev-k-p<r>`, `commit-writer-k-p<r>`) following Stage 4's `CLEANROOM_BARRIER` in [../dev-swarm/SKILL.md](../dev-swarm/SKILL.md) (forbidding reads of `$PROMPTS/`, `<artifactDir>`, or pre-Stage-6 board history).
- **Lean roster (no unsolicited helpers)**: Do **not** spawn unsolicited `monitor` or `narrator` workers.

### Lifecycle, `HANDOFF` & Ledgers

- **Unit 1 init**: In the first `swarm` batch, call `swarm{action: "title", title: "Zircon Port: <short unit>"}` and spawn Stage 1's `res-1-1` and `res-1-2`, then end the turn so board tools attach (output `open_board` on its own line if returned).
- **Spawn (`SPAWN_WORKER`)**: `swarm{action: "add", name: "<role>", model: "<pin>", workspace: "inherit", brief: "<brief>"}` so `Zircon-Coder` edits the Fuchsia git checkout or `.jiri_root/worktrees/<name>` in-place without losing `out/` build caches.
- **Wait & cleanup (`WAIT_FOR_WORKERS` / `KILL_WORKER`)**: Loop `swarm_wait{since: <last_seq>}` + `read_board{since: <last_seq>}`; `swarm{action: "kill", name: "<worker>"}` completed stage workers at each boundary (keep the active `Zircon-Coder` in `waiting` state across Stage 4 -> Stage 5 fixes, then `kill` it at Stage 6).
- **Handoff (`HANDOFF` / `READ_ARTIFACT`)**: Post via `post_message` starting with `@<recipient> [status] ...` and cite `#seq` numbers for `read_board{seqs: [...]}`. **Spurious `@`-wakeup guard**: never write literal `@` immediately before `coordinator` in peer-to-peer briefs or drafts; workers delivering final reports or escalations to the lead start with `@coordinator [done]` or `@coordinator [escalate]` and mention no other agent.
- **`STATE_LEDGER`**: Maintain `STATE_LEDGER` at `$PROMPTS/state.md` and update it across stage boundaries.
- **`DECISION_LEDGER`**: Maintain `DECISION_LEDGER` at `<artifactDir>/dev_swarm_zircon_decisions.md` for final handoff review.
