# `agent-skills` — Architecture, Style & Authoring Guide

This repository is the single, multi-machine source of truth for Clayton's agent skills across machines (macOS and Linux). It separates **universal/core workflow logic** from **thin harness-specific bindings**, and serves as the home for authoring new skills.

---

## 1. Non-Negotiable Repository Style & Portability Rules

Every skill, helper script, and CLI tool in this repository **must** follow these rules:

1. **Prefer Python 3 Over Bash/Shell Scripts**:
   - Write `init.py` and all core orchestration helper scripts (`core/*/scripts/*.py`) in **Python 3.11+ using the standard library** (`pathlib`, `argparse`, `subprocess`, `json`, `tempfile`, `shutil`, `re`, `os`) rather than Bash/shell scripts.
   - *Why*: Eliminates macOS BSD vs. Linux GNU tool divergences (`sed`, `grep`, `find`, `mktemp`, `readlink`), enables clean `argparse` flags and structured error handling, and makes every helper directly unit-testable under `pytest`.
   - Keep `init.py` and `core/*/scripts/*.py` orchestration helpers **100% stdlib-only** so a freshly cloned machine never needs a virtualenv or `pip install` just to run skills or `init.py` (`pytest` and `ruff` may be configured in `pyproject.toml` for development/testing). Upstream domain or vendor benchmark scripts bundled inside `shared/<skill>/scripts/` (such as MuJoCo flex benchmarks) may import their domain packages.
2. **Zero Machine-Specific Hardcoded Paths**:
   - **Never** hardcode `/Users/<username>` or `/home/<username>` in any tracked `.md`, `.py`, `.json`, or `.toml` file.
   - Use `$HOME` in shell command examples, `Path.home()` in Python, and relative `./core/...`, `./references/...`, or `./scripts/...` links inside skill directories.
   - Note that while tracked repository files never hardcode user home paths, `init.py apply` writes dynamically resolved **absolute `/...` POSIX paths** into machine-local `~/.gemini/config/skills.json` because Antigravity's Go loader (`customization.go:617`) rejects `~` / `~/...` entries in global `skills.json`.
   - When a coordinator/parent skill needs to give a spawned worker an absolute path to a playbook or helper script, instruct it to resolve `<SKILL_DIR>` dynamically from the directory of the `SKILL.md` file it just loaded.
3. **Strict `SKILL.md` Frontmatter Contract**:
   - Every `SKILL.md` must open with YAML frontmatter containing `name` (lowercase `kebab-case`, **matching its parent folder name exactly**) and a clear, third-person `description` stating what the skill does and when to invoke it.
4. **Core Workflow vs. Harness Adapter Separation**:
   - Simple, single-agent skills that only rely on standard shell/file tools belong in **`shared/<skill-name>/`** and are exposed verbatim everywhere.
   - Multi-agent orchestration skills (such as `dev-swarm`, `dev-swarm-zircon`, and `review`) must split:
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
├── global/
│   └── AGENTS.md                      # Portable global rules linked to ~/.gemini/config/AGENTS.md, ~/.claude/CLAUDE.md, and ~/.codex/AGENTS.md
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
│   ├── dev-swarm-zircon/              # Zircon C++-to-Rust porting swarm workflow & helper scripts
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
│   ├── hey/
│   │   ├── SKILL.md
│   │   ├── .installed-version
│   │   └── .managed-by-hey-cli
│   └── mujoco-deformables/            # MuJoCo 1D/2D/3D deformable flex modeling skill
└── harnesses/                         # Thin harness-specific SKILL.md adapters
    ├── agy/                           # Antigravity (skills.json + ~/.gemini/config/AGENTS.md)
    │   ├── dev-swarm/
    │   │   ├── SKILL.md               # AGY model pins + native `swarm` board bindings -> ./core/...
    │   │   └── core -> ../../../core/dev-swarm
    │   ├── dev-swarm-zircon/
    │   │   ├── SKILL.md               # AGY bindings -> ./core/...
    │   │   └── core -> ../../../core/dev-swarm-zircon
    │   └── review/
    │       ├── SKILL.md               # AGY standalone vs embedded swarm-worker bindings -> ./core/...
    │       └── core -> ../../../core/review
    ├── claude/                        # Claude Code (~/.claude/skills/* symlinks + ~/.claude/CLAUDE.md)
    │   ├── dev-swarm/
    │   │   ├── SKILL.md               # Claude model pins + Agent subagent bindings -> ./core/...
    │   │   └── core -> ../../../core/dev-swarm
    │   ├── dev-swarm-zircon/
    │   │   ├── SKILL.md               # Claude Zircon porting bindings -> ./core/...
    │   │   └── core -> ../../../core/dev-swarm-zircon
    │   └── review/
    │       ├── SKILL.md               # Claude subagent review bindings -> ./core/...
    │       └── core -> ../../../core/review
    └── codex/                         # Codex (~/.codex/skills/* symlinks + ~/.codex/AGENTS.md)
        ├── claude-review/             # Codex-only cross-model Claude review wrapper skill
        ├── dev-swarm/
        │   ├── SKILL.md               # Codex model pins + subagent bindings -> ./core/...
        │   └── core -> ../../../core/dev-swarm
        ├── dev-swarm-zircon/
        │   ├── SKILL.md               # Codex Zircon porting bindings -> ./core/...
        │   └── core -> ../../../core/dev-swarm-zircon
        └── review/
            ├── SKILL.md               # Codex subagent review bindings -> ./core/...
            └── core -> ../../../core/review
```

All three harnesses (`agy`, `claude`, `codex`) plus universal `~/.agents/skills` (`shared/`) are active and managed through `init.py`'s `HARNESS_REGISTRY`.

---

## 3. Abstract Interface Between `core/` and `harnesses/<harness>/`

Files under `core/` must **not** hardcode Antigravity-specific tool or model names (`swarm_wait`, `read_board`, `#seq`, `@coordinator`, `Carbon High`, `Neon xhigh`, `Opus 5.5 High`). Instead, `core/*.md` writes against six abstract concepts whose concrete bindings are supplied by the harness's `SKILL.md` (and passed into spawned workers' startup briefs):

1. **`MODEL_MAP`**: Maps abstract personas (`Researcher-Broad`, `Researcher-Deep`, `Planner`, `Critic`, `Coder`, `Reviewer`, `Commit-Reviewer`, `Commit-Writer`, `dev-swarm-zircon`'s `Zircon-Coder`/`Zircon-Coder-Escalation`/`Zircon-Parity-Reviewer`/`Zircon-Safety-Reviewer`, and `/review`'s `Correctness`, `Adversarial`, `Safety`, `Verifier`) to concrete harness model identifiers, with a no-flash-downgrade quota fallback rule.
2. **`SPAWN_WORKER` & Lifecycle**: How the Lead Agent (or Paired Reviewer running `/review`) starts, monitors, model-switches on quota exhaustion, and cleans up workers.
3. **`HANDOFF(recipient, status_tag, payload)` & `READ_ARTIFACT(ref)`**: How workers pass artifacts/verdicts peer-to-peer (`Planner <-> Critic`, `Coder <-> Reviewer`) or escalate to the Lead Agent without re-copying huge payloads.
4. **`CLEANROOM_BARRIER` (Stage 4 / Optional Stage 6 Air-Gap)**:
   - *Core invariant*: Neither `Commit Reviewer` nor `Commit Writer` may observe rejected plans, Critic debates, Reviewer rounds, `DECISION_LEDGER`, `STATE_LEDGER`, `$PROMPTS` briefs/artifacts, or prior swarm chatter. `Commit Reviewer` sees **only** `(sanitized_problem_statement, staged_git_diff_and_repo)`; `Commit Writer` sees **only** `(implementation_report, staged_git_diff_and_git_log)`.
   - *Antigravity binding*: Spawn clean-room commit workers only after coding/review finishes, isolate in `#commit-k` channel (when supported), enforce `MIN_ALLOWED_SEQ = <launch_seq>` floor on `read_board`, and forbid reading `board.jsonl`, `$PROMPTS`, or `<artifactDir>`.
5. **`DECISION_LEDGER`**: Where the Lead Agent records autonomous engineering/tradeoff calls during execution to present to the human at final handoff (`<artifactDir>/dev_swarm_decisions.md` for `dev-swarm` and `<artifactDir>/dev_swarm_zircon_decisions.md` for `dev-swarm-zircon` in Antigravity).
6. **`STATE_LEDGER`**: Compaction-safe per-session checkpoint tracking the active queue/unit, iteration and stage, active worker names, worktree/workspace paths, Step 0 pre-existing dirty paths, artifact refs, and board cursor (`<artifactDir>/dev_swarm_state.md` for `dev-swarm` and `$PROMPTS/state.md` for `dev-swarm-zircon` in Antigravity), strictly excluded from `CLEANROOM_BARRIER`.

---

## 4. `init.py` CLI Specification

`init.py` is an executable Python 3.11+ stdlib CLI supporting `--home <DIR>` (defaulting to `Path.home()`) on all stateful subcommands so tests never touch the real `$HOME`:

- **`python3 init.py status [--home DIR]`**:
  - Read-only report showing the state of `~/.agents/skills` (including unmigrated or differing local skills when it is a real directory), Antigravity (`~/.gemini/config/skills.json` with non-absolute `~/...` normalization checks, `~/.gemini/config/AGENTS.md`, any shadowing same-named directories in `~/.gemini/config/skills/`, and any shadowing non-marketplace local plugins in `~/.gemini/config/plugins/`), Claude Code (`~/.claude/skills/` per-skill symlink farm and `~/.claude/CLAUDE.md`), Codex (`~/.codex/skills/` per-skill symlink farm and `~/.codex/AGENTS.md`), and all skills discovered in this repo.
- **`python3 init.py apply [--dry-run] [--replace] [--no-global-agents | --skip-global-agents] [--home DIR]`**:
  1. **`~/.agents/skills` -> `<repo>/shared` symlink**: Creates `~/.agents` if needed and points `~/.agents/skills` at `<repo>/shared` (so `npx skills add ...` installs universal skills straight into `shared/`). If `~/.agents/skills` already exists as a real non-symlink directory:
     - Without `--replace`: report the conflict clearly and do **not** delete or overwrite it.
     - With `--replace`: warn about any unmigrated skills or local skills whose trees differ from `shared/`, then back up the existing directory safely (e.g., `~/.agents/skills.bak.<timestamp>`) before creating the symlink.
  2. **Antigravity `~/.gemini/config/skills.json` non-destructive merge**:
     - Parses existing `~/.gemini/config/skills.json` if present (preserving `inherits`, filter fields, and unrelated `entries`).
     - Writes dynamically resolved **absolute `/...` POSIX paths** (`<repo>/shared` and `<repo>/harnesses/agy`) because Antigravity's Go loader (`customization.go:617`) rejects `~` / `~/...` entries in global `skills.json`.
     - Normalizes any legacy `~` / `~/...` entry paths to their resolved absolute POSIX paths and deduplicates colliding entries with identical filter/metadata keys while preserving order, written atomically via temp file + `os.replace`.
  3. **Claude Code & Codex per-skill symlink farms (`~/.claude/skills/*` and `~/.codex/skills/*`)**:
     - Exposes both `shared/<name>` and `harnesses/<harness>/<name>` side-by-side via per-skill symlinks inside `~/.claude/skills/<name>` and `~/.codex/skills/<name>` (refusing duplicate or invalid skills without disturbing existing user entries).
     - Preserves hidden entries (such as Codex's `.system/`), Claude Code's `synced/` directory, and unrelated user skills or external symlinks untouched.
     - Prunes stale repo-owned symlinks pointing inside `<repo>` that are no longer desired for that harness.
     - Reports `CONFLICT` without `--replace` if a desired skill path (or `~/.<harness>/skills` itself) already exists; with `--replace`, backs up conflicting entries into `~/.<harness>/skills.bak.<timestamp>/` (or backs up a conflicting container file/symlink) before creating symlinks.
  4. **Global instruction links (`~/.gemini/config/AGENTS.md`, `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md` -> `<repo>/global/AGENTS.md`)**:
     - Unless `--no-global-agents` (alias `--skip-global-agents`) is passed, links all three global instruction files to `<repo>/global/AGENTS.md` (backing up an existing conflict to `<name>.bak.<timestamp>` under `--replace`).
     - When `--no-global-agents` / `--skip-global-agents` is passed, leaves all three global instruction files completely untouched even under `--replace`.
  5. **Antigravity shadowing detection & backup (`~/.gemini/config/skills/` and `~/.gemini/config/plugins/`)**:
     - Checks `~/.gemini/config/skills/` for loose directories whose names collide with repo skills. Warns clearly if present; if `--replace` is passed, moves colliding directories into `~/.gemini/config/skills.bak.<timestamp>/` after verifying `skills.json` exposes valid replacement skills.
     - Checks `~/.gemini/config/plugins/` for non-marketplace local plugins (skipping plugins whose `installedFrom` field in `~/.gemini/config/config.json` is truthy) whose `skills/` subdirectories shadow repo skills. Warns without `--replace`; with `--replace`, refuses to move any plugin containing unmigrated skills not in the repo or non-skill runtime components (`mcp_config.json`, `hooks.json`, `hooks`, `sidecars`, `sidecars.json`), and backs up fully migrated shadowing plugins into `~/.gemini/config/plugins.bak.<timestamp>/`.
- **`python3 init.py adopt <source-dir> --scope {shared,agy,claude,codex} [--force]`**:
  - Copies a loose skill directory into `shared/<name>` or `harnesses/<harness>/<name>` (validating `SKILL.md` frontmatter and rejecting cross-scope duplicates or reserved harness entry names such as `synced` in `shared`/`claude`) to streamline future skill development.
- **`python3 init.py doctor`**:
  - Exits non-zero if any check fails across the repo:
    1. Every skill directory under `shared/*` and `harnesses/*/*` has a `SKILL.md` with valid YAML frontmatter (`name == dir.name`, non-empty `description`), no duplicates between `shared/` and a harness, and no reserved harness entry names (such as `synced` in `shared/` or `harnesses/claude/`).
    2. `global/AGENTS.md` exists inside a non-symlink `global/` directory as a non-symlink, non-empty UTF-8 regular file `<= 24,000` bytes.
    3. Every git-tracked symlink (e.g. `harnesses/agy/*/core`) and every relative markdown file link (`[label](./path)` or `[label](references/path)`) inside `.md` files resolves to an existing path inside the repository.
    4. Zero hardcoded machine user paths (`/Users/<name>` or `/home/<name>`) appear anywhere in tracked `.md`, `.py`, `.json`, or `.toml` files.
    5. All `.py` files pass `py_compile` syntax validation.

---

## 5. Verification & Test Isolation

- Always run `python3 init.py doctor` and `pytest` before committing changes.
- All stateful tests must execute strictly inside isolated temporary `--home <tmp_path>` directories (enforced automatically by `tests/conftest.py`'s `isolate_home` fixture). Do not run `python3 init.py apply` against the live `$HOME` directory unless explicitly requested by the user.
