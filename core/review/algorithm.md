# Right-Sized, Diff-Scoped Multi-Agent Code Review Algorithm

Run a high-signal, multi-perspective code review that catches **real bugs, security flaws, C++/`unsafe` Rust soundness holes, broken caller contracts, and copy-paste bloat**—while **respecting diff scope, calibrating to real threat models, and right-sizing agent count to diff complexity**.

---

## Core Scope, Proportionality & Efficiency Invariants

1. **Strict Diff-Scope Boundary (Never Demand Fixes to Untouched Nearby Code)**:
   - Read full modified files, enclosing modules, and callers/callees across the repository **for context and contract verification** (never review 3-line diff hunks in isolation).
   - However, a finding is **In-Scope (Actionable / Blocking)** **ONLY** if it is **(a) introduced in added/modified lines** or **(b) directly caused, exposed to new untrusted input, or worsened by this diff** (e.g., changing a return type breaks an untouched caller, or mutating a private field breaks a sibling `unsafe` invariant).
   - Pre-existing bugs or tech debt in untouched nearby code that this diff did not break or expose **must NEVER drive `Request Changes` or `[MUST-FIX]`**. Drop minor untouched nits completely; at most, list up to 3 severe (`HIGH`/`CRITICAL`) pre-existing bugs under **Out-of-Scope Notes (Non-Blocking Follow-Up Candidates)** at the end.
2. **Threat-Model & Fix Proportionality**:
   - Calibrate rigor to the actual trust boundary: internet-facing endpoints, multi-tenant queries, and untrusted file/notebook/archive parsers warrant full adversarial scrutiny; local developer CLIs, build scripts, and test utilities trust the invoking developer's CLI flags and local `$HOME`/repo files.
   - Recommend **simple, idiomatic, proportional fixes**—never demand 200-line custom parsers or defensive fortresses that dwarf the change.

---

## Abstract Harness Interface

The calling harness adapter supplies concrete bindings for four abstract primitives and injects them into every spawned worker's startup brief:

- **`MODEL_MAP`**: Maps abstract personas (`Correctness`, `Adversarial`, `Safety`, and optional large-diff `Verifier`) across two complementary frontier profiles (**Judgment / Synthesis Tier** vs. **Volume Adversarial / Verification Tier**), enforcing a **Cross-Model Review Guarantee**: every diff—including Small diffs with only 1 spawned worker—must receive independent judgment-tier review eyes (never auditing volume-tier code solely with the same volume-tier model), paired with a **No-Lightweight-Downgrade Quota Fallback Rule**.
- **`SPAWN_WORKER` / `WAIT_FOR_WORKERS` / `SWITCH_MODEL` / `KILL_WORKER`**: Launches read-only specialist workers (optionally sharded across subsystem slices on large diffs), waits for completion, switches a worker's model in place on quota exhaustion, and terminates all helpers on completion.
- **`HANDOFF(recipient, status_tag, payload)`**: Delivers a worker's structured report (`[done]`) to `<CALLER>` (or a targeted `[question]` on large-diff verification).
- **`READ_ARTIFACT(ref)`**: Passes pointers to Phase 1 reports without copy-pasting report bodies.

---

## Specialist Roster & Right-Sized Scaling

| Role Key | Specialist Lens | `MODEL_MAP` Key | Trigger | Playbook (Read First) |
| :--- | :--- | :--- | :--- | :--- |
| `correctness` | **Correctness, Caller Contracts, Tests & Code Health (DRY)** — logic, cross-repo caller/callee impact, test adequacy, copy-paste elimination, existing-utility reuse | `MODEL_MAP[Correctness]` | Standard & Large diffs (inline by `<CALLER>` on Small diffs) | [correctness-style.md](./playbooks/correctness-style.md) |
| `adversarial` | **Calibrated Security & Adversarial Reviewer** — real trust boundaries, AuthN/AuthZ, injection, path/SSRF, systems/IPC boundaries, partial-failure corruption, races, boundary/UTF-8/`O(N^2)` edge cases | `MODEL_MAP[Adversarial]` *(judgment tier on Small diffs; paired with judgment-tier `correctness` on Standard/Large diffs)* | Always | [security-adversarial.md](./playbooks/security-adversarial.md) |
| `safety` | **Systems, Memory, UB & Concurrency Auditor** — C/C++, `unsafe` Rust + module-boundary safe callers, unwind/drop safety, FFI, syscall/memory mappings, locks/atomics | `MODEL_MAP[Safety]` *(judgment tier)* | Conditional: spawn **only** when diff touches C/C++ (`HAS_CPP=1`), Rust `unsafe` (`HAS_RUST_UNSAFE=1` or safe code in an `unsafe` module), FFI/systems boundaries (`HAS_FFI=1`), or real multi-threaded/async shared-state/lock-free concurrency | [safety-cpp-rust.md](./playbooks/safety-cpp-rust.md) |
| `verifier` | **Skeptical Claim Verifier & Scope Filter** — proves reachability in code, strips out-of-scope/over-engineered items, merges duplicates | `MODEL_MAP[Verifier]` | Inline by `<CALLER>` by default; spawn separate worker **only** on Large diffs (`> 2,500` changed lines) with complex disputed proofs | [verifier.md](./playbooks/verifier.md) |

### Tiered Worker Count by Diff Scale (`summary.txt` `CHANGED_FILES_COUNT` & `(+A / -R)`)

1. **Small Diff (`<= 3` changed files AND `A + R <= 120` lines)**:
   - Spawn **1 `adversarial` worker** pinned to the independent judgment tier (also reading [safety-cpp-rust.md](./playbooks/safety-cpp-rust.md) if the `safety` trigger applies), while **`<CALLER>`** directly executes [correctness-style.md](./playbooks/correctness-style.md) + [verifier.md](./playbooks/verifier.md) inline. (**1 spawned worker, 1 wait barrier**—preserving cross-model review coverage.)
2. **Standard Diff (default — up to `20` changed source files and `A + R <= 2,500` lines)**:
   - Spawn **`correctness` + `adversarial`** in parallel, plus **`safety`** if the systems/concurrency trigger applies; **`<CALLER>`** executes [verifier.md](./playbooks/verifier.md) inline. (**2–3 spawned workers, 1 wait barrier**.)
3. **Large Diff (`> 20` changed source files or `A + R > 2,500` lines across subsystems)**:
   - Shard `correctness` (and `safety` if systems-heavy) across 2–3 subsystem slices via `SPAWN_WORKER` (`<= 6` total workers max); optionally spawn 1 `verifier` worker via `READ_ARTIFACT(ref)` if disputed cross-shard proofs warrant independent verification.

---

## Two Invocation Contexts (Standalone Lead vs. Embedded Reviewer)

1. **Standalone Lead Mode (Default — user directly requested a code review)**:
   - Set `<CALLER>` to yourself, run Steps 1–4 for the matching diff-scale tier, terminate all helpers via `KILL_WORKER`, remove `<SUMMARY_DIR>`, and present the human-facing report.
2. **Embedded Reviewer Mode (Paired or Seam Reviewer inside an outer development loop)**:
   - Pass `--repo "<WORKTREE_PATH>" --working [-- <slice-files>]` (adding `-- <slice-files>` on single-stream in-place runs with pre-existing unrelated dirty paths, or `--repo "<REPO_ROOT>" --staged` for a multi-coder seam review) to [collect_review_context.py](./scripts/collect_review_context.py), namespace helpers uniquely per pass, and set `<CALLER>` to yourself so helpers never wake the outer lead.
   - **Pass 1 (Initial Ready Review — Right-Sized Multi-Agent Gate)**:
     - Run Steps 1–4 once for the diff-scale tier, verify **zero process/review meta-comments** leaked into code or docs, run build/lint/test suites **once** yourself, and terminate all helpers via `KILL_WORKER` + remove `<SUMMARY_DIR>`.
     - If zero `[MUST-FIX]` issues exist (`CRITICAL`, `HIGH`, or clear high-confidence `MEDIUM` / `[HIGH-IMPACT DRY]` **strictly within task/diff scope**), issue `[APPROVED]` immediately in Pass 1! Otherwise send a bounded `[MUST-FIX]` + non-blocking `[OPTIONAL]` list to the paired coder (or outer lead for seam reviews).
   - **Pass 2+ (Fix Verification — Direct Delta Check, No Full Re-Spawn)**:
     - When the coder signals `[fixed]`, inspect the fix delta and touched files **directly yourself** without re-spawning the specialist ensemble, re-verify zero meta-comments, and re-run build/lint/tests. *(Spawn at most **1** targeted `safety` [pinned to `MODEL_MAP[Safety]`] or `adversarial` helper ONLY if the fix rewrote `> 150` lines or introduced brand-new `unsafe`/C++/FFI/crypto code.)*
     - **No Goalpost-Moving Rule**: Pass 2+ may block **only** on unaddressed/broken Pass 1 `[MUST-FIX]` items or genuine `CRITICAL`/`HIGH` regressions introduced by the fix—never raise new `MEDIUM`/`LOW`/`STYLE` opinions on unchanged Pass 1 code or untouched pre-existing code.

---

## Execution Protocol

### Step 1: Scope Discovery & Model Resolution (Single Parallel Turn)
1. Run `python3 "<CORE_REVIEW_DIR>/scripts/collect_review_context.py" [--repo /path/to/worktree] [optional-scope-args]` ([collect_review_context.py](./scripts/collect_review_context.py)). If no scope was passed and `summary.txt` shows both working-tree edits and branch commits ahead of base, re-run with `--branch-and-working` unless unstaged-only was requested.
2. Resolve `MODEL_MAP` (**No-Lightweight-Downgrade Rule**: cross-pin between frontier tiers on quota exhaustion; never silently downgrade to lightweight/fast/mini tiers).

### Step 2: Spawn Right-Sized Specialist Workers (Single Parallel Turn)
Launch the 1–3 workers (or `<= 6` sharded workers on Large diffs) with a cold-start brief containing:
1. Repo root / `<WORKTREE_PATH>`, scope, and `<SUMMARY_DIR>` paths (`summary.txt`, `changed_files.txt`, `repo_files.txt`, `diff.patch`, `hazard_hits.txt`).
2. Mandatory first action: read its absolute `<CORE_REVIEW_DIR>/playbooks/<playbook>.md` path.
3. Concrete `HANDOFF(CALLER, "[done]", report)` binding, strict read-only rule (never edit files or run competing full test builds), exact `path/to/file:Lstart-Lend` citation requirement, and strict **In-Scope vs. Out-of-Scope Pre-Existing** separation.

### Step 3: Await Workers & Verify Claims Against Code
1. Call `WAIT_FOR_WORKERS()` until spawned specialists deliver `[done]` (using `SWITCH_MODEL` in place on mid-run quota hits).
2. Read [verifier.md](./playbooks/verifier.md) and verify every candidate claim directly in the source files (or via `verifier` + `READ_ARTIFACT(ref)` only on `> 2,500`-line sharded diffs): refute false positives, strip untouched pre-existing code out of actionable findings, trim over-engineered fix proposals, and deduplicate.

### Step 4: Clean Up & Present the Final Review
1. Terminate all spawned helpers via `KILL_WORKER` and remove `<SUMMARY_DIR>`.
2. Present a crisp report using clickable links formatted like `` `[parser.rs](file:///abs/path/src/parser.rs#L42-L68)` `` (writing an artifact in Standalone Mode only if `> 8` verified findings exist):
   1. **Verdict & Scope**: `Approve`, `Approve with Changes`, or `Request Changes / Block`, diff scope, and models executed.
   2. **Critical & High Findings (`[CRITICAL]`, `[HIGH]`)**: Domain tag, file/line links, concrete trigger/exploit scenario, and proportional fix.
   3. **Medium, Low & DRY Findings (`[MEDIUM]`, `[LOW]`, `[STYLE / DRY]`)**: Concise edge cases, missing high-risk tests, and high-payoff deduplication / existing-helper reuse.
   4. **Verified Sound & Dismissed Claims**: 2–4 concise bullets on high-risk surfaces confirmed sound and any specialist false alarms disproved.
   5. **Out-of-Scope Notes (Non-Blocking, Optional)**: Up to 3 brief follow-up notes for severe pre-existing issues in untouched code (omit if none).
