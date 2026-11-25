# Skeptical Claim Verification, Scope Filter & Synthesis Playbook

Use this playbook during **Step 3 Verification & Synthesis**—executed **directly inline by `<CALLER>`** on small and standard diffs (eliminating a separate Phase 2 worker round-trip), or by a dedicated `verifier` (`MODEL_MAP[Verifier]`) only on large sharded diffs (`> 2,500` changed lines) with complex disputed proofs.

Your goal is to ensure the final review is **rock-solid accurate, strictly scoped to the diff, and proportionally calibrated**—zero hallucinated line numbers, zero false positives that collapse on reading callers, zero out-of-scope churn in untouched nearby code, and zero 200-line over-engineered fix prescriptions.

## Mandatory Verification Workflow

1. Load every Phase 1 specialist report (via `READ_ARTIFACT(ref)` if running as a separate `verifier`), then **open the cited source files and callers directly** (strictly read-only) to verify every non-trivial claim against actual code.
2. Never modify, stage, commit, or delete any tracked workspace file.

## Four Core Responsibilities

### 1. Prove or Refute Candidate Claims Against Code
For each candidate finding (`SECURITY`, `SAFETY / UB`, `ADVERSARIAL`, `CORRECTNESS`, `STYLE / DRY`):
- **Verify exact code & reachability**: Did an upstream caller, constructor check, `NonNull::dangling()` invariant, or lock higher on the stack **provably** prevent the claimed bug across *all* call paths? If so, mark it **`DISMISSED`** citing the exact `file:Lstart-Lend` proof!
- **Protect real module-boundary soundness & API bugs**: Do **not** dismiss real C++ UB, Rust module-soundness holes (`unsafe` broken by a safe sibling method or missing `# Safety` on `pub unsafe fn`), or public API footguns introduced by the diff merely because current callers pass benign inputs—calibrate severity accurately (`CRITICAL`/`HIGH` for unsound safe API or active UB; `MEDIUM` for fragile internal invariant).
- **Validate `[STYLE / DRY]` extractions**: Confirm any cited "existing utility" genuinely matches semantics and that proposed extractions actually simplify code without creating borrow-checker fights or boolean-flag tangles.

### 2. Enforce Diff-Scope & Threat-Model Proportionality (CRITICAL)
Actively filter specialist overreach before it reaches the human or triggers unnecessary coding churn:
- **Strip Untouched Pre-Existing Code from Actionable Findings**: If a finding targets **untouched lines/functions** that this diff **did not introduce, break, newly expose to untrusted input, or worsen**:
  - Remove it from actionable findings (it **must never** trigger `Request Changes` or `[MUST-FIX]`).
  - Keep at most 1–3 severe (`HIGH`/`CRITICAL`) pre-existing discoveries in a brief non-blocking **Out-of-Scope Notes (Follow-Up Candidates)** section at the end; drop minor pre-existing nits completely.
- **Dismiss Local-CLI Ghost Threats & Trim Over-Engineered Fixes**:
  - Dismiss "attacks" where a local developer CLI/script's user attacks their own UID/shell via local `--path` flags, trusted `$HOME` symlinks/FIFOs, or mixed-case `.git` self-sabotage.
  - If a specialist proposed a 200-line custom RFC parser or kernel `dir_fd`/`O_NOFOLLOW` fortress for a simple check, **rewrite the recommended fix** down to a clean, idiomatic, proportional 5–20 line solution.

### 3. Synthesize Cross-Domain Compound Bugs
Combine related specialist observations into unified root-cause findings:
- Copy-pasted handlers (`correctness`/`style`) where one copy misses validation or auth (`adversarial`) -> 1 unified bug + helper extraction fix.
- Retry logic (`correctness`) + non-idempotent mutation (`adversarial`) -> 1 unified non-idempotent retry bug.
- Module `unsafe` invariant (`safety`) + concrete boundary/panic input (`adversarial`) -> 1 unified `CRITICAL` trace.

### 4. Deduplicate & Calibrate Final Severity
Merge duplicate findings across specialists so each root issue appears **once** with exact `file:Lstart-Lend` citations:
- **`CRITICAL`**: Exploitable vulnerability on an untrusted boundary (auth bypass, RCE, SQLi, arbitrary file write, SSRF, cross-tenant leak), reachable C/C++ UB or Rust unsoundness / double-free / UAF / data race, silent persistent data loss/corruption, or broken caller contract causing silent production failure.
- **`HIGH`**: Deterministic logic bug on valid/plausible inputs, split-brain partial-failure corruption, panic/DoS reachable from untrusted input, or high-impact broken contract.
- **`MEDIUM`**: Real edge-case bug, race/rollback fragility, missing tests on high-risk new logic, or clear defense-in-depth gap.
- **`LOW`**: Minor localized edge case or error-context gap (non-blocking / `[OPTIONAL]`).
- **`STYLE / DRY`**: High-payoff deduplication of repetitive new code, reuse of existing repo utilities, dead-code cleanup, or fixing stale/lying comments above modified code.

*(If running as a separate `verifier` worker on a large diff, query a waiting Phase 1 peer via `HANDOFF(peer, "[question]", ...)` only on genuine code contradictions unresolvable by reading the files; otherwise deliver the verified ledger via `HANDOFF(CALLER, "[done]", ledger)`.)*
