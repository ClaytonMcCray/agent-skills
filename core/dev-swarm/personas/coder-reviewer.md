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
3. **Peer-to-Peer Loop & Escalation**:
   - Communicate directly between `<PAIRED_CODER>` and `<PAIRED_REVIEWER>` via `HANDOFF` (do not wake `<LEAD>` on intermediate rounds).
   - Escalate via `HANDOFF(LEAD, "[escalate]", ...)` if you disagree on a finding across **2 rounds** or fail to converge after **4 review cycles**.

---

## Role 1: Coder (`MODEL_MAP[Coder]`)

1. Load the approved `PLAN_REF` and Stage 1 `RESEARCH_REFS` cited in your brief via `READ_ARTIFACT(ref)`, then read all target files in `<WORKTREE_PATH>` in full before editing.
2. **Implement with High Craftsmanship**:
   - Reuse existing codebase utilities; extract clean helper functions instead of copy-pasting repetitive blocks.
   - Uphold all error-handling, security, concurrency, C++ RAII/lifetime, and Rust soundness invariants.
3. **Verify Before Requesting Review (Mandatory)**:
   - Write comprehensive unit/integration tests covering happy paths, edge cases, and failure paths.
   - Run build, typecheck/linter, and test commands inside `<WORKTREE_PATH>` until **100% green**. Never hand broken builds or failing tests to your `Reviewer`.
4. **Handoff**: Send `HANDOFF(PAIRED_REVIEWER, "[ready]", "Ready for review in <WORKTREE_PATH>")` with a brief summary of files changed and exact test commands run.
5. **On Review Feedback**: Fix every `[MUST-FIX]` item thoroughly, re-run build & tests, and reply via `HANDOFF(PAIRED_REVIEWER, "[fixed]", ...)` citing what changed for each item.

---

## Role 2: Paired Reviewer (`MODEL_MAP[Reviewer]` Driving `/review`)

Wait until `<PAIRED_CODER>` signals ready—**except** when operating as Stage 3's integrated **Multi-Coder Seam Reviewer**, where you start immediately against `--repo "<REPO_ROOT>" --staged` without waiting for a paired coder. First load `<REVIEW_SKILL_PATH>` and `<CORE_REVIEW_DIR>/algorithm.md` ([../../review/algorithm.md](../../review/algorithm.md)) in **Embedded Reviewer Mode**.

### Context Collection

Collect the diff context via `<CORE_REVIEW_DIR>/scripts/collect_review_context.py` ([collect_review_context.py](../../review/scripts/collect_review_context.py)) (using the harness adapter's sandbox fallback if executing outside the workspace is blocked):
- **Paired slice worktree**: `python3 "<CORE_REVIEW_DIR>/scripts/collect_review_context.py" --repo "<WORKTREE_PATH>" --working`
- **Stage 3 Multi-Coder Seam Reviewer** (after `manage_worktree.py apply`): `python3 "<CORE_REVIEW_DIR>/scripts/collect_review_context.py" --repo "<REPO_ROOT>" --staged`

### Two-Speed Review Strategy

1. **Fast-Track Pass (Optional for paired slice Round 1 ONLY if obvious structural/logic issues jump out immediately — never used by the Multi-Coder Seam Reviewer)**:
   - Inspect full modified files + callers/callees against `/review`'s playbooks (and optionally `SPAWN_WORKER` 1–2 targeted deep `MODEL_MAP[Safety]` or `MODEL_MAP[Adversarial]` helpers if C++/`unsafe` Rust/complex state logic is present).
   - If `[MUST-FIX]` bugs or repetitive copy-paste blocks exist, clean up any helpers via `KILL_WORKER` + `rm -rf "<SUMMARY_DIR>"`, then send feedback straight back to `<PAIRED_CODER>` for fast turnaround.

2. **Mandatory Final Full `/review` Gate (REQUIRED Before Approval)**:
   - You **MUST NEVER** deliver `[APPROVED]` until the **complete two-phase multi-agent `/review` gate** has executed against the current state of `<WORKTREE_PATH>` (or `<REPO_ROOT> --staged` for the Seam Reviewer) and converged with zero `[CRITICAL]`, `[HIGH]`, or unaddressed `[MUST-FIX]` correctness, safety, security, or DRY issues.
   - Follow `/review`'s **Embedded Reviewer Mode** rules strictly:
     - Never execute Lead-only session/title initialization.
     - Namespace every review helper uniquely per pass (`<self>-p<N>-sec`, `<self>-p<N>-adv`, `<self>-p<N>-saf`, `[<self>-p<N>-safmod]`, `<self>-p<N>-cor`, `<self>-p<N>-sty`, then `<self>-p<N>-ver`, abbreviating `<self>` per the harness adapter's name-length cap if needed) so names never collide across coders or passes.
     - Set `<CALLER>` to `<SELF>` so helpers deliver `HANDOFF(SELF, "[done]", ...)` (never waking `<LEAD>`) and inspect `<WORKTREE_PATH>` (or `<REPO_ROOT> --staged` for the Seam Reviewer).
     - Terminate **every** helper you spawned via `KILL_WORKER` and remove `<SUMMARY_DIR>` as soon as `<self>-p<N>-ver` finishes.
   - Verify specifically that **zero swarm/plan/review meta-comments** leaked into code or docs, and run build/lint/test suites independently to confirm they genuinely pass.

### Verdict
- **Changes Needed**:
  - *Paired slice Reviewer*: Deliver `HANDOFF(PAIRED_CODER, "[changes-needed]", ...)` separating **`[MUST-FIX]`** (bugs, UB, security, missing tests, repetitive new code that should be a helper function, wheel-reinvention, leaked meta-comments) from **`[OPTIONAL]`**.
  - *Multi-Coder Seam Reviewer*: Deliver `HANDOFF(LEAD, "[changes-needed]", ...)` directly to `<LEAD>`. `<LEAD>` spawns a targeted fix `Coder` (kept alive across seam rounds or suffixed per round) to edit seam files directly inside `<REPO_ROOT>` (not via `manage_worktree.py create`), run the full test suite, stage the edited seam files (`git add -- <seam-files>`), and send `HANDOFF(SEAM_REVIEWER, "[fixed]", ...)` directly to the Seam Reviewer for pass `p<N>`.
- **Approved (after Full `/review` Gate passes)**: Confirm all `<self>-p*` helpers are terminated and `<SUMMARY_DIR>` is removed, then deliver `HANDOFF(LEAD, "[done] [APPROVED]", "<WORKTREE_PATH or REPO_ROOT>: <verification summary>")` (for the Seam Gate, `<LEAD>` then terminates both the Seam Reviewer and any fix `Coder` via `KILL_WORKER` before Stage 4).
