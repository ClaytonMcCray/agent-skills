# `agent-skills`

Single, multi-machine source of truth for agent skills across macOS and Linux. See
[AGENTS.md](./AGENTS.md) for architecture, portability rules, and skill authoring contracts.

## Repository Layout

- `global/` — Machine-portable global agent rules ([global/AGENTS.md](./global/AGENTS.md)), linked to
  `~/.gemini/config/AGENTS.md` by `init.py apply` (unless `--no-global-agents` is passed).
- `shared/` — Universal single-agent skills exposed directly through `~/.agents/skills` (for example,
  [shared/hey/SKILL.md](./shared/hey/SKILL.md) and
  [shared/mujoco-deformables/SKILL.md](./shared/mujoco-deformables/SKILL.md)).
- `core/` — Harness- and model-agnostic orchestration workflows, personas, domain review playbooks,
  and stdlib-only Python helper scripts:
  - [core/review/](./core/review/) (context collector:
    [collect_review_context.py](./core/review/scripts/collect_review_context.py))
  - [core/dev-swarm/](./core/dev-swarm/) (worktree manager:
    [manage_worktree.py](./core/dev-swarm/scripts/manage_worktree.py))
  - [core/dev-swarm-zircon/](./core/dev-swarm-zircon/) (BabelDiff wrapper:
    [run_babeldiff.py](./core/dev-swarm-zircon/scripts/run_babeldiff.py))
- `harnesses/agy/` — Thin Antigravity adapters binding concrete models and native tools to `core/`
  via relative symlinks:
  - [harnesses/agy/review/SKILL.md](./harnesses/agy/review/SKILL.md)
  - [harnesses/agy/dev-swarm/SKILL.md](./harnesses/agy/dev-swarm/SKILL.md)
  - [harnesses/agy/dev-swarm-zircon/SKILL.md](./harnesses/agy/dev-swarm-zircon/SKILL.md)
- [init.py](./init.py) — Stdlib-only Python CLI for inspecting status, applying machine bindings,
  adopting loose skills, and running repository integrity checks.
- [pyproject.toml](./pyproject.toml) — Python 3.11+ project metadata and `pytest`/`ruff` config.

## Included Skills

| Skill | Scope | Entry Point | Purpose |
| :--- | :--- | :--- | :--- |
| `hey` | Universal (`shared/`) | [shared/hey/SKILL.md](./shared/hey/SKILL.md) | Interact with HEY email, Feed, Paper Trail, and calendar via `hey` CLI |
| `mujoco-deformables` | Universal (`shared/`) | [shared/mujoco-deformables/SKILL.md](./shared/mujoco-deformables/SKILL.md) | Model, tune, and attach 1D/2D/3D deformable flex bodies in MuJoCo |
| `review` | Antigravity (`harnesses/agy/`) | [harnesses/agy/review/SKILL.md](./harnesses/agy/review/SKILL.md) | Multi-agent security, adversarial, C++/Rust safety, correctness, and DRY code review |
| `dev-swarm` | Antigravity (`harnesses/agy/`) | [harnesses/agy/dev-swarm/SKILL.md](./harnesses/agy/dev-swarm/SKILL.md) | Autonomous local research -> plan/critic -> worktree code/review -> clean-room commit swarm |
| `dev-swarm-zircon` | Antigravity (`harnesses/agy/`) | [harnesses/agy/dev-swarm-zircon/SKILL.md](./harnesses/agy/dev-swarm-zircon/SKILL.md) | Zircon C++-to-Rust porting swarm with parity doctrine and BabelDiff verification |

## Multi-Machine Setup

Clone this repository on any machine and inspect the current state before making changes:

```bash
python3 init.py status
```

Preview planned changes without touching the filesystem:

```bash
python3 init.py apply --dry-run
```

Apply the universal skills symlink (`~/.agents/skills -> <repo>/shared`), global Antigravity rules
symlink (`~/.gemini/config/AGENTS.md -> <repo>/global/AGENTS.md`), and non-destructively merge
Antigravity's `~/.gemini/config/skills.json` using resolved absolute `/...` POSIX paths (normalizing
and deduplicating legacy `~/...` entries that Antigravity's Go loader rejects):

```bash
python3 init.py apply
```

To leave `~/.gemini/config/AGENTS.md` untouched (even under `--replace`), pass `--no-global-agents`
(alias `--skip-global-agents`):

```bash
python3 init.py apply --no-global-agents
```

If `~/.agents/skills` or `~/.gemini/config/AGENTS.md` already exists, or if `~/.gemini/config/skills/`
or `~/.gemini/config/plugins/` contains non-marketplace local copies that shadow repository skills,
back them up automatically before switching over:

```bash
python3 init.py apply --replace
```

> **Safety**: `init.py apply` never overwrites malformed `skills.json` files, preserves valid
> symlinked `skills.json` targets, warns when local `~/.agents/skills/<name>` trees differ from or
> are absent in `shared/`, skips official marketplace plugins (`installedFrom` in `config.json`),
> refuses to move shadowing local plugins that contain unmigrated skills not in this repository or
> non-skill runtime components (`mcp_config.json`, `hooks.json`, `sidecars`), and refuses to move
> shadowing directories unless `skills.json` updated cleanly and the replacement repository skills
> pass validation.

## Installing & Authoring Skills

### Universal Skills (`npx skills add`)

Once `init.py apply` has linked `~/.agents/skills` to `<repo>/shared`, installing universal skills
via `npx skills add <package>` writes them straight into `shared/` for easy review and git commit.

### Adopting Loose Skills

Import an existing skill directory into `shared/` or `harnesses/agy/` with frontmatter validation:

```bash
python3 init.py adopt "$HOME/path/to/my-skill" --scope shared
python3 init.py adopt "$HOME/path/to/my-agy-skill" --scope agy --force
```

### Authoring Checklist

1. Give every skill a lowercase `kebab-case` folder name matching `name` in `SKILL.md` frontmatter,
   plus a non-empty third-person `description`.
2. Write `init.py` and `core/*/scripts/*.py` orchestration helpers in stdlib-only Python 3.11+ (never
   Bash) with zero hardcoded user paths (vendor/domain scripts under `shared/<skill>/scripts/` may
   import their domain packages).
3. Run the repository doctor and test suite before committing.

## Verification & Development

Validate skill frontmatter, `global/AGENTS.md` (non-symlink UTF-8 regular file `<= 24,000` bytes),
relative symlinks, markdown links, hardcoded user paths, and Python syntax:

```bash
python3 init.py doctor
```

Run the unit and integration test suite (all stateful tests execute in isolated temporary home
directories and never modify `$HOME`):

```bash
pytest -v
```
