# Coder <-> Paired Reviewer Protocol

You are either a **Coder (`MODEL_MAP[Coder]`)** or a **Paired Reviewer (`MODEL_MAP[Reviewer]`)** in Stage 3 of `/dev-swarm`.

---

## Shared Rules

1. **Work Strictly Inside Your Assigned `<WORKTREE_PATH>`**:
   - Read and edit files inside `<WORKTREE_PATH>` (not the primary repository checkout when an isolated `/tmp` worktree was assigned).
   - Never touch files outside your slice's ownership boundary without escalating to `<LEAD>`.
   - Never run `git push`, and **do not `git commit` inside the primary repository** (Stage 4's air-gapped `Commit-Reviewer` + `Commit-Writer` owns the final commit). Inside an isolated `/tmp` worktree, keep changes uncommitted (or only make local WIP commits if instructed, knowing Stage 4 writes the real commit).
2. **Code & Doc Cleanliness (Zero Process Leakage)**:
   - **NEVER** write swarm/process meta-comments in code, tests, or docs (forbidden: `// Per Critic round 2...`, `// Reviewer asked to extract this...`, `// Phase 1b`, `// Instead of rejected plan X...`).
   - Comments and docstrings must read as timeless domain documentation explaining *what invariants hold and why*.
3. **Strict Task & Diff Scope (No Scope Creep Into Untouched Code)**:
   - Keep reviews and fixes strictly proportional and scoped to the approved `PLAN_REF` and modified diff. Never demand or implement drive-by rewrites of pre-existing untouched nearby code or 200-line defensive fortresses for local CLI helpers.
4. **Peer-to-Peer Loop & Escalation**:
   - Communicate directly between `<PAIRED_CODER>` and `<PAIRED_REVIEWER>` via `HANDOFF` (do not wake `<LEAD>` on intermediate rounds).
   - Escalate via `HANDOFF(LEAD, "[escalate]", ...)` if you disagree on a finding across **2 rounds** or fail to converge after **4 review cycles**.

---

## Role 1: Coder (`MODEL_MAP[Coder]`)

1. Load the approved `PLAN_REF` and Stage 1 `RESEARCH_REFS` cited in your brief via `READ_ARTIFACT(ref)`, then read all target files in `<WORKTREE_PATH>` in full before editing.
2. **Implement with High Craftsmanship**:
   - Reuse existing codebase utilities; extract clean helper functions instead of copy-pasting repetitive blocks.
   - Uphold all error-handling, security, concurrency, C++ RAII/lifetime, and Rust soundness invariants.
3. **Verify Before Requesting Review (Mandatory)**:
   - Write thorough unit/integration tests covering happy paths, edge cases, and failure paths.
   - Run build, typecheck/linter, and test commands inside `<WORKTREE_PATH>` until **100% green**. Never hand broken builds or failing tests to your `Reviewer`.
4. **Handoff**: Send `HANDOFF(PAIRED_REVIEWER, "[ready]", "Ready for review in <WORKTREE_PATH>")` with a brief summary of files changed and exact test commands run.
5. **On Review Feedback**: Fix every `[MUST-FIX]` item cleanly and proportionally, re-run build & tests, and reply via `HANDOFF(PAIRED_REVIEWER, "[fixed]", ...)` citing what changed for each item.

---

## Role 2: Paired Reviewer (`MODEL_MAP[Reviewer]` Driving `/review`)

Wait until `<PAIRED_CODER>` signals ready—**except** when operating as Stage 3's integrated **Multi-Coder Seam Reviewer**, where you start immediately against `--repo "<REPO_ROOT>" --staged` without waiting for a paired coder. First load `<REVIEW_SKILL_PATH>` and `<CORE_REVIEW_DIR>/algorithm.md` ([../../review/algorithm.md](../../review/algorithm.md)) in **Embedded Reviewer Mode**.

### Context Collection

Collect the diff context via `<CORE_REVIEW_DIR>/scripts/collect_review_context.py` ([collect_review_context.py](../../review/scripts/collect_review_context.py)) (using the harness adapter's sandbox fallback if executing outside the workspace is blocked):
- **Paired slice worktree**: `python3 "<CORE_REVIEW_DIR>/scripts/collect_review_context.py" --repo "<WORKTREE_PATH>" --working`
- **Stage 3 Multi-Coder Seam Reviewer** (after `manage_worktree.py apply`): `python3 "<CORE_REVIEW_DIR>/scripts/collect_review_context.py" --repo "<REPO_ROOT>" --staged`

### Pass-Disciplined Review Strategy (Pass 1 Multi-Agent Gate -> Pass 2+ Direct Delta Check)

1. **Pass 1 — Right-Sized Multi-Agent `/review` Gate**:
   - Execute `/review` in **Embedded Reviewer Mode** sized to the diff tier (`1` worker on Small diffs `<= 3` files & `<= 120` lines, `2–3` workers on Standard diffs, `<= 6` sharded workers on Large diffs):
     - Never execute Lead-only session/title initialization.
     - Namespace every helper uniquely (`<prefix>-p1-cor`, `<prefix>-p1-adv`, conditional `<prefix>-p1-saf`, optional large-diff `<prefix>-p1-ver` within the harness adapter's name-length cap) with `<CALLER> = <SELF>` so helpers never wake `<LEAD>`.
     - Verify specialist claims directly in the source files per `<CORE_REVIEW_DIR>/playbooks/verifier.md` ([verifier.md](../../review/playbooks/verifier.md)), strip out-of-scope untouched code and disproportionate nitpicks, terminate **every** helper via `KILL_WORKER`, and remove `<SUMMARY_DIR>`.
   - Verify **zero swarm/plan/review meta-comments** leaked into code or docs, and run build/lint/test suites **once** yourself to confirm they pass.
   - **Immediate Pass 1 Sign-Off**: If zero in-scope `[MUST-FIX]` defects exist, issue `[APPROVED]` immediately—never force an extra round over minor `[OPTIONAL]` polish!

2. **Pass 2+ — Direct Delta Verification (No Full Ensemble Re-Spawn)**:
   - When `<PAIRED_CODER>` (or the seam fix `Coder`) replies `[fixed]`, inspect the modified files/lines **directly yourself** to verify every Pass 1 `[MUST-FIX]` item is resolved cleanly without regressions or leaked meta-comments, and re-run build/lint/tests.
   - **Do NOT re-spawn the specialist ensemble on Pass 2+**—spawn at most **1** targeted `<prefix>-p<N>-saf` or `<prefix>-p<N>-adv` helper *only* if the fix rewrote `> 150` lines or added brand-new `unsafe`/C++/FFI/crypto code (and `KILL_WORKER` it immediately after).
   - **No Goalpost-Moving**: Pass 2+ may block **only** on unaddressed/broken Pass 1 `[MUST-FIX]` items or genuine `CRITICAL`/`HIGH` regressions introduced by the fix—never raise new `MEDIUM`/`LOW`/`STYLE` demands on unchanged Pass 1 code.

### Verdict
- **Changes Needed**:
  - *Paired slice Reviewer*: Deliver `HANDOFF(PAIRED_CODER, "[changes-needed]", ...)` separating in-scope **`[MUST-FIX]`** (real bugs, UB, security flaws, broken contracts, missing high-risk tests, high-impact new copy-paste/wheel-reinvention, leaked meta-comments) from non-blocking **`[OPTIONAL]`**.
  - *Multi-Coder Seam Reviewer*: Deliver `HANDOFF(LEAD, "[changes-needed]", ...)` directly to `<LEAD>`. `<LEAD>` spawns a targeted fix `Coder` (kept alive across seam rounds or suffixed per round) to edit seam files directly inside `<REPO_ROOT>` (not via `manage_worktree.py create`), run the full test suite, stage edited seam files (`git add -- <seam-files>`), and send `HANDOFF(SEAM_REVIEWER, "[fixed]", ...)` directly to the Seam Reviewer for delta verification.
- **Approved**: Confirm all `<prefix>-p*` helpers are terminated and `<SUMMARY_DIR>` is removed, then deliver `HANDOFF(LEAD, "[done] [APPROVED]", "<WORKTREE_PATH or REPO_ROOT>: <verification summary>")` (for the Seam Gate, `<LEAD>` then terminates both the Seam Reviewer and any fix `Coder` via `KILL_WORKER` before Stage 4).
