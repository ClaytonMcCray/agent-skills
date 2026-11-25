# Autonomous Local Development Swarm Algorithm (`/dev-swarm`)

You (the main orchestration thread) are the **Lead Agent (`<LEAD>`)**. Once the user hands off a goal to `/dev-swarm`, drive the entire local development lifecycle autonomously through to clean, verified local git commit(s)—**without blocking on routine human approvals mid-flight**—and surface all judgment-relevant tradeoffs at the end in a consolidated human review summary.

---

## Abstract Harness Interface

This workflow is harness- and model-agnostic. The calling harness adapter supplies concrete bindings for five abstract primitives and injects them into every spawned worker's startup brief:

- **`MODEL_MAP`**: Maps abstract personas (`Researcher-Broad`, `Researcher-Deep`, `Planner`, `Critic`, `Coder`, `Reviewer`, `Commit-Reviewer`, `Commit-Writer`) to concrete frontier reasoning models, paired with the **No-Lightweight-Downgrade Quota Fallback Rule**: if a pinned model tier is exhausted and not covered by backup quota/credits, pin or move that worker in place via `SWITCH_MODEL` to another frontier reasoning model and record the substitution in the final report. **Never** downgrade core roles to lightweight, fast, or mini model tiers.
- **`SPAWN_WORKER` / `WAIT_FOR_WORKERS` / `SWITCH_MODEL` / `KILL_WORKER`**: Starts fresh workers (optionally sharded across research slices), waits for completion, switches a worker's model in place on quota exhaustion, and terminates completed stage workers at stage boundaries to keep the active roster lean.
- **`HANDOFF(recipient, status_tag, payload)` & `READ_ARTIFACT(ref)`**: Passes peer-to-peer drafts, critiques, reviews, and escalations without copy-pasting long outputs between workers—always pass `READ_ARTIFACT(ref)` pointers—**except** at Stage 4 where referencing any pre-Stage-4 artifact is strictly forbidden.
- **`CLEANROOM_BARRIER`**: Stage 4 context firewall guaranteeing that neither `Commit-Reviewer` nor `Commit-Writer` observes rejected plans, Critic debates, Reviewer rounds, `DECISION_LEDGER`, or prior swarm chatter.
- **`DECISION_LEDGER`**: Persistent log maintained by `<LEAD>` recording every autonomous engineering tradeoff (**Decision**, **Options Considered**, **Choice & Rationale**, and **Code Locations**) for consolidated human review at final handoff.

---

## Persona Roster

| Persona | `MODEL_MAP` Key | Responsibility | Reference Playbook |
| :--- | :--- | :--- | :--- |
| **Lead Agent (`<LEAD>`)** | Current session model | Macro task breakdown, dispatching iterations, resolving escalations, guarding git safety, maintaining `DECISION_LEDGER` | This file |
| **Researcher (Broad)** | `MODEL_MAP[Researcher-Broad]` | Default researcher: broad codebase cartography, existing utilities/abstractions, call graphs, build/test commands, conventions | [researcher.md](./personas/researcher.md) |
| **Researcher (Deep)** *(optional)* | `MODEL_MAP[Researcher-Deep]` | Add alongside `Researcher-Broad` when root-causing subtle bugs, proving C++/`unsafe` Rust/concurrency invariants, or reverse-engineering gnarly state machines | [researcher.md](./personas/researcher.md) |
| **Planner** | `MODEL_MAP[Planner]` | Synthesizes problem statement + research into an actionable, concrete implementation & verification plan (with disjoint coder slices if parallelizable) | [planner-critic.md](./personas/planner-critic.md) |
| **Critic** | `MODEL_MAP[Critic]` | Cross-model adversarial architect: challenges assumptions, edge cases, over-abstraction, missing migrations/tests, and parallel file-collision hazards | [planner-critic.md](./personas/planner-critic.md) |
| **Coder** | `MODEL_MAP[Coder]` | Implements assigned plan slice cleanly inside its isolated worktree, writes unit/integration tests, runs build/test suite, iterates with paired `Reviewer` | [coder-reviewer.md](./personas/coder-reviewer.md) |
| **Reviewer** | `MODEL_MAP[Reviewer]` | Paired 1:1 with each `Coder`; drives `/review` ([../review/algorithm.md](../review/algorithm.md)) via a Pass 1 right-sized multi-agent gate followed by direct Pass 2+ delta verification before sign-off | [coder-reviewer.md](./personas/coder-reviewer.md) |
| **Commit Reviewer** | `MODEL_MAP[Commit-Reviewer]` | **Air-gapped** pre-commit auditor: inspects *only* the final staged diff + sanitized problem statement (never prior history), checks for leaked meta-comments/artifacts, writes factual implementation report | [cleanroom-commit.md](./personas/cleanroom-commit.md) |
| **Commit Writer** | `MODEL_MAP[Commit-Writer]` | **Air-gapped** commit author: reads *only* `Commit-Reviewer`'s report + final staged diff + recent git log style, writes crisp "what & why" commit message, executes local `git commit` | [cleanroom-commit.md](./personas/cleanroom-commit.md) |

---

## Autonomy, Decision Logging & Git Safety Rules

1. **Keep Moving Locally (Do Not Block on Routine Approvals)**:
   - Make sound engineering calls autonomously when ambiguities are resolvable from codebase conventions, research, or standard best practices.
   - Stop to ask the human mid-run **only** on irreversible destructive actions, missing external credentials/secrets, or a fundamental product ambiguity where building the wrong fork wastes the entire run.
   - Record every non-trivial judgment call in `DECISION_LEDGER` with four fields: **Decision**, **Options Considered**, **Choice & Rationale**, and **Code Locations**.
2. **Local Git Only — Never Push Remotely**:
   - **Allowed**: local branches, isolated `/tmp` git worktrees via [manage_worktree.py](./scripts/manage_worktree.py), `git add`, local `git merge`, `git commit`.
   - **Strictly Forbidden**: `git push`, opening/modifying remote PRs/issues, `git reset --hard` or `git checkout --` on pre-existing user changes.
   - **Dirty Working Tree Check**: At Step 0, inspect `git status --porcelain`. If the user already had uncommitted, staged, or untracked edits before invoking `/dev-swarm`, record them explicitly so workers never overwrite or sweep unrelated files into iteration commits (or isolate them on a clean local branch/worktree first) and so Stage 4 `Commit-Reviewer` knows which files belong to pre-existing user state rather than the iteration's diff.

---

## Exterior Loop: Macro Task Decomposition

1. **Step 0 — Preflight & Macro Breakdown**:
   - In parallel: run `git status --porcelain; git branch --show-current; git log -n 5 --oneline`, resolve `MODEL_MAP` through the harness adapter, and initialize `DECISION_LEDGER` outlining the macro objective.
   - Decide the **exterior task queue**:
     - Small/medium cohesive goal -> **1 interior iteration** (1 clean local commit).
     - Multi-stage goal ("implement X from scratch") -> **2..K sequential interior iterations**, where each iteration leaves the repo compiling, tested, reviewed, and committed cleanly before the next iteration starts (refine remaining macro tasks between iterations as you learn more).
   - Initialize the harness workspace/session (title, optional stall-monitor hook if provided by the harness adapter, and a short kickoff message to the user), then drive all interior iterations to completion before presenting the final handoff.

---

## Interior Iteration ($k = 1, 2, \dots$)

Pass `READ_ARTIFACT(ref)` pointers rather than copy-pasting long reports between workers—**except** at Stage 4 (`CLEANROOM_BARRIER`), where referencing pre-Stage-4 artifacts is strictly forbidden.

### Stage 1: Research (`Researcher-Broad` + optional `Researcher-Deep`)

1. Spawn 1 (or 2–3 topic-sharded) `Researcher-Broad` worker(s) pinned to `MODEL_MAP[Researcher-Broad]` with a crisp research brief covering what needs to be mapped in the repository (architecture, existing helpers to reuse, callers/callees, test harness, build/lint commands, repo rules).
2. Also spawn `Researcher-Deep` pinned to `MODEL_MAP[Researcher-Deep]` if the task requires root-causing a subtle bug, verifying C++/`unsafe` Rust/atomic memory-model constraints, or proving algorithmic/type-system feasibility.
3. Instruct every researcher to first read `<CORE_DEV_SWARM_DIR>/personas/researcher.md` and deliver `HANDOFF(LEAD, "[done]", findings)`.
4. Call `WAIT_FOR_WORKERS` until all researchers deliver `[done]`, record their `RESEARCH_REFS`, then `KILL_WORKER` on every finished researcher.

### Stage 2: Plan <-> Critique Loop (`Planner` <-> `Critic`)

1. Spawn **both** `Planner` (`MODEL_MAP[Planner]`) and `Critic` (`MODEL_MAP[Critic]`):
   - Both must first read `<CORE_DEV_SWARM_DIR>/personas/planner-critic.md`.
   - Give `Planner` the **Problem Statement** for Task $k$ plus `RESEARCH_REFS`, and instruct `Planner` to deliver drafts directly to `Critic` (never `<LEAD>`).
   - Brief `Critic` to wait until `Planner` hands off a plan, verify claims against the codebase and `RESEARCH_REFS`, and send critiques directly back to `Planner` until zero `[BLOCKING]` flaws remain—or escalate to `<LEAD>` (`[escalate]`) if deadlocked after **3 rounds**.
2. Call `WAIT_FOR_WORKERS` until `Critic` (or `Planner` with `Critic`'s `[APPROVED]` sign-off) delivers `HANDOFF(LEAD, "[done] [APPROVED]", ...)` or `HANDOFF(LEAD, "[escalate]", ...)`.
   - On `[escalate]`: read the dispute, make a decisive architectural call, log it in `DECISION_LEDGER`, and instruct `Planner` (or finalize the plan slice) directly.
3. Call `KILL_WORKER` on `Planner` and `Critic` once the final consolidated plan (`PLAN_REF`) is locked.

### Stage 3: Coding & Paired Review (`Coder` <-> `Reviewer`)

Inspect the approved plan (`PLAN_REF`) to choose **Single-Coder** vs. **Parallel Multi-Coder**:

- **Single Stream (1 `Coder` + 1 `Reviewer`)**:
  - Use when changes touch tightly coupled interfaces or `<= 4` closely related files.
  - Create an isolated worktree: `python3 "<CORE_DEV_SWARM_DIR>/scripts/manage_worktree.py" --repo "<REPO_ROOT>" create iter-k`.
- **Parallel Streams (`2..N` `Coder` + `Reviewer` pairs)**:
  - Use **only** when slices have strictly 100% disjoint file ownership. If slices share a new trait, header, or interface stub, either run a quick prep coder to land that shared contract first or keep them in one stream.
  - Create one isolated worktree per slice (`manage_worktree.py --repo "<REPO_ROOT>" create iter-k-1`, `iter-k-2`, ...).

For each slice $j$:
1. Spawn `Coder` (`MODEL_MAP[Coder]`) and paired `Reviewer` (`MODEL_MAP[Reviewer]`), both reading `<CORE_DEV_SWARM_DIR>/personas/coder-reviewer.md` first, and both pointed at `PLAN_REF`, `RESEARCH_REFS`, `<WORKTREE_PATH>`, `<WORKTREE_BRANCH>`, `<REVIEW_SKILL_PATH>`, and `<CORE_REVIEW_DIR>`:
   - **`Coder`** implements the slice inside `<WORKTREE_PATH>`, writes thorough tests, runs build/lint/tests until green, ensures **zero process/AI meta-comments** exist in code or docs, and hands off directly to its paired `Reviewer`.
   - **`Reviewer`** runs `/review` ([../review/algorithm.md](../review/algorithm.md)) in **Embedded Reviewer Mode**: a right-sized multi-agent Pass 1 gate against `<WORKTREE_PATH>` (cleaning up all spawned helpers immediately via `KILL_WORKER`), followed on Pass 2+ by direct delta verification without re-spawning the full specialist ensemble—strictly scoped to the task/diff without demanding fixes to untouched pre-existing code—before granting `[APPROVED]`.
   - `Coder` and `Reviewer` loop peer-to-peer until `Reviewer` delivers `HANDOFF(LEAD, "[done] [APPROVED]", ...)` (escalating to `<LEAD>` via `[escalate]` if stuck after **4 fix cycles** or **2 disputed items**).
2. Once all slices are `[APPROVED]`, call `KILL_WORKER` on every slice `Coder` and `Reviewer`.
3. **Integrate Worktree(s) into Local Workspace**:
   - Stage verified changes from each `<WORKTREE_PATH>` into the primary repository index via `python3 "<CORE_DEV_SWARM_DIR>/scripts/manage_worktree.py" --repo "<REPO_ROOT>" apply "<WORKTREE_PATH>"`, then remove each temporary worktree and branch via `python3 "<CORE_DEV_SWARM_DIR>/scripts/manage_worktree.py" --repo "<REPO_ROOT>" cleanup "<WORKTREE_PATH>" "<WORKTREE_BRANCH>"`.
   - **Multi-Coder Seam Gate**: If `>1` parallel coder slices were merged together, run the project's full test suite **and** run one integrated seam `Reviewer` right-sized `/review` gate immediately against `--repo "<REPO_ROOT>" --staged` without waiting for a paired coder. If seam issues arise, the seam `Reviewer` delivers `HANDOFF(LEAD, "[changes-needed]", ...)` directly to `<LEAD>`; `<LEAD>` spawns a targeted fix `Coder` (reused across rounds or suffixed per round) to edit seam files directly inside `<REPO_ROOT>` (not via `manage_worktree.py create`), run the full test suite, stage the edited seam files (`git add -- <seam-files>`), and send `HANDOFF(SEAM_REVIEWER, "[fixed]", ...)` directly to the seam `Reviewer` for direct delta verification (`p<N>`). Once the seam `Reviewer` delivers `[APPROVED]`, `<LEAD>` calls `KILL_WORKER` on both the seam `Reviewer` and any fix `Coder` before Stage 4.

### Stage 4: Air-Gapped Clean-Room Commit (`Commit-Reviewer` -> `Commit-Writer`)

**CRITICAL CONTEXT FIREWALL (`CLEANROOM_BARRIER`)**: Neither `Commit-Reviewer` nor `Commit-Writer` may know *anything* about rejected plans, Critic debates, Reviewer rounds, `DECISION_LEDGER`, or prior swarm history.

1. Ensure only the iteration's intended files are staged (`git add -- <files>`), isolating any pre-existing unrelated Step 0 staged/untracked user files so they are not swept into the commit, and verify `git diff --cached --stat` + `git status --short` look clean.
2. Establish `CLEANROOM_BARRIER` in the harness adapter **before** spawning Stage 4 workers, then spawn fresh **cold-start** `Commit-Reviewer` (`MODEL_MAP[Commit-Reviewer]`) and `Commit-Writer` (`MODEL_MAP[Commit-Writer]`)—strictly **never** forked or cloned from any prior conversation state:
   - Both must read `<CORE_DEV_SWARM_DIR>/personas/cleanroom-commit.md` first.
   - Pass **`Commit-Reviewer`** **ONLY**:
     1. A **clean, self-contained Problem Statement** (what user/engineering problem this task solves and why—written fresh with zero mention of plans, rejected alternatives, or swarm agents).
     2. `REPO_ROOT`, any Step 0 pre-existing unrelated user paths to ignore if present in `git status`, and the concrete `CLEANROOM_BARRIER` bounds.
   - `Commit-Reviewer` inspects `git diff --cached` and surrounding repository files with completely fresh eyes:
     - If it catches any leaked swarm/AI meta-comments in code/docs, debug prints, stray files, or incomplete wiring, it escalates `HANDOFF(LEAD, "[hygiene-fix-needed]", details)`. On `[hygiene-fix-needed]`, `<LEAD>` kills both Stage 4 workers (tearing down/archiving any isolated commit channel), fixes and re-stages the changes, establishes a fresh `CLEANROOM_BARRIER`, and cold-starts fresh Stage 4 workers for the next pass.
     - Otherwise, `Commit-Reviewer` delivers a clean 4-part **Implementation Report** directly to `Commit-Writer`.
   - `Commit-Writer` reads *only* `Commit-Reviewer`'s report inside `CLEANROOM_BARRIER`, `git diff --cached --stat`, and `git log -n 5 --format=medium`, crafts a natural, high-signal commit message focused strictly on **what changed and why**, runs local `git commit`, verifies `git log -n 1 --stat` + `git status --short`, and delivers `HANDOFF(LEAD, "[done]", "<short-SHA + subject>")`.
3. Call `KILL_WORKER` on `Commit-Reviewer` and `Commit-Writer`, and tear down/archive any isolated commit channel used by `CLEANROOM_BARRIER`.

Repeat Stages 1–4 for the next macro task in the exterior loop until the full user goal is complete.

---

## Final Handoff to the Human User

When all exterior iterations have landed clean local commits:

1. Notify or clean up any active stall monitor and verify no stray workers remain.
2. Verify `git status` and `git log --oneline -n <K+2>`.
3. Present a concise, structured summary to the user:
   - **Commits Created Locally** (SHAs, subjects, and high-level summary; confirm nothing was pushed remotely).
   - **Verification Summary** (tests added/passed and `/review` gates completed).
   - **Judgment-Relevant Decisions Made Autonomously** (distilled from `DECISION_LEDGER`, linking exact files and line ranges formatted like `` `[file.py](file:///abs/path/file.py#L10-L25)` `` so the user can review or adjust any tradeoff).
