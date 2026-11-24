# Deep Multi-Agent Code Review Algorithm

Run a thorough, multi-perspective code review that prioritizes **depth, soundness, and concrete bug discovery over quota preservation**. Never rush, never skim raw 3-line diff hunks in isolation, and never skip specialist reviewers to save tokens.

---

## Abstract Harness Interface

This workflow is harness- and model-agnostic. The calling harness adapter supplies concrete bindings for four abstract primitives and injects them into every spawned worker's startup brief:

- **`MODEL_MAP`**: Maps abstract personas (`Security`, `Adversarial`, `Safety`, `Safety-Holistic`, `Correctness`, `Style`, `Verifier`) to concrete frontier models across two cognitive profiles, paired with a **No-Lightweight-Downgrade Quota Fallback Rule**.
- **`SPAWN_WORKER` / `WAIT_FOR_WORKERS` / `SWITCH_MODEL` / `KILL_WORKER`**: Launches read-only specialist workers (optionally sharded across subsystem slices), waits for completion, switches a worker's model in place on quota exhaustion, and terminates all helpers on completion.
- **`HANDOFF(recipient, status_tag, payload)`**: Delivers a worker's structured report (`[done]`) to `<CALLER>` or asks a targeted clarification (`[question]`) of a waiting Phase 1 specialist.
- **`READ_ARTIFACT(ref)`**: Passes pointers to Phase 1 reports into Phase 2 `Verifier` so the orchestrator never copy-pastes or summarizes Phase 1 reports inside the Verifier's brief.

---

## Two Invocation Contexts (Standalone Lead vs. Embedded Reviewer)

Detect how you are running before spawning helpers:

1. **Standalone Lead Mode (Default — user directly requested a code review)**:
   - Always execute the **Full Review Ensemble** below (never Fast-Track when invoked standalone).
   - Set `<CALLER>` to yourself, terminate all helpers via `KILL_WORKER` once Phase 2 finishes, and present the full human-facing report.
2. **Embedded Reviewer Mode (Running as a paired reviewer inside an outer development loop)**:
   - Pass `--repo "<WORKTREE_PATH>"` (and `--working` on uncommitted worktree edits) to [collect_review_context.py](./scripts/collect_review_context.py) when inspecting an isolated git worktree.
   - Namespace every spawned helper uniquely per pass so helper identifiers never collide across passes or parallel coders.
   - Set `<CALLER>` to yourself so spawned helpers report back to *you* rather than interrupting the outer coordinator.
   - **Two-Speed Gate (Fast-Track vs. Mandatory Full Gate)**:
     - *Fast-Track Pass (Optional on early mid-coding drafts only)*: Inspect full modified files and callers/callees directly against the playbooks, optionally spawning 1–2 targeted deep `Safety` or `Adversarial` helpers when C++, `unsafe` Rust, or complex stateful logic is present. Terminate any helpers via `KILL_WORKER` before sending `[MUST-FIX]` feedback back to the paired coder.
     - *Mandatory Full Review Ensemble Gate (REQUIRED before final `[APPROVED]` sign-off)*: You **MUST NEVER** issue `[APPROVED]` until the complete two-phase Full Review Ensemble below has run against the current worktree state and converged with zero `[CRITICAL]`, `[HIGH]`, or unaddressed `[MUST-FIX]` correctness, safety, security, or DRY issues. Also verify that **zero process/review meta-comments** leaked into code or docs, run build/lint/test suites independently, and terminate **every** helper via `KILL_WORKER` before delivering the final distilled `[MUST-FIX]`/`[OPTIONAL]` feedback to the paired coder or reporting `[APPROVED]` upward.

---

## Specialist Roster & Cognitive Profiles

Every full review executes a two-phase ensemble combining two complementary frontier profiles:

- **Holistic / Repo-Wide Synthesis Profile** (`Security`, `Safety-Holistic`, `Correctness`, `Style`): Repo-wide architectural context, end-to-end taint tracing, cross-file caller/callee contract verification, and duplication / wheel-reinvention detection.
- **Deep Symbolic, Adversarial & Proof Profile** (`Adversarial`, `Safety`, `Verifier`): Symbolic execution, concrete counterexample construction, C++ / `unsafe` Rust soundness & UB proofs, and skeptical Phase 2 claim verification.

| Role Key | Specialist Profile | `MODEL_MAP` Key | Phase & Trigger | Playbook (Worker Must Read First) |
| :--- | :--- | :--- | :--- | :--- |
| `security` | **Security & Trust-Boundary Auditor** — taint flows, AuthN/AuthZ, injection, SSRF, path/symlink races, crypto, secrets | `MODEL_MAP[Security]` | Phase 1 — Always | [security.md](./playbooks/security.md) |
| `adversarial` | **Adversarial Red-Team & Break-It Reviewer** — constructs concrete inputs, interleavings, partial failures, and complexity attacks | `MODEL_MAP[Adversarial]` | Phase 1 — Always | [adversarial.md](./playbooks/adversarial.md) |
| `safety` | **Systems, Memory & Concurrency Safety Auditor** — deep UB, lifetime, aliasing, unwind/panic safety, C++, `unsafe` Rust, FFI, locks/atomics | `MODEL_MAP[Safety]` | Phase 1 — Always | [safety-cpp-rust.md](./playbooks/safety-cpp-rust.md) |
| `safety-holistic` | **Cross-Module Soundness & Safe-Caller Auditor** — audits module/crate-wide invariants and all safe callers touching `unsafe`/C++/FFI state | `MODEL_MAP[Safety-Holistic]` | Phase 1 — Conditional: spawn whenever `summary.txt` reports `HAS_CPP=1`, `HAS_RUST_UNSAFE=1`, `HAS_FFI=1`, or `HAS_CONCURRENCY_HAZARDS=1` (or when reviewing any C/C++/Rust/systems code) | [safety-cpp-rust.md](./playbooks/safety-cpp-rust.md) |
| `correctness` | **Logic, Contracts & Test Adequacy Reviewer** — caller/callee impact across the repo, state invariants, edge cases, migration/schema compatibility, test gaps | `MODEL_MAP[Correctness]` | Phase 1 — Always | [correctness.md](./playbooks/correctness.md) |
| `style` | **Style, DRY & Structural Clarity Reviewer** — catches repetitive new code that should be a helper function, wheel-reinvention of existing utilities, and poor decomposition | `MODEL_MAP[Style]` | Phase 1 — Always | [style-dedup.md](./playbooks/style-dedup.md) |
| `verifier` | **Skeptical Proof Verifier & Synthesis Analyst** — traces every Phase 1 claim in actual code to prove/refute reachability, catch compound bugs, and calibrate severity | `MODEL_MAP[Verifier]` | Phase 2 — Always (after Phase 1 completes) | [verifier.md](./playbooks/verifier.md) |

### Adaptive Deep Scaling (Large Diffs)

Thoroughness beats frugality. If `summary.txt` shows **>15 changed source files** or **>1,200 changed lines** spanning multiple directories/subsystems:
- Do **not** cram the entire diff into a single `correctness` or `safety` pass where a reviewer might skim.
- Shard `correctness` (and `safety` if C++/Rust-heavy) across subsystem slices (`Subsystem A: <files>`, `Subsystem B: <files>`, ...) via `SPAWN_WORKER`'s slice mechanism so every shard receives unhurried, line-by-line scrutiny.

---

## Execution Protocol

### Step 1: Scope Discovery & Model Resolution (Single Parallel Turn)

In your first turn, execute in parallel:

1. **Collect the review context bundle** by running [collect_review_context.py](./scripts/collect_review_context.py) (passing `--repo <worktree>` if reviewing an external git worktree, plus any explicit ref/range/path scope args, or no scope args for smart auto-detection):
   ```bash
   python3 "<CORE_REVIEW_DIR>/scripts/collect_review_context.py" [--repo /path/to/worktree] [optional-scope-args]
   ```
   *Uncommitted + branch changes rule*: If no scope was specified and `summary.txt` shows **both** uncommitted working-tree edits **and** committed branch commits ahead of base, review the full branch + working-tree changes together (re-running with `--branch-and-working` if needed) unless the caller explicitly requested unstaged edits only.
2. **Resolve `MODEL_MAP`** using the harness adapter's model discovery mechanism:
   - **No-Lightweight-Downgrade Quota Fallback Rule**: If one frontier tier is completely exhausted (and not covered by backup quota/credits), inform the user (in Standalone Mode) which tier is exhausted and cross-pin affected workers to the other frontier tier. **Never** silently downgrade reviewers to lightweight, fast, or mini model tiers.

---

### Step 2: Spawn Phase 1 Specialist Reviewers (Single Parallel Turn)

In one parallel batch of `SPAWN_WORKER` calls, launch:
- Always: `security`, `adversarial`, `safety`, `correctness`, `style`.
- Conditionally: `safety-holistic` whenever `summary.txt` reports `HAS_CPP=1`, `HAS_RUST_UNSAFE=1`, `HAS_FFI=1`, or `HAS_CONCURRENCY_HAZARDS=1` (or for any C/C++/Rust/systems code).

#### Required Cold-Start Brief Contract for Every Phase 1 Worker

Spawned workers start fresh with zero prior conversation history. Every worker's startup brief **must** include:

1. **Context paths**: Repo root (or `<WORKTREE_PATH>`), review mode/scope, exact `SUMMARY_DIR` paths (`<SUMMARY_DIR>/summary.txt`, `<SUMMARY_DIR>/changed_files.txt`, `<SUMMARY_DIR>/diff.patch`, `<SUMMARY_DIR>/hazard_hits.txt`), plus any user focus or PR description.
2. **Mandatory first action**: Read its absolute playbook path (`<CORE_REVIEW_DIR>/playbooks/<playbook>.md`) before inspecting any code.
3. **Concrete harness bindings**: How to invoke `HANDOFF` (and `READ_ARTIFACT` for Phase 2) in this harness.
4. **Non-negotiable thoroughness & safety rules**:
   - **Read full context, never diff hunks alone**: Read the complete modified functions, types, modules, and files—and search for **all callers and callees** across the repository to verify contracts on both sides.
   - **Strictly non-destructive**: Never edit, format, stage, commit, or delete any tracked workspace file. Any throwaway script or PoC calculation must stay strictly inside `/tmp`.
   - **Evidence standard**: Every finding must cite exact file paths and line ranges (`path/to/file:L10-L25`), explain the concrete trigger/input/interleaving, assign a severity (`CRITICAL`, `HIGH`, `MEDIUM`, `LOW`, or `STYLE / DRY`), and propose a concrete fix.
5. **Independent Phase 1 handoff**:
   - Do **not** message or wake peer Phase 1 specialists during Phase 1 (keep initial audits independent).
   - Deliver the finished structured report via `HANDOFF(CALLER, "[done]", report)`. If zero issues exist in your domain after exhaustive inspection, explicitly list the high-risk paths and invariants verified and why they hold.

---

### Step 3: Await Phase 1 & Run Phase 2 Cross-Examination (`Verifier`)

1. Use `WAIT_FOR_WORKERS()` until **every** Phase 1 specialist has delivered `[done]`. If any worker hits quota mid-run, use `SWITCH_MODEL(worker, other_frontier_tier)` to move it in place to the other frontier tier rather than killing it.
2. Once all Phase 1 reports have arrived, launch `verifier` pinned to `MODEL_MAP[Verifier]` via `SPAWN_WORKER`:
   - Pass `READ_ARTIFACT(ref)` references for every Phase 1 report—**never** copy-paste or summarize Phase 1 reports inside the Verifier's brief.
   - Instruct `verifier` to first read `<CORE_REVIEW_DIR>/playbooks/verifier.md`, load all cited Phase 1 reports via `READ_ARTIFACT`, and inspect the actual workspace files directly to:
     1. Prove or refute every candidate bug's reachability and invariant assumptions (marking disproved claims `DISMISSED` with exact code proof, while keeping real latent soundness/security/API bugs even if current callers happen to dodge them).
     2. Identify **compound bugs** spanning multiple specialists (for example, duplicated code flagged by `style` combined with missing validation on one copy flagged by `security`).
     3. Query a waiting Phase 1 specialist via `HANDOFF(peer, "[question]", ...)` *only* if a subtle trace is genuinely ambiguous and unresolvable from the code; otherwise deliver the verified master ledger directly via `HANDOFF(CALLER, "[done]", ledger)`.
3. `WAIT_FOR_WORKERS()` until `verifier` delivers `[done]`.

---

### Step 4: Clean Up & Present the Final Review

1. **Clean up**:
   - Terminate every spawned review helper via `KILL_WORKER`.
   - Remove the temporary context directory (`rm -rf "<SUMMARY_DIR>"`).
2. **Deliver a high-signal, thorough review report** (to the user in Standalone Mode, or distilled into `[MUST-FIX]`/`[OPTIONAL]` vs. `[APPROVED]` feedback in Embedded Mode):
   - In Standalone Mode, if the review has many detailed findings (`>8` findings or complex C++/Rust proofs), write a full structured Markdown report artifact and present a concise executive summary pointing to it; otherwise present the full structured review directly.
   - Use clickable file links with exact line ranges formatted like `` `[parser.rs](file:///abs/path/src/parser.rs#L42-L68)` `` for every finding.
   - Structure the report in this exact order:
     1. **Verdict & Review Coverage**: Overall assessment (`Approve`, `Approve with Changes`, or `Request Changes / Block`), scope reviewed, and table of specialist roles + concrete models executed.
     2. **Critical & High-Severity Findings (`[CRITICAL]`, `[HIGH]`)**: Numbered findings with domain tag (`SECURITY`, `SAFETY / UB`, `ADVERSARIAL`, `CORRECTNESS`), file/line links, **Concrete Failure / Exploit Scenario**, and **Recommended Fix**.
     3. **Medium & Low-Severity Findings (`[MEDIUM]`, `[LOW]`)**: Edge cases, error-handling gaps, test coverage holes, and latent contract hazards.
     4. **Style, Duplication & Refactoring (`[STYLE / DRY]`)**: Repetitive new code blocks that should be extracted into functions/helpers (with proposed function signatures), existing codebase helpers that should be reused instead of duplicated, and structural readability improvements.
     5. **Audited & Verified Sound (Plus Dismissed False Positives)**: Brief summary of critical surfaces (`unsafe` blocks, C++ ownership boundaries, auth checks, concurrency primitives) inspected and confirmed sound, plus any candidate alarms `verifier` disproved with reasons.
