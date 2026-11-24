# `agent-skills`

Single, multi-machine source of truth for agent skills across macOS and Linux. See
[AGENTS.md](./AGENTS.md) for architecture, portability rules, and skill authoring contracts.

## Repository Layout

- `shared/` — Universal single-agent skills exposed directly through `~/.agents/skills` (for example,
  [shared/hey/SKILL.md](./shared/hey/SKILL.md)).
- `core/` — Harness- and model-agnostic orchestration workflows, personas, domain review playbooks,
  and stdlib-only Python helper scripts (such as `core/review/` and `core/dev-swarm/`).
- `harnesses/agy/` — Thin Antigravity adapters binding concrete models and native tools to `core/`
  via relative symlinks (such as `harnesses/agy/review/` and `harnesses/agy/dev-swarm/`).
- [init.py](./init.py) — Stdlib-only Python CLI for inspecting status, applying machine bindings,
  adopting loose skills, and running repository integrity checks.
- [pyproject.toml](./pyproject.toml) — Python 3.11+ project metadata and `pytest`/`ruff` config.

## Multi-Machine Setup

Clone this repository on any machine and inspect the current state before making changes:

```bash
python3 init.py status
```

Preview planned changes without touching the filesystem:

```bash
python3 init.py apply --dry-run
```

Apply the symlink (`~/.agents/skills -> <repo>/shared`) and non-destructively merge Antigravity's
`~/.gemini/config/skills.json`:

```bash
python3 init.py apply
```

If `~/.agents/skills` already exists as a directory or `~/.gemini/config/skills/` contains loose
copies that shadow repository skills, back them up automatically before switching over:

```bash
python3 init.py apply --replace
```

> **Safety**: `init.py apply` never overwrites malformed `skills.json` files, preserves valid
> symlinked `skills.json` targets, and refuses to move shadowing directories in
> `~/.gemini/config/skills/` unless `skills.json` updated cleanly and the replacement skill in this
> repository passes validation.

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
2. Write all helper scripts in stdlib-only Python 3.11+ (never Bash) with zero hardcoded user paths.
3. Run the repository doctor and test suite before committing.

## Verification & Development

Validate skill frontmatter, relative symlinks, markdown links, hardcoded user paths, and Python
syntax:

```bash
python3 init.py doctor
```

Run the unit and integration test suite (all stateful tests execute in isolated temporary home
directories and never modify `$HOME`):

```bash
pytest -v
```
