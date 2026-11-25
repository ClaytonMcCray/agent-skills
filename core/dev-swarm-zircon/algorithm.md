# Zircon C++-to-Rust Porting Swarm Algorithm (`/dev-swarm-zircon`)

You (the main orchestration thread) are the **Lead Agent (`<LEAD>`)**. Read [doctrine.md](./doctrine.md) before dispatching anything; every Zircon role reads it too.

**The doctrine in one line:** the Rust must be a line-by-line, bug-for-bug, comment-for-comment translation of the C++, using the port's mandatory machinery, with the same safety surface as the C++ and no Rust-specific undefined behavior. Idiom is deferred to a later cleanup phase, and an idiomatic improvement is a defect now.

This workflow runs autonomously end-to-end across stages (and across queued units in a batch) without pausing for human sign-off gates mid-flight. Never run `git push`, upload CLs, or file or comment on external bug trackers or repositories unless explicitly asked.

---

## Abstract Harness Interface

This workflow is harness- and model-agnostic. The calling harness adapter supplies concrete bindings for six abstract primitives and injects them into every spawned worker's startup brief:

- **`MODEL_MAP`**: Maps abstract personas (`Researcher`, `Planner`, `Critic-1`, optional cross-family `Critic-2`, `Zircon-Coder`, `Zircon-Coder-Escalation`, `Zircon-Parity-Reviewer`, `Zircon-Safety-Reviewer`, and optional Stage 6 `Commit-Reviewer` / `Commit-Writer`) to concrete frontier reasoning models, paired with the **No-Lightweight-Downgrade Quota Fallback Rule**: if a pinned model tier is exhausted, pin or move that worker in place via `SWITCH_MODEL` to another frontier reasoning model and record the substitution in the final report. **Never** downgrade core roles to lightweight, fast, or mini model tiers.
- **`SPAWN_WORKER` / `WAIT_FOR_WORKERS` / `SWITCH_MODEL` / `KILL_WORKER`**: Starts fresh workers, waits for completion, switches a worker's model in place on quota exhaustion, and terminates completed stage workers at stage boundaries to keep the roster lean.
- **`HANDOFF(recipient, status_tag, payload)` & `READ_ARTIFACT(ref)`**: Passes drafts, findings, and verdicts by reference without copy-pasting long outputs between workers—**except** at an optional Stage 6 clean-room commit where referencing pre-commit artifacts is forbidden.
- **`STATE_LEDGER`**: Persistent per-session ledger stored inside the harness-provided per-session brief/artifact directory (`$PROMPTS`, which also holds `plan.md`, `impl.md`, `fixes.md`, and saved `babeldiff` artifacts) tracking the active unit, C++ files, target Rust files, git workspace path and pre-existing dirty files, verified build/clippy/test/format commands, `babeldiff` baseline counts, and C++ bugs discovered so far with filing status.
- **`DECISION_LEDGER`**: Persistent log maintained by `<LEAD>` recording autonomous engineering/mapping tradeoffs (**Decision**, **Options Considered**, **Choice & Rationale**, and **Code Locations**) for consolidated human review at final handoff.
- **`CLEANROOM_BARRIER`**: Optional Stage 6 context firewall (matching [../dev-swarm/personas/cleanroom-commit.md](../dev-swarm/personas/cleanroom-commit.md)) used only when the workflow requests a local git commit, preventing Stage 6 workers from reading `$PROMPTS`, `STATE_LEDGER`, `DECISION_LEDGER`, or prior swarm history.

---

## Persona Roster

| Persona | `MODEL_MAP` Key | Writes | Responsibility & Playbook |
| :--- | :--- | :--- | :--- |
| **Lead Agent (`<LEAD>`)** | Current session model | No port code | Preflight, `STATE_LEDGER`, `DECISION_LEDGER`, `plan.md` / `impl.md` coordination, git verification gate, `run_babeldiff.py` invocation, final report |
| **Researcher** (x2 parallel) | `MODEL_MAP[Researcher]` | No | Independent dual inventory of C++ functions, lines, comments, locks, assertions, `LTRACE`, allocations, FFI touchpoints, tests, and nearest Rust port conventions ([../dev-swarm/personas/researcher.md](../dev-swarm/personas/researcher.md)) |
| **Planner** *(optional worker or `<LEAD>`)* | `MODEL_MAP[Planner]` | No | Writes `$PROMPTS/plan.md` from Stage 1 inventory under [doctrine.md](./doctrine.md) (function-by-function mapping table, file split, `cpp_`/`rust_` FFI shims, test suite mapping, known C++ oddities) and delivers `HANDOFF(LEAD, "[done]", ...)` |
| **Critic-1 / Critic-2** | `MODEL_MAP[Critic-1]` + optional `MODEL_MAP[Critic-2]` | No | Parallel cross-family audit of `plan.md` against cited C++ lines, [doctrine.md](./doctrine.md), and the in-tree rubric |
| **Zircon Coder** | `MODEL_MAP[Zircon-Coder]` (escalation: `MODEL_MAP[Zircon-Coder-Escalation]`) | Yes (single writer) | Ports C++ to Rust in-place inside the Fuchsia git workspace under [doctrine.md](./doctrine.md); playbook [zircon-coder.md](./personas/zircon-coder.md) |
| **Zircon Parity Reviewer** | `MODEL_MAP[Zircon-Parity-Reviewer]` | No (read-only; runs `run_babeldiff.py --pair` only) | Independent review required for parity; treats idiomatic improvements as defects; playbook [zircon-parity-reviewer.md](./personas/zircon-parity-reviewer.md) |
| **Zircon Safety Reviewer** | `MODEL_MAP[Zircon-Safety-Reviewer]` | No (read-only) | Verifies identical safety surface and zero Rust-specific UB without asking for safer Rust; playbook [zircon-safety-reviewer.md](./personas/zircon-safety-reviewer.md) |

There is no separate "correctness" review. In a bug-for-bug port, correctness *is* parity, so `Zircon-Parity-Reviewer` serves as the required independent reviewer. Both reviewers run in fresh contexts; when `Zircon-Coder` ran on its default tier, both reviewers also run on a distinct model tier (when `Zircon-Coder` was escalated, note that explicitly in the final report).

---

## In-Tree Fuchsia References

Roles read these files inside the active Fuchsia checkout:

| Reference | What It Is | Who Reads It |
| :--- | :--- | :--- |
| `zircon/skills/cpp-to-rust-rubric/SKILL.md` | Unified porting rubric: principles, machinery, patterns, 30-item pitfalls checklist, report format | Every role; [doctrine.md](./doctrine.md) item 7 defines which parts bind now |
| `zircon/skills/cpp-to-rust-dispatcher/SKILL.md` | Dispatcher and syscall ports: facades, `DispatcherState`, pin-init, bindgen, FFI shims | Coder and reviewers when porting a dispatcher or syscall |
| `zircon/skills/fbl-intrusive-porting/SKILL.md` | Porting `fbl` intrusive containers | Coder and reviewers when the unit uses intrusive containers |
| `zircon/skills/babeldiff/SKILL.md` | Line-by-line C++/Rust alignment checker and agent modes | Parity reviewer and coder via [run_babeldiff.py](./scripts/run_babeldiff.py) |
| `zircon/skills/cpp-to-rust/SKILL.md` | Coder/reviewer orchestration loop and git verification gate | `<LEAD>`; this workflow preserves its git verification gate |
| `docs/development/languages/rust/unsafe.md` | Tree-wide unsafe conventions | Safety reviewer |
| `local/zircon-kernel-research.md` §4 | Local dossier: FFI pattern, build architecture, test mechanics | `<LEAD>`, for accurate briefs |

`.agents/skills/rust_best_practices` is deliberately excluded from this list; it describes the cleanup phase's target, not Phase 1 parity porting.

---

## Preflight (Before the First Unit in a Session)

1. **`babeldiff` bootstrap**: Run once inside the Fuchsia checkout:
   ```bash
   python3 "<CORE_ZIRCON_DIR>/scripts/run_babeldiff.py" --update --allow-issues tree --summary
   ```
   This clones or fast-forward pulls `local/babeldiff` and builds it with Fuchsia's prebuilt Rust toolchain. `--allow-issues` maps `babeldiff` exit code `1` (issues found) to `0` while keeping exit code `2` (build/invocation error) non-zero—never mask exit code `2` with `; true`. Only the initial clone or `--update` pull requires network access; subsequent `tree` and `commit` runs stay offline.
2. **Build directory & test recipe**: Confirm the active `fx set` line, clippy build target, and QEMU boot-test recipe (`zircon/scripts/run-zircon` under `sg kvm`, since `fx run-boot-test` requires a disk image). Put exact verified commands in every coder brief; the coder must never guess them.
3. **Git workspace check (strictly `git`-native)**: Inspect `git rev-parse --show-toplevel`, `git branch --show-current`, and `git status -s`. Zircon port work normally lives in an `fx` git worktree (`.jiri_root/worktrees/<name>`) or the main Fuchsia git checkout. Run `Zircon-Coder` **in-place** inside that Fuchsia git workspace rather than creating `/tmp` git worktrees so `out/` incremental GN/Ninja build caches and the Jiri layout stay intact. Record any pre-existing uncommitted files in `STATE_LEDGER` so they are never overwritten or swept into diffs.

---

## Pipeline Stages

### Stage 0 — State & Decision Ledgers

Initialize `STATE_LEDGER` with: the unit name, full C++ source/header/test paths, target Rust file paths, Fuchsia git workspace root and pre-existing dirty files, verified build/clippy/boot-test/`fx format-code` commands, `babeldiff` baseline counts, and the running list of C++ bugs discovered so far with filing status. Initialize `DECISION_LEDGER`. If sandbox rules prevent workers from reading `<CORE_ZIRCON_DIR>` or `<CORE_DEV_SWARM_DIR>` outside the Fuchsia tree, copy [doctrine.md](./doctrine.md), [run_babeldiff.py](./scripts/run_babeldiff.py), [../dev-swarm/personas/researcher.md](../dev-swarm/personas/researcher.md), and the three playbooks under `personas/` into `$PROMPTS/` once at Stage 0 and reference those staged paths in Stage 1–5 briefs.

### Stage 1 — Dual Research (`Researcher` x2)

Spawn two independent `Researcher` workers (`MODEL_MAP[Researcher]`) in parallel with identical unit-inventory briefs asking for:
1. Complete C++ inventory: every function and line number, comment count per function, locks and lock policies (IRQ-saving or not), `ASSERT` vs. `DEBUG_ASSERT` counts and sites, `LTRACE` calls, allocations and allocation tier, FFI touchpoints, and unit tests.
2. Nearest existing Rust port of the same kind and its conventions (rubric 3.11: file split, FFI naming, `BUILD.gn` wiring, test suite naming).
3. Which sections of the dispatcher or `fbl` guides apply.

Inventory errors propagate into every later stage, and resolving a discrepancy between two independent researchers is far cheaper now than after coding. Wait via `WAIT_FOR_WORKERS`, call `KILL_WORKER` on both researchers, resolve any count or symbol disagreements directly against the C++ lines, synthesize into `STATE_LEDGER`, and advance immediately to Stage 2.

### Stage 2 — Plan & Parallel Cross-Family Critique

Author `$PROMPTS/plan.md` (directly as `<LEAD>`, or via an optional `Planner` worker pinned to `MODEL_MAP[Planner]` briefed under [doctrine.md](./doctrine.md) and the Stage 1 inventory to deliver `HANDOFF(LEAD, "[done]", ...)` once `$PROMPTS/plan.md` is written) containing:
- Function-by-function mapping table: C++ symbol, Rust symbol with rename rules applied, target file, notes.
- File split and `BUILD.gn` / module wiring.
- FFI shims required (`cpp_` / `rust_` names, `FFI_ALWAYS_INLINE` + `https://fxbug.dev/537458631` where applicable).
- Test mapping (C++ test -> Rust `#[cfg(ktest)]` `unittest` suite name under rubric collision rules).
- Known C++ oddities and bugs that must be ported as-is.

Spawn `Critic-1` (`MODEL_MAP[Critic-1]`) and, when available, cross-family `Critic-2` (`MODEL_MAP[Critic-2]`) in parallel to audit `$PROMPTS/plan.md` against the C++ sources, [doctrine.md](./doctrine.md), and the rubric. Wait via `WAIT_FOR_WORKERS`, call `KILL_WORKER` on both critics (and `Planner` if used), resolve disagreements directly against cited source lines, fold valid findings into `$PROMPTS/plan.md`, log non-trivial mapping decisions in `DECISION_LEDGER`, and advance immediately to Stage 3.

### Stage 3 — Code & Git Verification Gate

Write `$PROMPTS/impl.md` combining the locked plan with the exact build, clippy, test (with suite name), `fx format-code`, and `run_babeldiff.py --allow-issues` self-check commands. Spawn `Zircon-Coder` (`MODEL_MAP[Zircon-Coder]`, reading `<CORE_ZIRCON_DIR>/personas/zircon-coder.md` and `<CORE_ZIRCON_DIR>/doctrine.md`) working in-place in the Fuchsia git workspace, and wait via `WAIT_FOR_WORKERS`.

Run the mandatory **git verification gate** immediately after the coder reports `[done]`. First ensure any newly created target files owned by this unit (never pre-existing untracked files) are registered with intent-to-add so plain `git diff HEAD` includes them, then inspect status and diff against `HEAD`:
```bash
git add -N -- <new-port-files>
git status -s
git diff --stat HEAD
```
- An empty `git diff --stat HEAD`, or a diff that does not touch the target Rust/wiring files, is an immediate rejection: re-brief the coder to author real files in-tree (`<LEAD>` never writes the port code itself).
- On a thin or failed attempt (broken build/tests, major missing functions, or persistent doctrine violations), call `KILL_WORKER` on the initial coder and spawn a fresh escalation coder pinned to `MODEL_MAP[Zircon-Coder-Escalation]` with the same brief plus what went wrong; record the escalation for the final report.
- Keep the active coder in waiting state for Stage 5 fixes and advance immediately to Stage 4.

### Stage 4 — `babeldiff` Artifacts & Parallel Parity + Safety Review

1. With newly created target files registered via `git add -N` at Stage 3, `<LEAD>` runs [run_babeldiff.py](./scripts/run_babeldiff.py) once for the diff scope (`tree` for working copy vs. `HEAD`, or `commit <rev>` for a committed scope) and saves both agent-stage outputs next to the briefs:
   ```bash
   python3 "<CORE_ZIRCON_DIR>/scripts/run_babeldiff.py" --allow-issues tree --summary --issues-only > "$PROMPTS/babeldiff-summary.txt"
   python3 "<CORE_ZIRCON_DIR>/scripts/run_babeldiff.py" --allow-issues tree --layout stacked --issues-only -U 3 > "$PROMPTS/babeldiff-stacked.txt"
   ```
   Confirm both commands exit `0` (`--allow-issues` maps exit `1` to `0`, so any non-zero exit indicates a real exit `2` failure to investigate before launching reviewers). Read only the top summary header lines for counts.
2. Spawn `Zircon-Parity-Reviewer` (`MODEL_MAP[Zircon-Parity-Reviewer]`) and `Zircon-Safety-Reviewer` (`MODEL_MAP[Zircon-Safety-Reviewer]`) in parallel. Each brief names the diff scope, both saved babeldiff file paths, the coder's ported-bugs list, and the word budget. Reviewers read the saved babeldiff files rather than rerunning `babeldiff`, except `Zircon-Parity-Reviewer` may invoke `run_babeldiff.py --allow-issues <scope> --pair 'Cpp::Fn=rust_fn' --layout stacked -U 3` to resolve split or unpaired functions.
3. **Safety escalation**: If the safety verdict and the coder's report disagree, or the unit involves data races, raw-pointer aliasing, or FFI ownership transfer, spawn a fresh `Zircon-Safety-Reviewer` with a brief narrowed to the disputed sites.
4. Wait via `WAIT_FOR_WORKERS`, read verdicts, and call `KILL_WORKER` on every reviewer.

### Stage 5 — Fix Loop (Cap: 3 Rounds)

- If parity verdict is `PARITY` and safety verdict is `SAME-SURFACE`, advance immediately to Stage 6.
- Otherwise, synthesize actionable parity and safety findings into `$PROMPTS/fixes.md`. If the coder "fixed" a C++ bug or made an unauthorized idiomatic cleanup, `$PROMPTS/fixes.md` must explicitly instruct restoring the C++ behavior and listing the bug under "C++ bugs ported as-is".
- Deliver `HANDOFF(ZIRCON_CODER, "[changes-needed]", "$PROMPTS/fixes.md")` to the waiting coder so it keeps its workspace context (or spawn a fresh coder if gone), and wait via `WAIT_FOR_WORKERS`.
- After the coder reports `[done]`, register any newly created owned files with `git add -N`, re-run the git verification gate (`git status -s` and `git diff --stat HEAD`), regenerate `$PROMPTS/babeldiff-summary.txt` and `$PROMPTS/babeldiff-stacked.txt` via `run_babeldiff.py --allow-issues`, and spawn fresh `Zircon-Parity-Reviewer` and `Zircon-Safety-Reviewer` workers for pass 2 / pass 3.
- Stop after **3 review-fix rounds**; record any remaining unresolved findings explicitly in the final report rather than looping endlessly.

### Stage 6 — Optional Clean-Room Local Commit & Final Report

1. Call `KILL_WORKER` on the active coder worker.
2. **Optional clean-room local git commit**: Only if the user or calling workflow explicitly asked to create a local git commit, isolate any pre-existing dirty files recorded in Stage 0, stage only the unit's intended files, and run the air-gapped `Commit-Reviewer` -> `Commit-Writer` flow from [../dev-swarm/personas/cleanroom-commit.md](../dev-swarm/personas/cleanroom-commit.md) under `CLEANROOM_BARRIER` (if Stage 6 runs under out-of-tree read restrictions, stage `cleanroom-commit.md` into a separate private temporary directory outside `$PROMPTS`—never inside `$PROMPTS`, which `CLEANROOM_BARRIER` forbids Stage 6 workers from reading—and remove it once Stage 6 completes). Otherwise leave the verified edits uncommitted in the Fuchsia git workspace.
3. If porting multiple units in a batch, advance straight to the next unit and present a single consolidated report at the end of the batch.
4. **Final Report**:
   - Files created/modified and verification commands + results (build, clippy, `k ut <suite>` / QEMU boot-test, `fx format-code`).
   - Final `babeldiff` counts (pairs, issues by kind, unpaired functions) and any issues remaining after the 3-round cap.
   - Justified function-mapping deviations (and whether `Zircon-Coder` was escalated).
   - **C++ bugs ported as-is**: each with `cpp path:line` and observed behavior, ready for `fxbug.dev` filing (once a bug number exists, a one-line follow-up adds `TODO(https://fxbug.dev/NNN)` comments at the ported sites).
   - Autonomous decisions distilled from `DECISION_LEDGER`.
   - **Strict hygiene ban**: never run `git push`, never upload CLs, and never put `conversation://` links, `Co-Authored-By` trailers, or internal worker IDs into commits, code, comments, or external artifacts.

---

## Brief Checklists & Failure Handling

- **Every coder brief must specify**: full C++ file paths, target Rust files and file split, applicable in-tree guides (dispatcher, `fbl`), mapping table path, exact build/clippy/test commands (with suite name), `fx format-code`, exact `run_babeldiff.py --allow-issues` self-check command with scope, and pre-existing dirty files to leave untouched.
- **Every reviewer brief must specify**: diff scope (`tree` or `commit <rev>`), paths of both saved `babeldiff` files, the coder's ported-bugs list, and word budget.
- **`babeldiff` build fails**: run `run_babeldiff.py --update ...`, then check `zircon/skills/babeldiff/SKILL.md` section 1.
- **`babeldiff` mis-pairs functions**: pass `--pair 'Cpp::Fn=rust_fn'` in the reviewer brief; do not let mis-pairings turn into false findings.
- **Coder "fixes" a C++ bug**: treat as a parity finding; instruct the coder in `$PROMPTS/fixes.md` to restore the C++ behavior and list the bug with `cpp path:line`.
