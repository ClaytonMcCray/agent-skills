# `agent-skills` — Architecture, Style & Authoring Guide

This repository is the single, multi-machine source of truth for Clayton's agent skills across machines (macOS and Linux). It separates **universal/core workflow logic** from **thin harness-specific bindings**, and serves as the home for authoring new skills.

---

## 1. Non-Negotiable Repository Style & Portability Rules

Every skill, helper script, and CLI tool in this repository **must** follow these rules:

1. **Prefer Python 3 Over Bash/Shell Scripts**:
   - Write all skill helper scripts (`scripts/*.py`) and repository CLI tooling (`init.py`) in **Python 3.11+ using the standard library** (`pathlib`, `argparse`, `subprocess`, `json`, `tempfile`, `shutil`, `re`, `os`) rather than Bash/shell scripts.
   - *Why*: Eliminates macOS BSD vs. Linux GNU tool divergences (`sed`, `grep`, `find`, `mktemp`, `readlink`), enables clean `argparse` flags and structured error handling, and makes every helper directly unit-testable under `pytest`.
   - Keep runtime scripts **stdlib-only** so a freshly cloned machine never needs a virtualenv or `pip install` just to run skills or `init.py` (`pytest` and `ruff` may be configured in `pyproject.toml` for development/testing).
2. **Zero Machine-Specific Hardcoded Paths**:
   - **Never** hardcode `/Users/<username>` or `/home/<username>` in any `.md`, `.py`, or `.json` file.
   - Use `~/` in configuration JSON where supported (e.g. Antigravity's `skills.json`), `$HOME` in shell command examples, `Path.home()` in Python, and relative `./core/...`, `./references/...`, or `./scripts/...` links inside skill directories.
   - When a coordinator/parent skill needs to give a spawned worker an absolute path to a playbook or helper script, instruct it to resolve `<SKILL_DIR>` dynamically from the directory of the `SKILL.md` file it just loaded.
3. **Strict `SKILL.md` Frontmatter Contract**:
   - Every `SKILL.md` must open with YAML frontmatter containing `name` (lowercase `kebab-case`, **matching its parent folder name exactly**) and a clear, third-person `description` stating what the skill does and when to invoke it.
4. **Core Workflow vs. Harness Adapter Separation**:
   - Simple, single-agent skills that only rely on standard shell/file tools belong in **`shared/<skill-name>/`** and are exposed verbatim everywhere.
   - Multi-agent orchestration skills (such as `dev-swarm` and `review`) must split:
     - **Domain & workflow logic (`core/<skill-name>/`)**: 100% harness- and model-agnostic state machines, convergence/escalation caps, persona definitions, domain review playbooks, and Python helper scripts.
     - **Thin harness adapter (`harnesses/<harness>/<skill-name>/SKILL.md`)**: ~60–100 line binding sheet specifying only `Persona <-> Model` pins, concrete agent spawn/kill/message tool bindings, clean-room barrier mechanics for that harness, and a git-tracked relative symlink `core -> ../../../core/<skill-name>`.

---

## 2. Repository Layout

```text
agent-skills/
├── AGENTS.md                          # Repository rules, architecture & authoring guide
├── README.md                          # Concise multi-machine setup & authoring docs
├── pyproject.toml                     # Python 3.11+, pytest & ruff config (stdlib-only runtime)
├── init.py                            # Multi-machine installer, status checker, adopter & linter
├── tests/                             # pytest suite for init.py, doctor, and core helper scripts
├── core/                              # Harness- & model-agnostic workflows, personas, playbooks & scripts
│   ├── dev-swarm/
│   │   ├── algorithm.md               # Exterior macro loop + 4-stage interior state machine
│   │   ├── personas/
│   │   │   ├── researcher.md          # Broad cartography vs. deep mechanistic probe specs
│   │   │   ├── planner-critic.md      # Plan schema, disjoint slice rules, [BLOCKING] critique rubric
│   │   │   ├── coder-reviewer.md      # Worktree rules, zero meta-comment rule, Pass 1 gate vs delta check
│   │   │   └── cleanroom-commit.md    # Context firewall rules, hygiene check, commit writing rules
│   │   └── scripts/
│   │       └── manage_worktree.py     # Isolated worktree create / status / apply / cleanup CLI
│   └── review/
│       ├── algorithm.md               # Right-sized, diff-scoped specialist fan-out + inline verification
│       ├── playbooks/
│       │   ├── correctness-style.md
│       │   ├── security-adversarial.md
│       │   ├── safety-cpp-rust.md
│       │   └── verifier.md
│       └── scripts/
│           └── collect_review_context.py  # Read-only git diff/inventory/C++/unsafe-Rust context collector
├── shared/                            # Universal single-agent skills
│   └── hey/
│       ├── SKILL.md
│       ├── .installed-version
│       └── .managed-by-hey-cli
└── harnesses/                         # Thin harness-specific SKILL.md adapters
    └── agy/                           # Antigravity (active harness; structured for future harnesses)
        ├── dev-swarm/
        │   ├── SKILL.md               # AGY model pins + native `swarm` board bindings -> ./core/...
        │   └── core -> ../../../core/dev-swarm
        └── review/
            ├── SKILL.md               # AGY standalone vs embedded swarm-worker bindings -> ./core/...
            └── core -> ../../../core/review
```

*(Claude Code and Codex adapters are intentionally unpopulated for now—only Antigravity (`agy`) + universal `~/.agents/skills` (`shared/`) are active, while `init.py`'s harness registry stays modular so adding another harness later is trivial.)*

---

## 3. Abstract Interface Between `core/` and `harnesses/<harness>/`

Files under `core/` must **not** hardcode Antigravity-specific tool or model names (`swarm_wait`, `read_board`, `#seq`, `@coordinator`, `Carbon High`, `Neon xhigh`, `Opus 5.5 High`). Instead, `core/*.md` writes against five abstract concepts whose concrete bindings are supplied by the harness's `SKILL.md` (and passed into spawned workers' startup briefs):

1. **`MODEL_MAP`**: Maps abstract personas (`Researcher-Broad`, `Researcher-Deep`, `Planner`, `Critic`, `Coder`, `Reviewer`, `Commit-Reviewer`, `Commit-Writer`, and `/review`'s `Correctness`, `Adversarial`, `Safety`, `Verifier`) to concrete harness model identifiers, with a no-flash-downgrade quota fallback rule.
2. **`SPAWN_WORKER` & Lifecycle**: How the Lead Agent (or Paired Reviewer running `/review`) starts, monitors, model-switches on quota exhaustion, and cleans up workers.
3. **`HANDOFF(recipient, status_tag, payload)` & `READ_ARTIFACT(ref)`**: How workers pass artifacts/verdicts peer-to-peer (`Planner <-> Critic`, `Coder <-> Reviewer`) or escalate to the Lead Agent without re-copying huge payloads.
4. **`CLEANROOM_BARRIER` (Stage 4 Air-Gap)**:
   - *Core invariant*: Neither `Commit Reviewer` nor `Commit Writer` may observe rejected plans, Critic debates, Reviewer rounds, Decision Ledgers, or prior swarm chatter. `Commit Reviewer` sees **only** `(sanitized_problem_statement, staged_git_diff_and_repo)`; `Commit Writer` sees **only** `(implementation_report, staged_git_diff_and_git_log)`.
   - *Antigravity binding*: Spawn Stage 4 workers only after Stage 3 finishes, isolate in `#commit-k` channel (when supported), enforce `MIN_ALLOWED_SEQ = <launch_seq>` floor on `read_board`, and forbid reading `board.jsonl` or `<artifactDir>`.
5. **`DECISION_LEDGER`**: Where the Lead Agent records autonomous engineering/tradeoff calls during execution to present to the human at final handoff (`<artifactDir>/dev_swarm_decisions.md` in Antigravity).

---

## 4. `init.py` CLI Specification

`init.py` is an executable Python 3.11+ stdlib CLI supporting `--home <DIR>` (defaulting to `Path.home()`) on all stateful subcommands so tests never touch the real `$HOME`:

- **`python3 init.py status [--home DIR]`**:
  - Read-only report showing the state of `~/.agents/skills`, `~/.gemini/config/skills.json`, any shadowing same-named directories in `~/.gemini/config/skills/`, and all skills discovered in this repo.
- **`python3 init.py apply [--dry-run] [--replace] [--home DIR]`**:
  1. **`~/.agents/skills` -> `<repo>/shared` symlink**: Creates `~/.agents` if needed and points `~/.agents/skills` at `<repo>/shared` (so `npx skills add ...` installs universal skills straight into `shared/`). If `~/.agents/skills` already exists as a real non-symlink directory:
     - Without `--replace`: report the conflict clearly and do **not** delete or overwrite it.
     - With `--replace`: back up the existing directory safely (e.g., `~/.agents/skills.bak.<timestamp>`) before creating the symlink.
  2. **Antigravity `~/.gemini/config/skills.json` non-destructive merge**:
     - Parses existing `~/.gemini/config/skills.json` if present (preserving `inherits` and unrelated `entries`).
     - Ensures home-relative entries (e.g. `~/projects/agent-skills/shared` and `~/projects/agent-skills/harnesses/agy`, computed relative to `--home` when `<repo>` is under `--home`, else absolute path) are present without duplicates, written atomically via temp file + `os.replace`.
     - Checks `~/.gemini/config/skills/` for loose directories whose names collide with repo skills (`dev-swarm`, `review`, `hey`). Warns clearly if present; if `--replace` is passed, moves colliding directories into `~/.gemini/config/skills.bak.<timestamp>/` so they don't shadow or duplicate `skills.json` entries.
- **`python3 init.py adopt <source-dir> --scope {shared,agy} [--force]`**:
  - Copies a loose skill directory into `shared/<name>` or `harnesses/agy/<name>` (validating `SKILL.md` frontmatter) to streamline future skill development.
- **`python3 init.py doctor`**:
  - Exits non-zero if any check fails across the repo:
    1. Every skill directory under `shared/*` and `harnesses/*/*` has a `SKILL.md` with valid YAML frontmatter (`name == dir.name`, non-empty `description`).
    2. Every git-tracked symlink (e.g. `harnesses/agy/*/core`) and every relative markdown file link (`[label](./path)` or `[label](references/path)`) inside `.md` files resolves to an existing path inside the repository.
    3. Zero hardcoded machine user paths (`/Users/<name>` or `/home/<name>`) appear anywhere in tracked `.md`, `.py`, or `.json` files.
    4. All `.py` files pass `py_compile` syntax validation.

---

## 5. Verification & Test Isolation

- Always run `python3 init.py doctor` and `pytest` before committing changes.
- All stateful tests must execute strictly inside isolated temporary `--home <tmp_path>` directories (enforced automatically by `tests/conftest.py`'s `isolate_home` fixture). Do not run `python3 init.py apply` against the live `$HOME` directory unless explicitly requested by the user.
