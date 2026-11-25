"""Unit and integration tests for init.py and repository integrity."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

import init

REAL_REPO = Path(__file__).resolve().parent.parent


def _write_skill(
    skill_dir: Path,
    *,
    name: str | None = None,
    description: str = "Sample test skill description.",
    extra_frontmatter: str = "",
    body: str = "# Skill\n",
) -> Path:
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_name = skill_dir.name if name is None else name
    fm = f"---\nname: {skill_name}\ndescription: {description}\n"
    if extra_frontmatter:
        fm += extra_frontmatter.rstrip("\n") + "\n"
    fm += "---\n\n" + body
    (skill_dir / "SKILL.md").write_text(fm, encoding="utf-8")
    return skill_dir


def _make_fake_repo(
    repo_dir: Path,
    *,
    include_agy_skills: bool = True,
    include_global_agents: bool = True,
) -> Path:
    _write_skill(repo_dir / "shared" / "hey", description="Interact with HEY email.")
    if include_agy_skills:
        _write_skill(
            repo_dir / "harnesses" / "agy" / "review",
            description="Run a thorough multi-agent code review.",
        )
        _write_skill(
            repo_dir / "harnesses" / "agy" / "dev-swarm",
            description="Run an autonomous multi-agent development swarm.",
        )
    if include_global_agents:
        global_dir = repo_dir / "global"
        global_dir.mkdir(parents=True, exist_ok=True)
        (global_dir / "AGENTS.md").write_text("# Global Agent Rules\n", encoding="utf-8")
    return repo_dir


def _run_init_subprocess(
    args: list[str],
    *,
    home_dir: Path,
    executable: str = sys.executable,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(home_dir / ".config")
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return subprocess.run(
        [executable, str(REAL_REPO / "init.py"), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _snapshot_tree(root: Path) -> list[tuple[str, str, bytes | str]]:
    """Capture a deterministic snapshot of every file, dir, and symlink under root."""
    if not root.exists():
        return []
    items: list[tuple[str, str, bytes | str]] = []
    for dirpath_str, dirnames, filenames in os.walk(root, followlinks=False):
        dirpath = Path(dirpath_str)
        dirnames.sort()
        filenames.sort()
        for dirname in list(dirnames):
            full = dirpath / dirname
            rel = full.relative_to(root).as_posix()
            if full.is_symlink():
                items.append((rel, "symlink", os.readlink(full)))
                dirnames.remove(dirname)
            else:
                items.append((rel, "dir", ""))
        for filename in filenames:
            full = dirpath / filename
            rel = full.relative_to(root).as_posix()
            if full.is_symlink():
                items.append((rel, "symlink", os.readlink(full)))
            else:
                items.append((rel, "file", full.read_bytes()))
    return sorted(items)


# ---------------------------------------------------------------------------
# Repository baseline & shared/hey verification
# ---------------------------------------------------------------------------


def test_shared_hey_verbatim_files() -> None:
    hey_dir = REAL_REPO / "shared" / "hey"
    expected = {
        "SKILL.md": (49317, "4e75060369cfa8fc"),
        ".installed-version": (5, "1f4dc096d58f7d21"),
        ".managed-by-hey-cli": (79, "f8032ab559f099f8"),
    }
    assert (hey_dir / ".installed-version").read_text(encoding="utf-8") == "1.5.0"
    actual_files = sorted(p.name for p in hey_dir.iterdir())
    assert actual_files == sorted(expected.keys())
    for filename, (exp_size, exp_prefix) in expected.items():
        raw = (hey_dir / filename).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        assert len(raw) == exp_size
        assert digest.startswith(exp_prefix)
    assert not (hey_dir / ".installed-version").read_bytes().endswith(b"\n")


def test_pytest_collect_only_repo_root_ignores_shared_vendor_scripts() -> None:
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "."],
        cwd=REAL_REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"pytest --collect-only failed:\n{res.stdout}\n{res.stderr}"
    assert "shared/mujoco-deformables" not in res.stdout
    assert "ModuleNotFoundError" not in res.stdout
    assert "ModuleNotFoundError" not in res.stderr


# ---------------------------------------------------------------------------
# status tests
# ---------------------------------------------------------------------------


def test_status_clean_empty_and_nonexistent_home(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _make_fake_repo(tmp_path / "repo", include_agy_skills=False)
    missing_home = tmp_path / "does_not_exist_yet"

    rc = init.main(["--repo", str(repo), "status", "--home", str(missing_home)])
    out = capsys.readouterr().out
    assert rc == 0
    assert not missing_home.exists()
    assert "shared skills (1): hey" in out
    assert "harness 'agy' skills (0): (none)" in out
    assert "not installed" in out
    assert "not present" in out
    assert "no shadowing collisions" in out


def test_status_omitted_home_uses_isolated_default(
    tmp_path: Path, isolate_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _make_fake_repo(tmp_path / "repo")
    rc = init.main(["--repo", str(repo), "status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert str(isolate_home) in out


def test_status_configured_and_symlinked_skills_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    repo = _make_fake_repo(home / "projects" / "agent-skills")
    assert init.main(["--repo", str(repo), "apply", "--home", str(home)]) == 0
    capsys.readouterr()

    skills_json = home / ".gemini" / "config" / "skills.json"
    real_target = tmp_path / "dotfiles" / "skills.json"
    real_target.parent.mkdir(parents=True)
    real_target.write_bytes(skills_json.read_bytes())
    skills_json.unlink()
    skills_json.symlink_to(real_target)

    rc = init.main(["status", "--repo", str(repo), "--home", str(home)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ok (symlink ->" in out
    assert "symlink ->" in out and ": configured" in out
    assert "no shadowing collisions" in out


def test_status_conflicts_and_selective_shadowing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    repo = _make_fake_repo(tmp_path / "external_repo")
    (home / ".agents" / "skills").mkdir(parents=True)

    config_dir = home / ".gemini" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "skills.json").symlink_to(tmp_path / "does_not_exist.json")

    _write_skill(config_dir / "skills" / "hey", description="Old local hey.")
    _write_skill(config_dir / "skills" / "my-private-skill", description="Private skill.")

    rc = init.main(["--repo", str(repo), "status", "--home", str(home)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "conflict (real directory" in out
    assert "invalid (broken or circular symlink)" in out
    assert "shadowing repo skills (hey)" in out
    assert "my-private-skill" not in out


def test_status_handles_circular_agents_and_symlinked_agy_skills(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    repo = _make_fake_repo(tmp_path / "repo")
    agents_dir = home / ".agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "skills").symlink_to("loop_b")
    (agents_dir / "loop_b").symlink_to("skills")

    config_dir = home / ".gemini" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "skills").symlink_to(repo / "shared")

    rc = init.main(["--repo", str(repo), "status", "--home", str(home)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "conflict (broken or circular symlink" in out
    assert "not traversed for shadowing" in out


def test_status_surfaces_regex_filtered_skills_and_bad_or_unreadable_ancestors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    repo = _make_fake_repo(home / "projects" / "agent-skills")
    config_dir = home / ".gemini" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "skills.json").write_text(
        json.dumps(
            {
                "exclude": ["hey"],
                "entries": [
                    {"path": "~/projects/agent-skills/shared"},
                    {
                        "path": "~/projects/agent-skills/harnesses/agy",
                        "exclude": ["dev-.*"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    rc = init.main(["--repo", str(repo), "status", "--home", str(home)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "needs path normalization" in out
    assert "filtered out by include_only/exclude: dev-swarm, hey" in out

    bad_home = tmp_path / "bad_home"
    bad_home.mkdir()
    (bad_home / ".gemini").write_text("file", encoding="utf-8")
    (bad_home / ".agents").symlink_to(tmp_path / "broken_agents_target")
    rc2 = init.main(["--repo", str(repo), "status", "--home", str(bad_home)])
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert "conflict (broken or circular symlink ancestor" in out2
    assert "invalid (non-directory ancestor" in out2

    if os.geteuid() != 0:
        locked_home = tmp_path / "locked_home"
        (locked_home / ".gemini").mkdir(parents=True)
        os.chmod(locked_home / ".gemini", 0o000)
        try:
            rc3 = init.main(["--repo", str(repo), "status", "--home", str(locked_home)])
            out3 = capsys.readouterr().out
            assert rc3 == 0
            assert "inaccessible ancestor" in out3
        finally:
            os.chmod(locked_home / ".gemini", 0o755)


# ---------------------------------------------------------------------------
# apply tests
# ---------------------------------------------------------------------------


def test_apply_home_relative_nonexistent_home_and_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "new_home"
    repo = _make_fake_repo(tmp_path / "repo_without_agy", include_agy_skills=False)

    rc1 = init.main(["--repo", str(repo), "apply", "--home", str(home)])
    assert rc1 == 0
    assert (home / ".agents" / "skills").is_symlink()
    assert (home / ".agents" / "skills").resolve() == (repo / "shared").resolve()

    skills_json = home / ".gemini" / "config" / "skills.json"
    data = json.loads(skills_json.read_text(encoding="utf-8"))
    assert data["entries"] == [
        {"path": (repo / "shared").resolve().as_posix()},
        {"path": (repo / "harnesses" / "agy").resolve().as_posix()},
    ]

    rel_home = tmp_path / "rel_home"
    rel_repo = _make_fake_repo(rel_home / "projects" / "agent-skills")
    assert init.main(["--repo", str(rel_repo), "apply", "--home", str(rel_home)]) == 0
    rel_json = rel_home / ".gemini" / "config" / "skills.json"
    assert json.loads(rel_json.read_text(encoding="utf-8")) == {
        "entries": [
            {"path": (rel_repo / "shared").resolve().as_posix()},
            {"path": (rel_repo / "harnesses" / "agy").resolve().as_posix()},
        ]
    }
    before_text = rel_json.read_text(encoding="utf-8")
    assert init.main(["--repo", str(rel_repo), "apply", "--home", str(rel_home)]) == 0
    assert rel_json.read_text(encoding="utf-8") == before_text


def test_apply_non_destructive_skills_json_merge_utf8_and_double_slash_tilde(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    repo = _make_fake_repo(home / "projects" / "agent-skills")
    skills_json = home / ".gemini" / "config" / "skills.json"
    skills_json.parent.mkdir(parents=True)

    existing = {
        "inherits": [{"path": "~/other/base-skills.json"}],
        "note": "café — 日本語",
        "entries": [
            {"path": "~/custom/skills", "include_only": ["foo"]},
            {
                "path": "~//projects/agent-skills/shared/",
                "exclude": ["beta"],
                "extra": True,
            },
        ],
    }
    skills_json.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")

    rc = init.main(["--repo", str(repo), "apply", "--home", str(home)])
    assert rc == 0

    raw_text = skills_json.read_text(encoding="utf-8")
    assert "café — 日本語" in raw_text
    merged = json.loads(raw_text)
    assert merged["inherits"] == existing["inherits"]
    assert merged["note"] == "café — 日本語"
    assert len(merged["entries"]) == 3
    assert merged["entries"][0] == {
        "path": (home / "custom" / "skills").resolve().as_posix(),
        "include_only": ["foo"],
    }
    assert merged["entries"][1] == {
        "path": (repo / "shared").resolve().as_posix(),
        "exclude": ["beta"],
        "extra": True,
    }
    assert merged["entries"][2] == {"path": (repo / "harnesses" / "agy").resolve().as_posix()}


def test_apply_preserves_valid_symlinked_skills_json(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = _make_fake_repo(home / "projects" / "agent-skills")
    dotfiles_target = tmp_path / "dotfiles" / "agy_skills.json"
    dotfiles_target.parent.mkdir(parents=True)
    dotfiles_target.write_text(json.dumps({"inherits": [], "entries": []}), encoding="utf-8")
    os.chmod(dotfiles_target, 0o600)

    skills_json = home / ".gemini" / "config" / "skills.json"
    skills_json.parent.mkdir(parents=True)
    skills_json.symlink_to(dotfiles_target)

    rc = init.main(["--repo", str(repo), "apply", "--home", str(home)])
    assert rc == 0
    assert skills_json.is_symlink()
    assert skills_json.resolve() == dotfiles_target.resolve()
    assert stat.S_IMODE(dotfiles_target.stat().st_mode) == 0o600

    merged = json.loads(dotfiles_target.read_text(encoding="utf-8"))
    assert [e["path"] for e in merged["entries"]] == [
        (repo / "shared").resolve().as_posix(),
        (repo / "harnesses" / "agy").resolve().as_posix(),
    ]


@pytest.mark.parametrize("kind", ["broken", "loop", "dir", "real_dir"])
def test_apply_refuses_to_clobber_invalid_skills_json_targets(
    tmp_path: Path, kind: str
) -> None:
    home = tmp_path / "home"
    repo = _make_fake_repo(home / "projects" / "agent-skills")
    config_dir = home / ".gemini" / "config"
    config_dir.mkdir(parents=True)
    skills_json = config_dir / "skills.json"

    if kind == "broken":
        skills_json.symlink_to(tmp_path / "missing.json")
    elif kind == "loop":
        skills_json.symlink_to("other.json")
        (config_dir / "other.json").symlink_to("skills.json")
    elif kind == "dir":
        target_dir = tmp_path / "some_dir"
        target_dir.mkdir()
        skills_json.symlink_to(target_dir)
    else:
        skills_json.mkdir()

    rc = init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home)])
    assert rc == 1
    if kind == "real_dir":
        assert skills_json.is_dir()
    else:
        assert skills_json.is_symlink()


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        '{"entries": "not-a-list"}',
        '{"entries": [{"no_path": 1}]}',
        '{"entries": [{"path": "~/foo\\u0000bar"}]}',
        '{"entries": [{"path": "~/a", "exclude": "not-a-list"}]}',
        '{"entries": [{"path": "~/a", "include_only": [123]}]}',
        '{"exclude": "not-a-list", "entries": []}',
        '{"inherits": "not-a-list", "entries": []}',
    ],
)
def test_load_skills_json_rejects_malformed_schemas(tmp_path: Path, payload: str) -> None:
    target = tmp_path / "skills.json"
    target.write_text(payload, encoding="utf-8")
    data, err = init._load_skills_json(target)
    assert data is None
    assert err is not None


def test_apply_dry_run_and_dry_run_replace_mutate_nothing(tmp_path: Path) -> None:
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    external_repo = _make_fake_repo(tmp_path / "external_repo")
    before_empty = _snapshot_tree(empty_home)
    assert init.main(
        ["--repo", str(external_repo), "apply", "--dry-run", "--home", str(empty_home)]
    ) == 0
    assert _snapshot_tree(empty_home) == before_empty

    home = tmp_path / "home"
    repo = _make_fake_repo(home / "projects" / "agent-skills")
    _write_skill(home / ".agents" / "skills" / "hey", description="Existing hey.")
    _write_skill(home / ".agents" / "skills" / "extra", description="Unmigrated local skill.")
    config_dir = home / ".gemini" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "skills.json").write_text('{"entries": []}\n', encoding="utf-8")
    _write_skill(config_dir / "skills" / "hey", description="Shadowing hey.")
    _write_skill(config_dir / "skills" / "review", description="Shadowing review.")
    _write_skill(config_dir / "skills" / "private", description="Unrelated skill.")

    before_populated = _snapshot_tree(home)
    rc = init.main(["--repo", str(repo), "apply", "--dry-run", "--replace", "--home", str(home)])
    assert rc == 0
    assert _snapshot_tree(home) == before_populated


def test_apply_dry_run_and_live_agree_on_broken_ancestors_and_bad_repo_targets(
    tmp_path: Path,
) -> None:
    repo = _make_fake_repo(tmp_path / "repo")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".agents").symlink_to(tmp_path / "missing_agents")
    (home / ".gemini").write_text("not-a-dir", encoding="utf-8")

    assert init.main(["--repo", str(repo), "apply", "--dry-run", "--home", str(home)]) == 1
    assert init.main(["--repo", str(repo), "apply", "--home", str(home)]) == 1
    assert init.main(
        ["--repo", str(repo), "apply", "--dry-run", "--replace", "--home", str(home)]
    ) == 1

    # Regular file at repo/harnesses must fail apply cleanly (not write impossible entry)
    bad_repo = _make_fake_repo(tmp_path / "bad_repo", include_agy_skills=False)
    (bad_repo / "harnesses").write_text("not-a-dir", encoding="utf-8")
    clean_home = tmp_path / "clean_home"
    clean_home.mkdir()
    assert init.main(["--repo", str(bad_repo), "apply", "--home", str(clean_home)]) == 1
    assert not (clean_home / ".gemini" / "config" / "skills.json").exists()


def test_apply_rejects_empty_nul_unknown_tilde_and_file_cli_paths(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path / "repo")
    reg_file = tmp_path / "file.txt"
    reg_file.write_text("x", encoding="utf-8")

    assert init.main(["--repo", "", "doctor"]) == 1
    assert init.main(["--repo", "   ", "status", "--home", str(tmp_path)]) == 1
    assert init.main(["--repo", str(repo), "status", "--home", ""]) == 1
    assert init.main(["--repo", str(repo), "apply", "--home", ""]) == 1
    assert init.main(["--repo", str(repo), "apply", "--home", str(reg_file)]) == 1
    assert init.main(["--repo", str(repo), "status", "--home", str(reg_file)]) == 1
    assert init.main(["--repo", str(repo), "status", "--home", "a\x00b"]) == 1
    assert init.main(["--repo", str(repo), "adopt", "", "--scope", "shared"]) == 1
    assert init.main(["--repo", str(repo), "adopt", "   ", "--scope", "shared"]) == 1
    assert init.main(["--repo", str(repo), "adopt", "a\x00b", "--scope", "shared"]) == 1
    assert init.main(
        ["--repo", str(repo), "status", "--home", "~definitely_no_such_user_xyz_99999/sub"]
    ) == 1


def test_apply_refuses_repo_overlap_and_symlinked_or_file_agy_skills_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 1. Repo cloned inside ~/.agents/skills/agent-skills must refuse --replace (dry-run & live)
    home_a = tmp_path / "home_a"
    repo_a = _make_fake_repo(home_a / ".agents" / "skills" / "agent-skills")
    assert init.main(
        ["--repo", str(repo_a), "apply", "--dry-run", "--replace", "--home", str(home_a)]
    ) == 1
    assert init.main(
        ["--repo", str(repo_a), "apply", "--replace", "--home", str(home_a)]
    ) == 1
    assert (repo_a / "shared" / "hey" / "SKILL.md").is_file()

    # 2. ~/.gemini/config symlinked into <repo>/sub must not move in-repo symlinks
    home_b = tmp_path / "home_b"
    repo_b = _make_fake_repo(tmp_path / "repo_b")
    in_repo_cfg = repo_b / "sub_cfg"
    (in_repo_cfg / "skills").mkdir(parents=True)
    (in_repo_cfg / "skills" / "hey").symlink_to("../../shared/hey")
    (home_b / ".gemini").mkdir(parents=True)
    (home_b / ".gemini" / "config").symlink_to(in_repo_cfg)
    assert init.main(["--repo", str(repo_b), "apply", "--replace", "--home", str(home_b)]) == 1
    assert (in_repo_cfg / "skills" / "hey").is_symlink()

    # 3. ~/.gemini/config/skills symlinked directly to <repo>/shared
    home_c = tmp_path / "home_c"
    repo_c = _make_fake_repo(home_c / "projects" / "agent-skills")
    cfg_c = home_c / ".gemini" / "config"
    cfg_c.mkdir(parents=True)
    (cfg_c / "skills").symlink_to(repo_c / "shared")

    rc_warn = init.main(["--repo", str(repo_c), "apply", "--home", str(home_c)])
    assert rc_warn == 0
    assert (repo_c / "shared" / "hey" / "SKILL.md").is_file()

    rc_rep = init.main(["--repo", str(repo_c), "apply", "--replace", "--home", str(home_c)])
    err_rep = capsys.readouterr().err
    assert rc_rep == 1
    assert "refusing to move entries through symlinked directory" in err_rep
    assert (repo_c / "shared" / "hey" / "SKILL.md").is_file()
    assert list(cfg_c.glob("skills.bak.*")) == []

    # 4. Regular file at ~/.gemini/config/skills warns without --replace and errors with --replace
    home_d = tmp_path / "home_d"
    repo_d = _make_fake_repo(home_d / "projects" / "agent-skills")
    cfg_d = home_d / ".gemini" / "config"
    cfg_d.mkdir(parents=True)
    (cfg_d / "skills").write_text("not-a-dir", encoding="utf-8")
    assert init.main(["--repo", str(repo_d), "apply", "--home", str(home_d)]) == 0
    assert init.main(["--repo", str(repo_d), "apply", "--replace", "--home", str(home_d)]) == 1


def test_apply_shadow_warning_only_path_returns_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    repo = _make_fake_repo(home / "projects" / "agent-skills")
    _write_skill(home / ".gemini" / "config" / "skills" / "review", description="Shadow.")

    rc = init.main(["--repo", str(repo), "apply", "--home", str(home)])
    err = capsys.readouterr().err
    assert rc == 0
    assert "WARNING:" in err and "review" in err
    assert (home / ".gemini" / "config" / "skills" / "review" / "SKILL.md").is_file()


def test_apply_replace_moves_symlinks_without_touching_targets(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = _make_fake_repo(home / "projects" / "agent-skills")

    outside_shared = tmp_path / "outside_shared"
    outside_shared.mkdir()
    (outside_shared / "keep.txt").write_text("intact", encoding="utf-8")
    agents_skills = home / ".agents" / "skills"
    agents_skills.parent.mkdir(parents=True)
    agents_skills.symlink_to(outside_shared)

    outside_review = tmp_path / "outside_review"
    _write_skill(outside_review, name="review", description="External review.")
    agy_skills = home / ".gemini" / "config" / "skills"
    agy_skills.mkdir(parents=True)
    (agy_skills / "review").symlink_to(outside_review)

    rc = init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home)])
    assert rc == 0
    assert agents_skills.is_symlink()
    assert agents_skills.resolve() == (repo / "shared").resolve()
    assert (outside_shared / "keep.txt").read_text(encoding="utf-8") == "intact"

    assert not (agy_skills / "review").exists() and not (agy_skills / "review").is_symlink()
    assert (outside_review / "SKILL.md").is_file()
    backups = list((home / ".gemini" / "config").glob("skills.bak.*"))
    assert len(backups) == 1
    assert (backups[0] / "review").is_symlink()


def test_apply_conflict_without_replace_leaves_agents_skills_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    repo = _make_fake_repo(home / "projects" / "agent-skills")
    marker = home / ".agents" / "skills" / "hey" / "marker.txt"
    marker.parent.mkdir(parents=True)
    marker.write_text("keep-me", encoding="utf-8")
    _write_skill(home / ".gemini" / "config" / "skills" / "review", description="Shadow review.")

    rc = init.main(["--repo", str(repo), "apply", "--home", str(home)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "CONFLICT:" in err
    assert "WARNING:" in err
    assert not (home / ".agents" / "skills").is_symlink()
    assert marker.read_text(encoding="utf-8") == "keep-me"
    assert (home / ".gemini" / "config" / "skills" / "review").is_dir()
    assert (home / ".gemini" / "config" / "skills.json").is_file()


def test_apply_replace_backups_unmigrated_warning_and_single_agy_backup_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    repo = _make_fake_repo(home / "projects" / "agent-skills")

    _write_skill(home / ".agents" / "skills" / "hey", description="Old hey.")
    _write_skill(home / ".agents" / "skills" / "custom-tool", description="Unmigrated tool.")
    (home / ".agents" / "skills" / "broken-extra").symlink_to(tmp_path / "nowhere")

    agy_skills = home / ".gemini" / "config" / "skills"
    _write_skill(agy_skills / "hey", description="Shadow hey.")
    _write_skill(agy_skills / "review", description="Shadow review.")
    _write_skill(agy_skills / "my-local-only", description="Keep untouched.")

    class FixedDateTime:
        @classmethod
        def now(cls) -> FixedDateTime:
            return cls()

        def strftime(self, _fmt: str) -> str:
            return "20261124-120000"

    monkeypatch.setattr(init, "datetime", FixedDateTime)
    (home / ".agents" / "skills.bak.20261124-120000").mkdir(parents=True)
    (home / ".gemini" / "config" / "skills.bak.20261124-120000").mkdir(parents=True)

    rc = init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home)])
    err = capsys.readouterr().err
    assert rc == 0
    assert "unmigrated skills" in err and "custom-tool" in err and "broken-extra" in err

    agents_bak = home / ".agents" / "skills.bak.20261124-120000-1"
    assert (agents_bak / "custom-tool" / "SKILL.md").is_file()
    assert (home / ".agents" / "skills").is_symlink()
    assert (home / ".agents" / "skills").resolve() == (repo / "shared").resolve()

    agy_backups = sorted((home / ".gemini" / "config").glob("skills.bak.20261124-120000*"))
    assert [p.name for p in agy_backups] == [
        "skills.bak.20261124-120000",
        "skills.bak.20261124-120000-1",
    ]
    moved_dir = home / ".gemini" / "config" / "skills.bak.20261124-120000-1"
    assert (moved_dir / "hey" / "SKILL.md").is_file()
    assert (moved_dir / "review" / "SKILL.md").is_file()
    assert not (agy_skills / "hey").exists()
    assert not (agy_skills / "review").exists()
    assert (agy_skills / "my-local-only" / "SKILL.md").is_file()


def test_apply_non_stranding_guards_malformed_regex_filter_dup_and_broken_adapter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Guard 1: malformed skills.json prevents moving shadowing skills
    home1 = tmp_path / "home1"
    repo1 = _make_fake_repo(home1 / "projects" / "agent-skills")
    config1 = home1 / ".gemini" / "config"
    config1.mkdir(parents=True)
    (config1 / "skills.json").write_text("{not valid json", encoding="utf-8")
    _write_skill(config1 / "skills" / "review", description="Live review.")

    assert init.main(["--repo", str(repo1), "apply", "--replace", "--home", str(home1)]) == 1
    assert (config1 / "skills.json").read_text(encoding="utf-8") == "{not valid json"
    assert (config1 / "skills" / "review" / "SKILL.md").is_file()
    assert list(config1.glob("skills.bak.*")) == []

    # Guard 2: regex entry exclude AND top-level exclude hide replacement skills
    home2 = tmp_path / "home2"
    repo2 = _make_fake_repo(home2 / "projects" / "agent-skills")
    config2 = home2 / ".gemini" / "config"
    config2.mkdir(parents=True)
    (config2 / "skills.json").write_text(
        json.dumps(
            {
                "exclude": ["review"],
                "entries": [
                    {"path": "~/projects/agent-skills/shared"},
                    {
                        "path": "~/projects/agent-skills/harnesses/agy",
                        "exclude": ["dev-.*"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    _write_skill(config2 / "skills" / "review", description="Live review.")
    _write_skill(config2 / "skills" / "dev-swarm", description="Live dev-swarm.")
    rc2 = init.main(["--repo", str(repo2), "apply", "--replace", "--home", str(home2)])
    err2 = capsys.readouterr().err
    assert rc2 == 1
    assert "filters out 'review'" in err2 and "filters out 'dev-swarm'" in err2
    assert (config2 / "skills" / "review" / "SKILL.md").is_file()
    assert (config2 / "skills" / "dev-swarm" / "SKILL.md").is_file()

    # Guard 3: invalid repo SKILL.md, broken adapter symlink, or cross-scope duplicate
    home3 = tmp_path / "home3"
    repo3 = _make_fake_repo(home3 / "projects" / "agent-skills")
    (repo3 / "harnesses" / "agy" / "review" / "SKILL.md").write_text(
        "---\nname: review\ndescription:\n---\n", encoding="utf-8"
    )
    (repo3 / "harnesses" / "agy" / "dev-swarm" / "core").symlink_to("../../../core/missing")
    _write_skill(repo3 / "harnesses" / "agy" / "hey", description="Duplicate hey.")
    agy3 = home3 / ".gemini" / "config" / "skills"
    _write_skill(agy3 / "review", description="Live review.")
    _write_skill(agy3 / "dev-swarm", description="Live dev-swarm.")
    _write_skill(agy3 / "hey", description="Live hey.")

    rc3 = init.main(["--repo", str(repo3), "apply", "--replace", "--home", str(home3)])
    assert rc3 == 1
    assert (agy3 / "review" / "SKILL.md").is_file()
    assert (agy3 / "dev-swarm" / "SKILL.md").is_file()
    assert (agy3 / "hey" / "SKILL.md").is_file()
    assert list((home3 / ".gemini" / "config").glob("skills.bak.*")) == []


def test_apply_agents_symlink_rollback_interrupt_and_exdev_fallback(
    tmp_path: Path, isolate_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    repo = _make_fake_repo(home / "projects" / "agent-skills")
    existing = home / ".agents" / "skills" / "old-skill"
    _write_skill(existing, description="Keep on rollback.")

    with monkeypatch.context() as m:
        def fail_symlink(_self: Path, _target: Path) -> None:
            raise OSError("simulated symlink failure")

        m.setattr(Path, "symlink_to", fail_symlink)
        assert init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home)]) == 1
        assert (existing / "SKILL.md").is_file()

    # Verify isolate_home stayed active after nested monkeypatch context exited
    assert Path.home() == isolate_home

    # KeyboardInterrupt during symlink_to must also roll back ~/.agents/skills
    with monkeypatch.context() as m:
        def interrupt_symlink(_self: Path, _target: Path) -> None:
            raise KeyboardInterrupt("simulated interrupt")

        m.setattr(Path, "symlink_to", interrupt_symlink)
        with pytest.raises(KeyboardInterrupt):
            init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home)])
        assert (existing / "SKILL.md").is_file()

    # Explicit EXDEV fallback copies dir cleanly without losing data
    src_dir = _write_skill(tmp_path / "exdev_src" / "review", description="Cross-device.")
    (src_dir / "link.txt").symlink_to("SKILL.md")
    backup_dir = tmp_path / "exdev_bak"
    backup_dir.mkdir()
    real_rename = os.rename

    with monkeypatch.context() as m:
        def fake_rename(src: object, dst: object) -> None:
            if Path(str(src)) == src_dir:
                raise OSError(errno.EXDEV, "cross-device link")
            real_rename(str(src), str(dst))

        m.setattr(os, "rename", fake_rename)
        init._move_entry_to_backup(src_dir, backup_dir / "review", backup_dir)
    assert not src_dir.exists()
    assert (backup_dir / "review" / "SKILL.md").is_file()
    assert (backup_dir / "review" / "link.txt").is_symlink()


# ---------------------------------------------------------------------------
# adopt tests
# ---------------------------------------------------------------------------


def test_adopt_shared_and_agy_with_core_symlink_ignores_git_and_node_modules(
    tmp_path: Path,
) -> None:
    repo = _make_fake_repo(tmp_path / "repo", include_agy_skills=False)
    (repo / "core" / "review").mkdir(parents=True)
    (repo / "core" / "review" / "algorithm.md").write_text("# Algorithm\n", encoding="utf-8")

    src_shared = _write_skill(tmp_path / "loose" / "my-tool", description="A shared tool.")
    (src_shared / ".installed-version").write_bytes(b"1.4.3")
    (src_shared / "real.txt").write_text("payload", encoding="utf-8")
    (src_shared / "alias.txt").symlink_to("real.txt")
    (src_shared / ".DS_Store").write_bytes(b"junk")
    (src_shared / ".Git").mkdir()
    (src_shared / ".Git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (src_shared / "node_modules").mkdir()
    (src_shared / "node_modules" / "pkg.js").write_text("module.exports = 1;\n", encoding="utf-8")
    (src_shared / "__pycache__").mkdir()
    (src_shared / "__pycache__" / "x.pyc").write_bytes(b"pyc")

    rc1 = init.main(["--repo", str(repo), "adopt", str(src_shared), "--scope", "shared"])
    assert rc1 == 0
    dst = repo / "shared" / "my-tool"
    assert (dst / ".installed-version").read_bytes() == b"1.4.3"
    assert (dst / "alias.txt").is_symlink()
    assert os.readlink(dst / "alias.txt") == "real.txt"
    assert not (dst / ".Git").exists()
    assert not (dst / "node_modules").exists()
    assert not (dst / ".DS_Store").exists()
    assert not (dst / "__pycache__").exists()
    assert list((repo / "shared").glob(".adopt-*")) == []

    # Fresh adopt --scope agy with relative adapter symlink core -> ../../../core/review
    src_agy = _write_skill(tmp_path / "loose" / "review", description="Review adapter.")
    (src_agy / "core").symlink_to("../../../core/review")
    rc2 = init.main(["--repo", str(repo), "adopt", str(src_agy), "--scope", "agy"])
    assert rc2 == 0
    assert (repo / "harnesses" / "agy" / "review" / "core").is_symlink()
    assert init.cmd_doctor(repo) == 0


def test_adopt_rejects_bad_stale_and_chained_escape_symlinks(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path / "repo")

    abs_src = _write_skill(tmp_path / "bad_abs" / "abs-skill", description="Bad abs link.")
    (abs_src / "evil").symlink_to("/etc/hosts")
    assert init.main(["--repo", str(repo), "adopt", str(abs_src), "--scope", "shared"]) == 1
    assert not (repo / "shared" / "abs-skill").exists()

    esc_src = _write_skill(tmp_path / "bad_esc" / "esc-skill", description="Bad escape link.")
    (esc_src / "escape").symlink_to("../../../../etc/hosts")
    assert init.main(["--repo", str(repo), "adopt", str(esc_src), "--scope", "shared"]) == 1
    assert not (repo / "shared" / "esc-skill").exists()

    # --force must not validate symlinks against stale pre-swap dest_dir files
    existing_foo = _write_skill(repo / "shared" / "foo", description="Old foo.")
    (existing_foo / "old_only.txt").write_text("vanishes on swap", encoding="utf-8")
    incoming_foo = _write_skill(tmp_path / "in" / "foo", description="New foo.")
    (incoming_foo / "link.txt").symlink_to("../foo/old_only.txt")
    assert init.main(
        ["--repo", str(repo), "adopt", str(incoming_foo), "--scope", "shared", "--force"]
    ) == 1
    assert (existing_foo / "old_only.txt").is_file()

    # --force must block chained symlink escaping outside repo_root
    outside_secret = tmp_path / "outside_secret.txt"
    outside_secret.write_text("secret", encoding="utf-8")
    old_evil = _write_skill(repo / "harnesses" / "agy" / "evil", description="Old evil.")
    (old_evil / "a" / "b" / "c" / "d" / "e").mkdir(parents=True)
    (old_evil / "hop").symlink_to("a/b/c/d/e")

    new_evil = _write_skill(tmp_path / "in_evil" / "evil", description="New evil.")
    (new_evil / "hop").symlink_to("../../..")
    (new_evil / "escape").symlink_to("../evil/hop/../outside_secret.txt")
    assert init.main(
        ["--repo", str(repo), "adopt", str(new_evil), "--scope", "agy", "--force"]
    ) == 1
    assert os.readlink(old_evil / "hop") == "a/b/c/d/e"


def test_adopt_force_over_dir_symlink_readonly_subdir_and_cross_scope_dup(
    tmp_path: Path,
) -> None:
    repo = _make_fake_repo(tmp_path / "repo")
    src = _write_skill(tmp_path / "incoming" / "alpha-skill", description="Version 2.")
    (src / "v2.txt").write_text("two", encoding="utf-8")

    existing = _write_skill(repo / "shared" / "alpha-skill", description="Version 1.")
    ro_sub = existing / "readonly_sub"
    ro_sub.mkdir()
    (ro_sub / "old.txt").write_text("old", encoding="utf-8")
    os.chmod(ro_sub, 0o555)
    try:
        assert init.main(["--repo", str(repo), "adopt", str(src), "--scope", "shared"]) == 1
        assert not (existing / "v2.txt").exists()

        assert init.main(
            ["--repo", str(repo), "adopt", str(src), "--scope", "shared", "--force"]
        ) == 0
        assert (repo / "shared" / "alpha-skill" / "v2.txt").read_text(encoding="utf-8") == "two"
        assert list((repo / "shared").glob(".adopt-*")) == []
    finally:
        if ro_sub.exists():
            os.chmod(ro_sub, 0o755)

    # Cross-scope duplicate adoption (hey already exists in shared/) must be rejected
    dup_hey = _write_skill(tmp_path / "dup" / "hey", description="Duplicate hey.")
    assert init.main(["--repo", str(repo), "adopt", str(dup_hey), "--scope", "agy"]) == 1
    assert not (repo / "harnesses" / "agy" / "hey").exists()

    # --force over destination symlink pointing to source_dir succeeds safely
    gamma_src = _write_skill(tmp_path / "incoming" / "gamma-skill", description="Gamma.")
    (gamma_src / "g.txt").write_text("ok", encoding="utf-8")
    symlink_dest = repo / "harnesses" / "agy" / "gamma-skill"
    symlink_dest.symlink_to(gamma_src)
    assert init.main(
        ["--repo", str(repo), "adopt", str(gamma_src), "--scope", "agy", "--force"]
    ) == 0
    assert not symlink_dest.is_symlink()
    assert (symlink_dest / "g.txt").read_text(encoding="utf-8") == "ok"
    assert (gamma_src / "g.txt").read_text(encoding="utf-8") == "ok"

    # Self-adopting the destination symlink path itself must still fail
    sym_self = repo / "harnesses" / "agy" / "beta-skill"
    beta_src = _write_skill(tmp_path / "ext" / "beta-skill", description="Beta.")
    sym_self.symlink_to(beta_src)
    rc_self_sym = init.main(
        ["--repo", str(repo), "adopt", str(sym_self), "--scope", "agy", "--force"]
    )
    assert rc_self_sym == 1
    assert sym_self.is_symlink()


def test_adopt_rollback_preserves_old_on_double_failure_and_respects_no_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_fake_repo(tmp_path / "repo")
    existing = _write_skill(repo / "shared" / "keep-skill", description="Original.")
    (existing / "orig.txt").write_text("precious", encoding="utf-8")
    incoming = _write_skill(tmp_path / "in" / "keep-skill", description="Replacement.")

    # Case 1: swap fails, rollback succeeds
    real_rename = os.rename
    calls = 0

    with monkeypatch.context() as m:
        def fail_second_rename(src: object, dst: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated swap failure")
            real_rename(str(src), str(dst))

        m.setattr(os, "rename", fail_second_rename)
        rc = init.main(
            ["--repo", str(repo), "adopt", str(incoming), "--scope", "shared", "--force"]
        )
    assert rc == 1
    assert (existing / "orig.txt").read_text(encoding="utf-8") == "precious"
    assert list((repo / "shared").glob(".adopt-*")) == []

    # Case 2: swap fails AND rollback fails -> _old must be preserved, NOT deleted
    calls = 0
    with monkeypatch.context() as m:
        def fail_swap_and_rollback(src: object, dst: object) -> None:
            nonlocal calls
            calls += 1
            if calls in (2, 3):
                raise OSError("simulated double failure")
            real_rename(str(src), str(dst))

        m.setattr(os, "rename", fail_swap_and_rollback)
        rc2 = init.main(
            ["--repo", str(repo), "adopt", str(incoming), "--scope", "shared", "--force"]
        )
    assert rc2 == 1
    stage_dirs = list((repo / "shared").glob(".adopt-keep-skill-*"))
    assert len(stage_dirs) == 1
    assert (stage_dirs[0] / "_old" / "orig.txt").read_text(encoding="utf-8") == "precious"
    assert not (stage_dirs[0] / "keep-skill").exists()

    # Case 3: force=False never overwrites a concurrently appearing destination
    repo2 = _make_fake_repo(tmp_path / "repo2")
    racer_src = _write_skill(tmp_path / "in_racer" / "race-skill", description="Racer.")
    racer_dst = repo2 / "shared" / "race-skill"
    real_validate = init._validate_staged_skill_symlinks

    with monkeypatch.context() as m:
        def create_winner_during_stage(*args: object, **kwargs: object) -> list[str]:
            _write_skill(racer_dst, description="Winner.")
            (racer_dst / "winner.txt").write_text("WINNER", encoding="utf-8")
            return real_validate(*args, **kwargs)  # type: ignore[arg-type]

        m.setattr(init, "_validate_staged_skill_symlinks", create_winner_during_stage)
        rc3 = init.main(["--repo", str(repo2), "adopt", str(racer_src), "--scope", "shared"])
    assert rc3 == 1
    assert (racer_dst / "winner.txt").read_text(encoding="utf-8") == "WINNER"


def test_adopt_validation_and_self_guard(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path / "repo")

    rc_self = init.main(
        ["--repo", str(repo), "adopt", str(repo / "shared" / "hey"), "--scope", "shared", "--force"]
    )
    assert rc_self == 1
    assert (repo / "shared" / "hey" / "SKILL.md").is_file()

    bad_name = _write_skill(tmp_path / "bad-dir", name="other-name", description="Mismatch.")
    assert init.main(["--repo", str(repo), "adopt", str(bad_name), "--scope", "shared"]) == 1

    camel = _write_skill(tmp_path / "Bad_Name", name="Bad_Name", description="Invalid case.")
    assert init.main(["--repo", str(repo), "adopt", str(camel), "--scope", "shared"]) == 1

    no_desc = tmp_path / "no-desc"
    no_desc.mkdir()
    (no_desc / "SKILL.md").write_text("---\nname: no-desc\n---\n", encoding="utf-8")
    assert init.main(["--repo", str(repo), "adopt", str(no_desc), "--scope", "shared"]) == 1


# ---------------------------------------------------------------------------
# doctor tests
# ---------------------------------------------------------------------------


def test_doctor_passes_on_real_repo_inprocess_and_subprocess(tmp_path: Path) -> None:
    assert init.cmd_doctor(REAL_REPO) == 0

    proc_before = _run_init_subprocess(["--repo", str(REAL_REPO), "doctor"], home_dir=tmp_path)
    assert proc_before.returncode == 0, proc_before.stderr
    assert "DOCTOR OK:" in proc_before.stdout

    proc_after = _run_init_subprocess(["doctor", "--repo", str(REAL_REPO)], home_dir=tmp_path)
    assert proc_after.returncode == 0, proc_after.stderr
    assert "DOCTOR OK:" in proc_after.stdout


@pytest.mark.parametrize("executable", ["/usr/local/bin/python3.11", "/usr/bin/python3"])
def test_doctor_smoke_host_interpreters(tmp_path: Path, executable: str) -> None:
    if not Path(executable).is_file():
        pytest.skip(f"{executable} not installed on this host")
    proc = _run_init_subprocess(["doctor"], home_dir=tmp_path, executable=executable)
    assert proc.returncode == 0, proc.stderr
    assert "DOCTOR OK:" in proc.stdout


def test_frontmatter_parser_variants_and_edge_cases(tmp_path: Path) -> None:
    block_literal = (
        "---\n"
        "name: alpha-skill\n"
        "description: |\n"
        "  First line of literal block.\n"
        "  Second line of literal block.\n"
        "triggers:\n"
        "  # Section comment\n"
        "  - alpha\n"
        "  - /alpha\n"
        "invocable: true\n"
        'argument-hint: "[arg]"\n'
        "---\n"
        "# Body\n"
    )
    parsed = init.parse_skill_frontmatter(block_literal)
    assert parsed["name"] == "alpha-skill"
    assert isinstance(parsed["description"], str)
    assert "First line" in parsed["description"] and "Second line" in parsed["description"]
    assert parsed["triggers"] == ["alpha", "/alpha"]
    assert parsed["invocable"] is True
    assert parsed["argument-hint"] == "[arg]"

    folded = (
        "---\nname: folded-skill\ndescription: >-\n  Line one\n  line two.\n\n"
        "  Paragraph two.\n---\n"
    )
    assert init.parse_skill_frontmatter(folded)["description"] == (
        "Line one line two.\nParagraph two."
    )

    folded_indented = (
        "---\nname: folded-ind\ndescription: >-\n\n  First\n\n    code\n\n  After\n---\n"
    )
    assert init.parse_skill_frontmatter(folded_indented)["description"] == (
        "\nFirst\n\n  code\n\nAfter"
    )

    # Apostrophes, doubled single quotes, multi-line quoted items, and 3 trailing backslashes
    tricky_fm = (
        "---\n"
        "name: tricky-skill\n"
        'description: "foo\\\\\\\n  bar"\n'
        "triggers: [what's in hey, what's up, it's me, 'it''s #1', 'two']\n"
        "multiline_flow: [\n"
        '  "hello\n   # world",\n'
        "  tail\n"
        "]\n"
        "examples:\n"
        '  - "Item 1\n    continued"\n'
        "metadata:\n"
        '  note: "Line 1\n    Line 2"\n'
        "---\n"
    )
    parsed_tricky = init.parse_skill_frontmatter(tricky_fm)
    assert parsed_tricky["description"] == "foo\\bar"
    assert parsed_tricky["triggers"] == [
        "what's in hey",
        "what's up",
        "it's me",
        "it's #1",
        "two",
    ]
    assert parsed_tricky["multiline_flow"] == ["hello # world", "tail"]
    assert parsed_tricky["examples"] == ["Item 1 continued"]
    assert parsed_tricky["metadata"] == {"note": "Line 1 Line 2"}

    plain_multiline = (
        "---\nname: plain-skill\ndescription: First\n  # inline comment\n  second line.\n---\n"
    )
    assert init.parse_skill_frontmatter(plain_multiline)["description"] == "First second line."
    assert init.parse_skill_frontmatter(
        "---\nname: b\ndescription: true\n  when invoked\n---\n"
    )["description"] == "true when invoked"
    assert init.parse_skill_frontmatter(
        "---\nname: apos\ndescription: Don't fail # comment\n---\n"
    )["description"] == "Don't fail"

    url_skill = tmp_path / "url-skill"
    url_skill.mkdir()
    (url_skill / "SKILL.md").write_text(
        '---\nname:\n  "url-skill"\ndescription:\n  https://example.com helper\n---\n',
        encoding="utf-8",
    )
    assert init.validate_skill_dir(url_skill) == []

    for bad_val in ['""', "null", "~", "false", "[]", "{}", "{\n  }"]:
        bad_dir = tmp_path / f"bad-{abs(hash(bad_val))}"
        bad_dir.mkdir()
        (bad_dir / "SKILL.md").write_text(
            f"---\nname: {bad_dir.name}\ndescription:\n  {bad_val}\n---\n",
            encoding="utf-8",
        )
        assert init.validate_skill_dir(bad_dir), f"Expected failure for {bad_val!r}"

    with pytest.raises(init.FrontmatterError):
        init.parse_skill_frontmatter("---\nname:no-space\ndescription: ok\n---\n")


def test_doctor_frontmatter_duplicates_symlink_skills_and_bad_containers(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path / "repo")
    assert init.cmd_doctor(repo) == 0

    stray_h = repo / "harnesses" / "notes.txt"
    stray_h.write_text("stray", encoding="utf-8")
    assert init.cmd_doctor(repo) == 1
    stray_h.unlink()

    dup = _write_skill(repo / "harnesses" / "agy" / "hey", description="Duplicate hey.")
    assert init.cmd_doctor(repo) == 1
    (dup / "SKILL.md").unlink()
    dup.rmdir()

    sym_skill = repo / "shared" / "alias-hey"
    sym_skill.symlink_to("hey")
    assert init.cmd_doctor(repo) == 1
    sym_skill.unlink()

    # Valid unregistered future harness remains allowed
    _write_skill(repo / "harnesses" / "cursor" / "future-skill", description="Future harness.")
    assert init.cmd_doctor(repo) == 0

    # Regular file at repo/shared or repo/harnesses must fail doctor
    bad_repo = tmp_path / "file_shared_repo"
    (bad_repo / "global").mkdir(parents=True)
    (bad_repo / "global" / "AGENTS.md").write_text("# Global\n", encoding="utf-8")
    (bad_repo / "shared").write_text("not-a-dir", encoding="utf-8")
    assert init.cmd_doctor(bad_repo) == 1
    (bad_repo / "shared").unlink()
    (bad_repo / "harnesses").write_text("not-a-dir", encoding="utf-8")
    assert init.cmd_doctor(bad_repo) == 1


def test_doctor_symlinks_chained_reenter_broken_loop_absolute_and_escape(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path / "repo-name")
    (repo / "core" / "review").mkdir(parents=True)
    (repo / "core" / "review" / "algorithm.md").write_text("# Algorithm\n", encoding="utf-8")

    link_dir = repo / "harnesses" / "agy" / "review" / "core"
    link_dir.symlink_to("../../../core/review")
    assert init.cmd_doctor(repo) == 0

    # Direct escape-and-reenter symlink must fail
    reenter = repo / "reenter-link"
    reenter.symlink_to("../repo-name/core/review/algorithm.md")
    assert init.cmd_doctor(repo) == 1
    reenter.unlink()

    # Chained-symlink escape-and-reenter (hop -> ../../.. + reenter -> hop/../repo-name/...)
    evil = _write_skill(repo / "harnesses" / "agy" / "evil", description="Evil.")
    (evil / "hop").symlink_to("../../..")
    (evil / "reenter").symlink_to("hop/../repo-name/shared/hey/SKILL.md")
    assert init.cmd_doctor(repo) == 1
    (evil / "reenter").unlink()
    (evil / "hop").unlink()
    (evil / "SKILL.md").unlink()
    evil.rmdir()

    # Broken, circular, absolute, escaping symlinks
    broken = repo / "broken-link"
    broken.symlink_to("does-not-exist")
    assert init.cmd_doctor(repo) == 1
    broken.unlink()

    loop_a = repo / "loop-a"
    loop_b = repo / "loop-b"
    loop_a.symlink_to("loop-b")
    loop_b.symlink_to("loop-a")
    assert init.cmd_doctor(repo) == 1
    loop_a.unlink()
    loop_b.unlink()

    abs_link = repo / "abs-link"
    abs_link.symlink_to((repo / "core" / "review").resolve())
    assert init.cmd_doctor(repo) == 1
    abs_link.unlink()

    outside = tmp_path / "outside.txt"
    outside.write_text("external", encoding="utf-8")
    esc_link = repo / "escape-link"
    esc_link.symlink_to("../outside.txt")
    assert init.cmd_doctor(repo) == 1
    esc_link.unlink()

    assert init.cmd_doctor(repo) == 0


def test_doctor_markdown_links_scanner_perf_escapes_and_list_fences(tmp_path: Path) -> None:
    # Linear-time performance assertions on adversarial inputs
    t0 = time.perf_counter()
    assert init._iter_markdown_links_on_line("[a](<b>" * 4000) == []
    assert init._iter_markdown_links_on_line("[a](<x" * 4000 + ">") == []
    assert init._iter_markdown_links_on_line("[a]((" * 4000 + " x") == []
    assert init._iter_markdown_links_on_line("[" * 10000) == []
    assert (time.perf_counter() - t0) < 0.25

    repo = _make_fake_repo(tmp_path / "checkout")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (repo / "docs" / "file_(v1).md").write_text("# V1\n", encoding="utf-8")
    (repo / "docs" / "badge.png").write_bytes(b"PNG")

    doc = repo / "README.md"
    doc.write_text(
        "# Docs\n\n"
        "Valid: [guide](./docs/guide.md#L1-L5), [parens](./docs/file_(v1).md),\n"
        "escaped [spec](./docs/file_\\(v1\\).md),\n"
        'titled [t](<./docs/guide.md> "Title (1"), [![img](./docs/badge.png)](./docs/guide.md).\n'
        "External: [web](https://example.com/home/page) and [anchor](#section).\n"
        "Inline code ignored: `[broken](./nope.md)`, ``[also](./nope2.md)``, `file:///etc/hosts`.\n"
        "Escaped closer: `foo\\` and [ok](./docs/guide.md).\n"
        "```markdown\n[fenced-broken](./nope3.md)\n```\n"
        "- ```markdown\n  [list-fenced](./nope4.md)\n  ```\n"
        "> ```markdown\n> [blockquoted-fence](./nope5.md)\n> ```\n"
        "~~~\n[tilde-fenced](./nope6.md)\n~~~\n"
        "Multi-line brackets [not\na link](./nope7.md)\n",
        encoding="utf-8",
    )
    assert init.cmd_doctor(repo) == 0

    failing_markdowns = [
        "[missing](./docs/missing.md)\n",
        "[abs](/etc/hosts)\n",
        "[pct](%2fetc%2fhosts)\n",
        "[nul](docs/foo%00bar.md)\n",
        "[fileuri](file:///etc/hosts)\n",
        "[reenter](../checkout/docs/guide.md)\n",
        "[[BLOCKING]](./docs/missing.md)\n",
        "[![img](./docs/badge.png)](./docs/missing.md)\n",
        "[empty]()\n",
        "[blank](   )\n",
        "[empty-angle](<>)\n",
        "\\` escaped then [broken](./docs/missing.md) and `code`\n",
        "Text [oops](<./docs/guide.md> and [broken](./docs/missing.md)\n",
        '[guide](./docs/missing.md "Section (1")\n',
        "- ```markdown\n  [ok](./docs/guide.md)\n  ```\n[broken](./docs/missing.md)\n",
    ]
    for snippet in failing_markdowns:
        doc.write_text(snippet, encoding="utf-8")
        assert init.cmd_doctor(repo) == 1, f"Expected failure for markdown: {snippet!r}"


def test_doctor_hardcoded_user_paths(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path / "repo")

    safe_md = repo / "SAFE.md"
    safe_md.write_text(
        "Placeholders: /Users/<username> and /home/<name>\n"
        "Vars: /home/$USER and /Users/${USER}\n"
        "URLs: https://example.com/home/page, https://example.com/var/home/page, "
        "and http://home/index\n",
        encoding="utf-8",
    )
    assert init.cmd_doctor(repo) == 0

    mac_user = "/" + "Users" + "/alice/project"
    linux_user = "/" + "home" + "/bob/project"

    cases = [
        ("bad.md", f"Path is {mac_user}\n"),
        ("bad.md", f"Link: file://{mac_user}/doc.md\n"),
        ("bad.md", f"Localhost: file://localhost{mac_user}/doc.md\n"),
        ("bad.md", f"Mount: /System/Volumes/Data{mac_user}\n"),
        ("bad.md", f"Private: /private{mac_user}\n"),
        ("bad.md", f"Ostree: /var{linux_user}\n"),
        ("bad.py", f'X = "{linux_user}"\n'),
        ("bad.json", json.dumps({"path": mac_user})),
    ]
    for filename, content in cases:
        target = repo / filename
        target.write_text(content, encoding="utf-8")
        assert init.cmd_doctor(repo) == 1, f"Expected failure for {filename}: {content!r}"
        target.unlink()

    assert init.cmd_doctor(repo) == 0


def test_doctor_py_compile_errors_and_unreadable_subdirs(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path / "repo")
    good_py = repo / "helper.py"
    good_py.write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")

    assert init.cmd_doctor(repo) == 0
    assert list(repo.rglob("__pycache__")) == []
    assert list(repo.rglob("*.pyc")) == []

    bad_py = repo / "broken.py"
    bad_py.write_text("def broken(:\n    pass\n", encoding="utf-8")
    assert init.cmd_doctor(repo) == 1
    bad_py.unlink()

    if os.geteuid() != 0:
        unreadable = repo / "unreadable.py"
        unreadable.write_text("x = 1\n", encoding="utf-8")
        os.chmod(unreadable, 0o000)
        try:
            assert init.cmd_doctor(repo) == 1
        finally:
            os.chmod(unreadable, 0o644)
            unreadable.unlink()

        # Non-git mode must fail closed on unreadable 0o000 subdirectory
        locked_sub = repo / "shared" / "hey" / "locked"
        locked_sub.mkdir()
        (locked_sub / "bad.py").write_text("def broken(:\n", encoding="utf-8")
        os.chmod(locked_sub, 0o000)
        try:
            assert init.cmd_doctor(repo) == 1
        finally:
            os.chmod(locked_sub, 0o755)
            (locked_sub / "bad.py").unlink()
            locked_sub.rmdir()


def test_doctor_git_repo_mode_fsmonitor_locked_dirs_and_nested_git(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path / "git_repo")
    (repo / ".gitignore").write_text("ignored.md\nignored_locked/\n", encoding="utf-8")
    (repo / "core" / "review").mkdir(parents=True)
    (repo / "core" / "review" / "algorithm.md").write_text("# Algorithm\n", encoding="utf-8")
    (repo / "harnesses" / "agy" / "review" / "core").symlink_to("../../../core/review")
    (repo / "tracked_deleted.md").write_text("# Temp\n", encoding="utf-8")

    fake_user_path = "/" + "Users" + "/alice/secret"
    (repo / "ignored.md").write_text(f"Path: {fake_user_path}\n", encoding="utf-8")

    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)

    pwned = tmp_path / "fsmonitor_ran"
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.fsmonitor", f"touch {pwned}"],
        check=True,
    )
    assert not pwned.exists()

    (repo / "tracked_deleted.md").unlink()
    assert init.cmd_doctor(repo) == 0
    assert not pwned.exists()

    # Untracked broken symlink must still be caught in git mode
    broken = repo / "untracked-broken"
    broken.symlink_to("no-such-file")
    assert init.cmd_doctor(repo) == 1
    broken.unlink()

    # Embedded nested .Git directory (case variant) must fail doctor
    nested_git = repo / "shared" / "hey" / ".Git"
    nested_git.mkdir()
    assert init.cmd_doctor(repo) == 1
    nested_git.rmdir()

    # Unreadable non-ignored subdir fails git-mode doctor, while gitignored 0o000 dir passes
    if os.geteuid() != 0:
        ignored_locked = repo / "ignored_locked"
        ignored_locked.mkdir()
        os.chmod(ignored_locked, 0o000)
        try:
            assert init.cmd_doctor(repo) == 0
        finally:
            os.chmod(ignored_locked, 0o755)
            ignored_locked.rmdir()

        locked_dir = repo / "shared" / "hey" / "locked"
        locked_dir.mkdir()
        os.chmod(locked_dir, 0o000)
        try:
            assert init.cmd_doctor(repo) == 1
        finally:
            os.chmod(locked_dir, 0o755)
            locked_dir.rmdir()

    assert init.cmd_doctor(repo) == 0


def test_read_only_noexec_dirs_and_inaccessible_leaf_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if os.geteuid() == 0:
        pytest.skip("chmod permission tests require non-root user")

    # 1. Non-git mode + _validate_skill_tree_symlinks fail closed on 0o444 subdir
    repo = _make_fake_repo(tmp_path / "nongit_repo")
    sub = repo / "shared" / "hey" / "sub"
    sub.mkdir()
    (sub / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    (sub / "evil").symlink_to("/etc/hosts")
    os.chmod(sub, 0o444)
    try:
        assert init.cmd_doctor(repo) == 1
        errs = init._validate_skill_tree_symlinks(
            repo / "shared" / "hey", repo, repo.resolve()
        )
        assert errs
    finally:
        os.chmod(sub, 0o755)

    # 2. Git-tracked files inside a 0o444 subdir fail doctor cleanly (no silent drop)
    git_repo = _make_fake_repo(tmp_path / "git_444_repo")
    gsub = git_repo / "shared" / "hey" / "sub"
    gsub.mkdir()
    (gsub / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    (gsub / "evil").symlink_to("/etc/hosts")
    subprocess.run(["git", "-C", str(git_repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
    os.chmod(gsub, 0o444)
    try:
        assert init.cmd_doctor(git_repo) == 1
    finally:
        os.chmod(gsub, 0o755)

    # 3. 0o444 ~/.gemini/config/skills/ fails apply --replace cleanly
    home = tmp_path / "home_444"
    agy_skills = home / ".gemini" / "config" / "skills"
    _write_skill(agy_skills / "hey", description="Shadow hey.")
    os.chmod(agy_skills, 0o444)
    try:
        assert init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home)]) == 1
    finally:
        os.chmod(agy_skills, 0o755)

    # 4. 0o000 leaf ~/.agents and ~/.gemini/config report inaccessible in status
    home_leaf = tmp_path / "home_leaf_000"
    (home_leaf / ".agents").mkdir(parents=True)
    (home_leaf / ".gemini" / "config").mkdir(parents=True)
    os.chmod(home_leaf / ".agents", 0o000)
    os.chmod(home_leaf / ".gemini" / "config", 0o000)
    try:
        rc = init.main(["--repo", str(repo), "status", "--home", str(home_leaf)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "conflict (inaccessible:" in out
        assert out.count("invalid (inaccessible:") == 4
    finally:
        os.chmod(home_leaf / ".agents", 0o755)
        os.chmod(home_leaf / ".gemini" / "config", 0o755)


def test_doctor_audits_tracked_node_modules_and_adopt_leftovers(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path / "git_tracked_repo")
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)

    # Untracked gitignored node_modules/ does not fail doctor
    ignored_nm = repo / "node_modules" / "pkg"
    ignored_nm.mkdir(parents=True)
    fake_user = "/" + "Users" + "/alice/.ssh"
    (ignored_nm / "leak.json").write_text(json.dumps({"p": fake_user}), encoding="utf-8")
    assert init.cmd_doctor(repo) == 0

    # Force-tracked node_modules/ entry MUST be audited by doctor
    subprocess.run(["git", "-C", str(repo), "add", "-f", "node_modules/pkg/leak.json"], check=True)
    assert init.cmd_doctor(repo) == 1
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "-f", "node_modules/pkg/leak.json"],
                   check=True)

    # Leftover .adopt-* directory in repo MUST be audited by doctor
    leftover = repo / "shared" / ".adopt-old-123"
    leftover.mkdir()
    (leftover / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    assert init.cmd_doctor(repo) == 1
    (leftover / "bad.py").unlink()
    leftover.rmdir()
    assert init.cmd_doctor(repo) == 0


def test_frontmatter_deep_flow_nesting_and_multiline_escaped_eol(tmp_path: Path) -> None:
    # Deep nesting must raise FrontmatterError (never RecursionError)
    deep_fm = "---\nname: deep\ndescription: ok\ntriggers: " + "[" * 600 + "]" * 600 + "\n---\n"
    with pytest.raises(init.FrontmatterError):
        init.parse_skill_frontmatter(deep_fm)

    deep_skill = tmp_path / "deep"
    deep_skill.mkdir()
    (deep_skill / "SKILL.md").write_text(deep_fm, encoding="utf-8")
    assert init.validate_skill_dir(deep_skill)

    # Escaped EOL inside multi-line flow double-quoted strings
    flow_cases = [
        ('---\nname: a\ndescription: ok\ntriggers: [\n  "hello\\\n  "\n]\n---\n', ["hello"]),
        (
            '---\nname: b\ndescription: ok\ntriggers: [\n  "hello\\\n  \\"a]b\\""\n]\n---\n',
            ['hello"a]b"'],
        ),
        (
            '---\nname: c\ndescription: ok\ntriggers: [\n  "hello\\\n  world"\n]\n---\n',
            ["helloworld"],
        ),
        (
            '---\nname: d\ndescription: ok\ntriggers: [\n  "hello\\\\\n  world"\n]\n---\n',
            ["hello\\ world"],
        ),
    ]
    for fm_text, expected in flow_cases:
        assert init.parse_skill_frontmatter(fm_text)["triggers"] == expected


def test_symlinked_in_repo_containers_and_agents_dir_inside_repo_rejected(
    tmp_path: Path,
) -> None:
    # Symlinked repo/shared must be rejected by adopt (--force) and apply
    outside_shared = tmp_path / "outside_shared"
    victim = _write_skill(outside_shared / "victim", description="External victim.")
    (victim / "important.txt").write_text("do-not-delete", encoding="utf-8")

    repo = tmp_path / "repo_sym_shared"
    repo.mkdir()
    (repo / "shared").symlink_to(outside_shared)
    incoming = _write_skill(tmp_path / "incoming" / "victim", description="Incoming.")
    assert init.main(
        ["--repo", str(repo), "adopt", str(incoming), "--scope", "shared", "--force"]
    ) == 1
    assert (victim / "important.txt").read_text(encoding="utf-8") == "do-not-delete"

    home = tmp_path / "home"
    home.mkdir()
    assert init.main(["--repo", str(repo), "apply", "--home", str(home)]) == 1
    assert not (home / ".agents" / "skills").exists()

    # Symlinked repo/harnesses/agy must also be rejected by adopt and apply
    repo2 = _make_fake_repo(tmp_path / "repo_sym_agy", include_agy_skills=False)
    outside_agy = tmp_path / "outside_agy"
    outside_agy.mkdir()
    (repo2 / "harnesses").mkdir()
    (repo2 / "harnesses" / "agy").symlink_to(outside_agy)
    assert init.main(["--repo", str(repo2), "adopt", str(incoming), "--scope", "agy"]) == 1
    assert init.main(["--repo", str(repo2), "apply", "--home", str(home)]) == 1

    # ~/.agents -> <repo>/sub must be rejected by apply and apply --replace
    repo3 = _make_fake_repo(tmp_path / "repo_agents_in")
    in_repo_sub = repo3 / "sub_agents"
    in_repo_sub.mkdir()
    home3 = tmp_path / "home3"
    home3.mkdir()
    (home3 / ".agents").symlink_to(in_repo_sub)
    assert init.main(["--repo", str(repo3), "apply", "--home", str(home3)]) == 1
    assert init.main(["--repo", str(repo3), "apply", "--replace", "--home", str(home3)]) == 1
    assert not (in_repo_sub / "skills").exists() and not (in_repo_sub / "skills").is_symlink()

    # Cloning repo at ~/.agents/agent-skills (parent outside repo) must still work
    home4 = tmp_path / "home4"
    repo4 = _make_fake_repo(home4 / ".agents" / "agent-skills")
    assert init.main(["--repo", str(repo4), "apply", "--home", str(home4)]) == 0
    assert (home4 / ".agents" / "skills").resolve() == (repo4 / "shared").resolve()


def test_doctor_rejects_non_dir_dot_and_chained_trailing_slash_links(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path / "repo_dot")
    hey_dir = repo / "shared" / "hey"

    # Symlink SKILL.md/. must fail
    bad_dot = hey_dir / "bad_dot"
    bad_dot.symlink_to("SKILL.md/.")
    assert init.cmd_doctor(repo) == 1
    bad_dot.unlink()

    # Chained hop -> bad_slash -> SKILL.md/ must fail
    bad_slash = hey_dir / "bad_slash"
    hop = hey_dir / "hop"
    bad_slash.symlink_to("SKILL.md/")
    hop.symlink_to("bad_slash")
    assert init.cmd_doctor(repo) == 1
    hop.unlink()
    bad_slash.unlink()

    # Markdown [bad](./SKILL.md/.) must fail
    doc = hey_dir / "README.md"
    doc.write_text("[bad](./SKILL.md/.)\n", encoding="utf-8")
    assert init.cmd_doctor(repo) == 1
    doc.unlink()

    assert init.cmd_doctor(repo) == 0


# ---------------------------------------------------------------------------
# skills.json normalization, global AGENTS.md, plugins & diff warnings
# ---------------------------------------------------------------------------


def test_skills_json_tilde_normalization_and_deduplication(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    repo = _make_fake_repo(home / "projects" / "agent-skills")
    config_dir = home / ".gemini" / "config"
    config_dir.mkdir(parents=True)
    skills_json = config_dir / "skills.json"

    # Case 1: Legacy {"entries": [{"path": "~/.agents/skills"}]}
    skills_json.write_text(
        json.dumps({"entries": [{"path": "~/.agents/skills"}]}, indent=2),
        encoding="utf-8",
    )
    rc_status = init.main(["--repo", str(repo), "status", "--home", str(home)])
    out_status = capsys.readouterr().out
    assert rc_status == 0
    assert "needs path normalization" in out_status
    assert "missing entries" in out_status

    # Dry-run mutates nothing and evaluates explicit repo targets
    before = _snapshot_tree(home)
    assert init.main(["--repo", str(repo), "apply", "--dry-run", "--home", str(home)]) == 0
    assert _snapshot_tree(home) == before

    # Live apply links ~/.agents/skills -> shared and normalizes without duplication
    assert init.main(["--repo", str(repo), "apply", "--home", str(home)]) == 0
    data = json.loads(skills_json.read_text(encoding="utf-8"))
    shared_abs = (repo / "shared").resolve().as_posix()
    agy_abs = (repo / "harnesses" / "agy").resolve().as_posix()
    assert data["entries"] == [{"path": shared_abs}, {"path": agy_abs}]
    assert all(e["path"].startswith("/") for e in data["entries"])

    capsys.readouterr()
    assert init.main(["--repo", str(repo), "status", "--home", str(home)]) == 0
    out_after = capsys.readouterr().out
    assert "needs path normalization" not in out_after
    assert ": configured" in out_after

    # Case 2: Deduplicate ~/.agents/skills + explicit shared, keep distinct filters, ~ & ~otheruser
    skills_json.write_text(
        json.dumps(
            {
                "entries": [
                    {"path": "~"},
                    {"path": "~otheruser/skills"},
                    {"path": "~/.agents/skills"},
                    {"path": shared_abs},
                    {"path": "~/.agents/skills", "exclude": ["hey"]},
                    {"path": "~/extra/custom", "include_only": ["my-skill"]},
                    {"path": "~/extra/custom", "include_only": ["my-skill"]},
                    {"path": agy_abs},
                ]
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    assert init.main(["--repo", str(repo), "apply", "--home", str(home)]) == 0
    merged = json.loads(skills_json.read_text(encoding="utf-8"))
    assert merged["entries"] == [
        {"path": home.resolve().as_posix()},
        {"path": "~otheruser/skills"},
        {"path": shared_abs},
        {"path": shared_abs, "exclude": ["hey"]},
        {"path": (home / "extra" / "custom").resolve().as_posix(), "include_only": ["my-skill"]},
        {"path": agy_abs},
    ]

    # Case 3: 2-step apply (conflict on real ~/.agents/skills) -> apply --replace deduplicates
    home_2step = tmp_path / "home_2step"
    _write_skill(home_2step / ".agents" / "skills" / "hey", description="Old local hey.")
    cfg_2step = home_2step / ".gemini" / "config"
    cfg_2step.mkdir(parents=True)
    sj_2step = cfg_2step / "skills.json"
    sj_2step.write_text(json.dumps({"entries": [{"path": "~/.agents/skills"}]}), encoding="utf-8")

    assert init.main(["--repo", str(repo), "apply", "--home", str(home_2step)]) == 1
    assert init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home_2step)]) == 0
    data_2step = json.loads(sj_2step.read_text(encoding="utf-8"))
    assert data_2step["entries"] == [{"path": shared_abs}, {"path": agy_abs}]

    # Case 4: Pre-existing absolute <home>/.agents/skills alias canonicalizes to <repo>/shared
    sj_2step.write_text(
        json.dumps(
            {
                "entries": [
                    {"path": (home_2step / ".agents" / "skills").as_posix()},
                    {"path": agy_abs},
                ]
            }
        ),
        encoding="utf-8",
    )
    capsys.readouterr()
    assert init.main(["--repo", str(repo), "status", "--home", str(home_2step)]) == 0
    assert "needs path normalization" in capsys.readouterr().out
    assert init.main(["--repo", str(repo), "apply", "--home", str(home_2step)]) == 0
    assert json.loads(sj_2step.read_text(encoding="utf-8"))["entries"] == [
        {"path": shared_abs},
        {"path": agy_abs},
    ]

    # Case 5: --dry-run --replace and live --replace agree when ~/.agents/skills excludes 'hey'
    home_parity = tmp_path / "home_parity"
    _write_skill(home_parity / ".agents" / "skills" / "hey", description="Local hey.")
    cfg_parity = home_parity / ".gemini" / "config"
    cfg_parity.mkdir(parents=True)
    (cfg_parity / "skills.json").write_text(
        json.dumps({"entries": [{"path": "~/.agents/skills", "exclude": ["hey"]}]}),
        encoding="utf-8",
    )
    _write_skill(cfg_parity / "skills" / "hey", description="Shadow hey.")

    before_parity = _snapshot_tree(home_parity)
    rc_dry_p = init.main(
        ["--repo", str(repo), "apply", "--dry-run", "--replace", "--home", str(home_parity)]
    )
    err_dry_p = capsys.readouterr().err
    assert rc_dry_p == 1
    assert "filters out 'hey'" in err_dry_p
    assert _snapshot_tree(home_parity) == before_parity

    rc_live_p = init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home_parity)])
    err_live_p = capsys.readouterr().err
    assert rc_live_p == 1
    assert "filters out 'hey'" in err_live_p
    assert (cfg_parity / "skills" / "hey" / "SKILL.md").is_file()


def test_global_agents_symlink_conflicts_replace_rollback_and_opt_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    repo = _make_fake_repo(home / "projects" / "agent-skills")
    agents_md = home / ".gemini" / "config" / "AGENTS.md"
    expected_target = (repo / "global" / "AGENTS.md").resolve()

    # Initial status reports not installed
    assert init.main(["--repo", str(repo), "status", "--home", str(home)]) == 0
    out = capsys.readouterr().out
    assert "~/.gemini/config/AGENTS.md" in out and "not installed" in out

    # --no-global-agents and --skip-global-agents skip installation when missing
    for flag in ("--no-global-agents", "--skip-global-agents"):
        assert init.main(["--repo", str(repo), "apply", flag, "--home", str(home)]) == 0
        assert not agents_md.exists() and not agents_md.is_symlink()

    # Normal apply creates symlink and is idempotent
    assert init.main(["--repo", str(repo), "apply", "--home", str(home)]) == 0
    assert agents_md.is_symlink()
    assert agents_md.resolve() == expected_target
    assert init.main(["--repo", str(repo), "apply", "--home", str(home)]) == 0
    capsys.readouterr()
    assert init.main(["--repo", str(repo), "status", "--home", str(home)]) == 0
    assert f"ok (symlink -> {expected_target})" in capsys.readouterr().out

    # Regular file conflict: fails without --replace, untouched under --no-global-agents --replace
    agents_md.unlink()
    agents_md.write_text("# Custom local rules\n", encoding="utf-8")
    capsys.readouterr()
    assert init.main(["--repo", str(repo), "status", "--home", str(home)]) == 0
    assert "conflict (regular file;" in capsys.readouterr().out

    assert init.main(["--repo", str(repo), "apply", "--home", str(home)]) == 1
    err = capsys.readouterr().err
    assert "CONFLICT:" in err and "--no-global-agents" in err
    assert not agents_md.is_symlink()
    assert agents_md.read_text(encoding="utf-8") == "# Custom local rules\n"

    for flag in ("--no-global-agents", "--skip-global-agents"):
        rc_skip = init.main(
            ["--repo", str(repo), "apply", "--replace", flag, "--home", str(home)]
        )
        assert rc_skip == 0
        assert not agents_md.is_symlink()
        assert agents_md.read_text(encoding="utf-8") == "# Custom local rules\n"
        assert list((home / ".gemini" / "config").glob("AGENTS.md.bak.*")) == []

    # Dry-run --replace leaves regular file untouched
    before = _snapshot_tree(home)
    assert (
        init.main(["--repo", str(repo), "apply", "--dry-run", "--replace", "--home", str(home)])
        == 0
    )
    assert _snapshot_tree(home) == before

    # Rollback restores original file if symlink_to fails
    with monkeypatch.context() as m:
        real_symlink = Path.symlink_to

        def fail_agents_md_symlink(self: Path, target: Path) -> None:
            if self == agents_md:
                raise OSError("simulated agents.md symlink failure")
            real_symlink(self, target)

        m.setattr(Path, "symlink_to", fail_agents_md_symlink)
        assert init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home)]) == 1
        assert not agents_md.is_symlink()
        assert agents_md.read_text(encoding="utf-8") == "# Custom local rules\n"

    # Real --replace backs up regular file and links global/AGENTS.md
    assert init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home)]) == 0
    assert agents_md.is_symlink()
    assert agents_md.resolve() == expected_target
    backups = list((home / ".gemini" / "config").glob("AGENTS.md.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "# Custom local rules\n"

    # Wrong/broken symlink at AGENTS.md is reported in status and replaced under --replace
    other_rules = tmp_path / "other_rules.md"
    other_rules.write_text("# Other\n", encoding="utf-8")
    agents_md.unlink()
    agents_md.symlink_to(other_rules)
    capsys.readouterr()
    assert init.main(["--repo", str(repo), "status", "--home", str(home)]) == 0
    assert "conflict (symlink ->" in capsys.readouterr().out
    assert init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home)]) == 0
    assert agents_md.resolve() == expected_target
    assert other_rules.read_text(encoding="utf-8") == "# Other\n"

    agents_md.unlink()
    agents_md.symlink_to(tmp_path / "dangling_rules.md")
    capsys.readouterr()
    assert init.main(["--repo", str(repo), "status", "--home", str(home)]) == 0
    assert "conflict (broken or circular symlink ->" in capsys.readouterr().out
    assert init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home)]) == 0
    assert agents_md.resolve() == expected_target


def test_agy_plugin_shadow_detection_backup_unmigrated_refusal_and_marketplace_skip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    repo = _make_fake_repo(home / "projects" / "agent-skills")
    config_dir = home / ".gemini" / "config"
    plugins_dir = config_dir / "plugins"

    # Marketplace plugin with colliding skill name must be ignored
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "plugins": {
                    "robotics": {"installedFrom": "antigravity-marketplace"},
                    "dev-swarm": {"installedFrom": ""},
                }
            }
        ),
        encoding="utf-8",
    )
    _write_skill(plugins_dir / "robotics" / "skills" / "review", description="Marketplace review.")

    # Non-marketplace plugin whose skills are all in repo
    _write_skill(plugins_dir / "dev-swarm" / "skills" / "dev-swarm", description="Local swarm.")
    _write_skill(plugins_dir / "dev-swarm" / "skills" / "review", description="Local review.")

    # Non-marketplace plugin containing an unmigrated extra skill
    _write_skill(plugins_dir / "mixed" / "skills" / "hey", description="Local hey.")
    _write_skill(plugins_dir / "mixed" / "skills" / "custom-unmigrated", description="Unmigrated.")

    # Status detects dev-swarm & mixed, skips marketplace robotics
    assert init.main(["--repo", str(repo), "status", "--home", str(home)]) == 0
    out = capsys.readouterr().out
    assert "dev-swarm: dev-swarm, review" in out
    assert "mixed: hey (unmigrated: custom-unmigrated)" in out
    assert "robotics" not in out

    # Apply without --replace warns and returns 0, leaving plugins intact
    rc_warn = init.main(["--repo", str(repo), "apply", "--home", str(home)])
    err_warn = capsys.readouterr().err
    assert rc_warn == 0
    assert "WARNING: local plugins" in err_warn
    assert "dev-swarm" in err_warn and "mixed" in err_warn
    assert "robotics" not in err_warn
    assert (plugins_dir / "dev-swarm").is_dir()
    assert (plugins_dir / "mixed").is_dir()

    # Dry-run --replace refuses mixed (exits 1) without modifying anything
    before_dry = _snapshot_tree(home)
    rc_dry = init.main(
        ["--repo", str(repo), "apply", "--dry-run", "--replace", "--home", str(home)]
    )
    err_dry = capsys.readouterr().err
    assert rc_dry == 1
    assert "unmigrated skills not in repo: custom-unmigrated" in err_dry
    assert _snapshot_tree(home) == before_dry

    # Live --replace backs up dev-swarm, refuses mixed, and preserves marketplace robotics
    rc_rep = init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home)])
    err_rep = capsys.readouterr().err
    assert rc_rep == 1
    assert "unmigrated skills not in repo: custom-unmigrated" in err_rep
    assert not (plugins_dir / "dev-swarm").exists()
    assert (plugins_dir / "mixed" / "skills" / "custom-unmigrated" / "SKILL.md").is_file()
    assert (plugins_dir / "robotics" / "skills" / "review" / "SKILL.md").is_file()
    plugin_backups = list(config_dir.glob("plugins.bak.*"))
    assert len(plugin_backups) == 1
    assert (plugin_backups[0] / "dev-swarm" / "skills" / "dev-swarm" / "SKILL.md").is_file()

    # If only mixed remains and --replace fails, no empty plugins.bak.* directory is left behind
    before_bak_count = len(list(config_dir.glob("plugins.bak.*")))
    assert init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home)]) == 1
    assert len(list(config_dir.glob("plugins.bak.*"))) == before_bak_count

    # Removing mixed leaves only marketplace robotics -> status reports no shadowing collisions
    for child in sorted((plugins_dir / "mixed").rglob("*"), reverse=True):
        if child.is_file() or child.is_symlink():
            child.unlink()
        else:
            child.rmdir()
    (plugins_dir / "mixed").rmdir()
    capsys.readouterr()
    assert init.main(["--repo", str(repo), "status", "--home", str(home)]) == 0
    assert "~/.gemini/config/plugins" in capsys.readouterr().out
    assert init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home)]) == 0
    assert (plugins_dir / "robotics" / "skills" / "review" / "SKILL.md").is_file()

    # Guard: plugin must not be moved if skills.json filters out one of its colliding skills
    home2 = tmp_path / "home2"
    cfg2 = home2 / ".gemini" / "config"
    cfg2.mkdir(parents=True)
    (cfg2 / "skills.json").write_text(
        json.dumps(
            {
                "entries": [
                    {"path": (repo / "shared").resolve().as_posix()},
                    {
                        "path": (repo / "harnesses" / "agy").resolve().as_posix(),
                        "exclude": ["review"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    _write_skill(cfg2 / "plugins" / "local-p" / "skills" / "review", description="Shadow.")
    rc_filt = init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home2)])
    err_filt = capsys.readouterr().err
    assert rc_filt == 1
    assert "filters out 'review'" in err_filt
    assert (cfg2 / "plugins" / "local-p" / "skills" / "review" / "SKILL.md").is_file()
    assert list(cfg2.glob("plugins.bak.*")) == []

    # Guard: malformed/corrupt config.json fails closed so marketplace plugins are never moved
    home3 = tmp_path / "home3"
    cfg3 = home3 / ".gemini" / "config"
    cfg3.mkdir(parents=True)
    _write_skill(cfg3 / "plugins" / "robotics" / "skills" / "review", description="Marketplace.")
    for bad_cfg in ("{invalid json", "[]", '{"plugins": "not-a-dict"}', '{"plugins": {"a": 1}}'):
        (cfg3 / "config.json").write_text(bad_cfg, encoding="utf-8")
        capsys.readouterr()
        assert init.main(["--repo", str(repo), "status", "--home", str(home3)]) == 0
        assert "~/.gemini/config/plugins" in capsys.readouterr().out
        rc_bad_cfg = init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home3)])
        assert rc_bad_cfg == 1
        assert (cfg3 / "plugins" / "robotics" / "skills" / "review" / "SKILL.md").is_file()
        assert list(cfg3.glob("plugins.bak.*")) == []

    (cfg3 / "config.json").unlink()
    (cfg3 / "config.json").symlink_to(tmp_path / "missing_config.json")
    assert init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home3)]) == 1
    assert (cfg3 / "plugins" / "robotics" / "skills" / "review" / "SKILL.md").is_file()
    assert list(cfg3.glob("plugins.bak.*")) == []


def test_agy_plugin_shadow_guards_runtime_components_while_allowing_agents_rules_assets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    repo = _make_fake_repo(home / "projects" / "agent-skills")
    config_dir = home / ".gemini" / "config"
    plugins_dir = config_dir / "plugins"

    # Plugin with agents/, rules/, assets/, and only migrated skills backs up cleanly.
    dev_swarm = plugins_dir / "dev-swarm"
    _write_skill(dev_swarm / "skills" / "dev-swarm", description="Local swarm.")
    _write_skill(dev_swarm / "skills" / "review", description="Local review.")
    (dev_swarm / "plugin.json").write_text('{"name": "dev-swarm"}\n', encoding="utf-8")
    (dev_swarm / "agents").mkdir()
    (dev_swarm / "agents" / "helper.md").write_text("# Helper\n", encoding="utf-8")
    (dev_swarm / "rules").mkdir()
    (dev_swarm / "rules" / "AGENTS.md").write_text("# Local rule\n", encoding="utf-8")
    (dev_swarm / "assets").mkdir()
    (dev_swarm / "assets" / "icon.txt").write_text("icon\n", encoding="utf-8")

    # Plugins bundling mcp_config.json, hooks.json, or sidecars/ must not be moved wholesale.
    mcp_kit = plugins_dir / "mcp-kit"
    _write_skill(mcp_kit / "skills" / "review", description="Local review.")
    (mcp_kit / "plugin.json").write_text('{"name": "mcp-kit"}\n', encoding="utf-8")
    (mcp_kit / "mcp_config.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")

    hooks_kit = plugins_dir / "hooks-kit"
    _write_skill(hooks_kit / "skills" / "hey", description="Local hey.")
    (hooks_kit / "hooks.json").write_text('{"hooks": []}\n', encoding="utf-8")

    sidecar_kit = plugins_dir / "sidecar-kit"
    _write_skill(sidecar_kit / "skills" / "dev-swarm", description="Local swarm.")
    (sidecar_kit / "sidecars").mkdir()
    (sidecar_kit / "sidecars" / "worker.json").write_text("{}\n", encoding="utf-8")

    # Status reports non-skill components on runtime-bundled plugins, not on dev-swarm.
    assert init.main(["--repo", str(repo), "status", "--home", str(home)]) == 0
    out_status = capsys.readouterr().out
    assert (
        "dev-swarm: dev-swarm, review;" in out_status
        or "dev-swarm: dev-swarm, review)" in out_status
    )
    assert "mcp-kit: review (non-skill components: mcp_config.json)" in out_status
    assert "hooks-kit: hey (non-skill components: hooks.json)" in out_status
    assert "sidecar-kit: dev-swarm (non-skill components: sidecars)" in out_status

    # Dry-run --replace refuses the runtime-component plugins without touching disk.
    before = _snapshot_tree(home)
    rc_dry = init.main(
        ["--repo", str(repo), "apply", "--dry-run", "--replace", "--home", str(home)]
    )
    err_dry = capsys.readouterr().err
    assert rc_dry == 1
    assert "non-skill runtime components: mcp_config.json" in err_dry
    assert "non-skill runtime components: hooks.json" in err_dry
    assert "non-skill runtime components: sidecars" in err_dry
    assert _snapshot_tree(home) == before

    # Live --replace backs up dev-swarm cleanly while leaving runtime-component plugins intact.
    rc_live = init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home)])
    err_live = capsys.readouterr().err
    assert rc_live == 1
    assert "non-skill runtime components: mcp_config.json" in err_live
    assert "non-skill runtime components: hooks.json" in err_live
    assert "non-skill runtime components: sidecars" in err_live
    assert not dev_swarm.exists()
    assert (mcp_kit / "mcp_config.json").is_file()
    assert (hooks_kit / "hooks.json").is_file()
    assert (sidecar_kit / "sidecars" / "worker.json").is_file()
    backups = list(config_dir.glob("plugins.bak.*"))
    assert len(backups) == 1
    assert (backups[0] / "dev-swarm" / "skills" / "dev-swarm" / "SKILL.md").is_file()
    assert (backups[0] / "dev-swarm" / "agents" / "helper.md").is_file()

    # Re-running --replace with only refused plugins left creates no empty plugins.bak.* directory.
    assert init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home)]) == 1
    assert len(list(config_dir.glob("plugins.bak.*"))) == 1


def test_agents_skills_diff_warning_in_status_and_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    repo = _make_fake_repo(home / "projects" / "agent-skills")
    local_hey = home / ".agents" / "skills" / "hey"
    _write_skill(local_hey, description="Interact with HEY email.")

    # Identical tree (plus pruned .DS_Store) produces no diff warning
    (local_hey / ".DS_Store").write_bytes(b"junk")
    assert init.main(["--repo", str(repo), "status", "--home", str(home)]) == 0
    out_same = capsys.readouterr().out
    assert "differs from repo shared/" not in out_same

    assert (
        init.main(["--repo", str(repo), "apply", "--dry-run", "--replace", "--home", str(home)])
        == 0
    )
    err_same = capsys.readouterr().err
    assert "differ from repo shared/" not in err_same

    # Executable bit change on a script file also triggers diff detection
    repo_script = repo / "shared" / "hey" / "run.py"
    local_script = local_hey / "run.py"
    repo_script.write_text("print('ok')\n", encoding="utf-8")
    local_script.write_text("print('ok')\n", encoding="utf-8")
    os.chmod(repo_script, 0o644)
    os.chmod(local_script, 0o755)
    assert init.main(["--repo", str(repo), "status", "--home", str(home)]) == 0
    assert "differs from repo shared/: hey" in capsys.readouterr().out
    os.chmod(local_script, 0o644)
    assert init.main(["--repo", str(repo), "status", "--home", str(home)]) == 0
    assert "differs from repo shared/" not in capsys.readouterr().out

    # Modifying non-pruned metadata (.installed-version) also triggers diff detection
    (repo / "shared" / "hey" / ".installed-version").write_text("1.4.3", encoding="utf-8")
    (local_hey / ".installed-version").write_text("1.5.0", encoding="utf-8")
    assert init.main(["--repo", str(repo), "status", "--home", str(home)]) == 0
    assert "differs from repo shared/: hey" in capsys.readouterr().out

    # Modifying SKILL.md surfaces diff warning in both status and apply --replace
    _write_skill(local_hey, description="Newer local HEY skill.")
    assert init.main(["--repo", str(repo), "status", "--home", str(home)]) == 0
    out_diff = capsys.readouterr().out
    assert "differs from repo shared/: hey" in out_diff

    rc_rep = init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home)])
    err_diff = capsys.readouterr().err
    assert rc_rep == 0
    assert "differ from repo shared/: hey" in err_diff
    assert (home / ".agents" / "skills").resolve() == (repo / "shared").resolve()


def test_doctor_validates_global_agents_md(tmp_path: Path) -> None:
    # Missing global/AGENTS.md fails doctor
    repo_missing = _make_fake_repo(tmp_path / "missing_repo", include_global_agents=False)
    assert init.cmd_doctor(repo_missing) == 1

    # Valid global/AGENTS.md at exact 24,000-byte boundary passes
    repo = _make_fake_repo(tmp_path / "valid_repo")
    agents_md = repo / "global" / "AGENTS.md"
    agents_md.write_bytes(b"a" * init.GLOBAL_AGENTS_MAX_BYTES)
    assert init.cmd_doctor(repo) == 0

    # Oversized (> 24,000 bytes) fails, including multi-byte UTF-8 boundary
    agents_md.write_bytes(b"a" * (init.GLOBAL_AGENTS_MAX_BYTES + 1))
    assert init.cmd_doctor(repo) == 1
    agents_md.write_text("é" * 12_000, encoding="utf-8")
    assert init.cmd_doctor(repo) == 0
    agents_md.write_text("é" * 12_001, encoding="utf-8")
    assert init.cmd_doctor(repo) == 1

    # Empty / whitespace-only fails
    agents_md.write_text("   \n\t\n", encoding="utf-8")
    assert init.cmd_doctor(repo) == 1

    # Non-UTF-8 bytes fail
    agents_md.write_bytes(b"\xff\xfe\x00invalid")
    assert init.cmd_doctor(repo) == 1

    # Symlinked global/AGENTS.md fails
    agents_md.unlink()
    (repo / "global" / "real.md").write_text("# Real\n", encoding="utf-8")
    agents_md.symlink_to("real.md")
    assert init.cmd_doctor(repo) == 1
    agents_md.unlink()
    (repo / "global" / "real.md").unlink()

    # Symlinked global/ container fails
    (repo / "global").rmdir()
    ext_global = tmp_path / "ext_global"
    ext_global.mkdir()
    (ext_global / "AGENTS.md").write_text("# External\n", encoding="utf-8")
    (repo / "global").symlink_to(ext_global)
    assert init.cmd_doctor(repo) == 1


# ---------------------------------------------------------------------------
# Multi-harness (claude & codex) symlink farm, global rules, adopt & doctor tests
# ---------------------------------------------------------------------------


def _make_multi_harness_repo(repo_dir: Path) -> Path:
    repo = _make_fake_repo(repo_dir)
    _write_skill(
        repo / "harnesses" / "claude" / "review",
        description="Run Claude multi-agent code review.",
    )
    _write_skill(
        repo / "harnesses" / "claude" / "dev-swarm",
        description="Run Claude autonomous development swarm.",
    )
    _write_skill(
        repo / "harnesses" / "codex" / "review",
        description="Run Codex multi-agent code review.",
    )
    _write_skill(
        repo / "harnesses" / "codex" / "dev-swarm",
        description="Run Codex autonomous development swarm.",
    )
    return repo


def test_multi_harness_fresh_apply_idempotent_and_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    repo = _make_multi_harness_repo(tmp_path / "repo")
    expected_global = (repo / "global" / "AGENTS.md").resolve()

    # Before apply: status reports missing symlinks and not installed without creating home
    assert init.main(["--repo", str(repo), "status", "--home", str(home)]) == 0
    out_before = capsys.readouterr().out
    assert not home.exists()
    assert "harness 'claude' skills (2): dev-swarm, review" in out_before
    assert "harness 'codex' skills (2): dev-swarm, review" in out_before
    assert "~/.claude/skills" in out_before
    assert "~/.codex/skills" in out_before
    assert out_before.count("missing symlinks (dev-swarm, hey, review)") == 2
    assert "~/.claude/CLAUDE.md" in out_before and "not installed" in out_before
    assert "~/.codex/AGENTS.md" in out_before and "not installed" in out_before

    # Dry-run mutates nothing
    assert init.main(["--repo", str(repo), "apply", "--dry-run", "--home", str(home)]) == 0
    assert not home.exists()
    capsys.readouterr()

    # Live apply creates per-skill symlinks and global instruction links for all harnesses
    assert init.main(["--repo", str(repo), "apply", "--home", str(home)]) == 0
    capsys.readouterr()

    for harness, global_name in (("claude", "CLAUDE.md"), ("codex", "AGENTS.md")):
        skills_dir = home / f".{harness}" / "skills"
        assert skills_dir.is_dir() and not skills_dir.is_symlink()
        assert (skills_dir / "hey").is_symlink()
        assert (skills_dir / "hey").resolve() == (repo / "shared" / "hey").resolve()
        assert (skills_dir / "review").is_symlink()
        assert (skills_dir / "review").resolve() == (
            repo / "harnesses" / harness / "review"
        ).resolve()
        assert (skills_dir / "dev-swarm").is_symlink()
        assert (skills_dir / "dev-swarm").resolve() == (
            repo / "harnesses" / harness / "dev-swarm"
        ).resolve()

        global_link = home / f".{harness}" / global_name
        assert global_link.is_symlink()
        assert global_link.resolve() == expected_global

    # Status reports configured and ok
    assert init.main(["--repo", str(repo), "status", "--home", str(home)]) == 0
    out_after = capsys.readouterr().out
    assert (
        f"~/.claude/skills ({home / '.claude' / 'skills'}): configured (3 skill symlink(s))"
        in out_after
    )
    assert (
        f"~/.codex/skills ({home / '.codex' / 'skills'}): configured (3 skill symlink(s))"
        in out_after
    )
    assert (
        f"~/.claude/CLAUDE.md ({home / '.claude' / 'CLAUDE.md'}): "
        f"ok (symlink -> {expected_global})" in out_after
    )
    assert (
        f"~/.codex/AGENTS.md ({home / '.codex' / 'AGENTS.md'}): "
        f"ok (symlink -> {expected_global})" in out_after
    )

    # Second apply is completely idempotent
    before_second = _snapshot_tree(home)
    assert init.main(["--repo", str(repo), "apply", "--home", str(home)]) == 0
    out_second = capsys.readouterr().out
    assert _snapshot_tree(home) == before_second
    assert "already links all 3 skill(s)" in out_second

    # Absent harnesses/claude and harnesses/codex still expose shared/* cleanly
    home_shared_only = tmp_path / "home_shared_only"
    repo_shared_only = _make_fake_repo(tmp_path / "repo_shared_only", include_agy_skills=False)
    assert (
        init.main(["--repo", str(repo_shared_only), "apply", "--home", str(home_shared_only)])
        == 0
    )
    assert (home_shared_only / ".claude" / "skills" / "hey").resolve() == (
        repo_shared_only / "shared" / "hey"
    ).resolve()
    assert (home_shared_only / ".codex" / "skills" / "hey").resolve() == (
        repo_shared_only / "shared" / "hey"
    ).resolve()


def test_symlink_farm_preserves_builtin_and_unrelated_entries_across_replace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    repo = _make_multi_harness_repo(tmp_path / "repo")

    # Claude built-in synced/ directory and unrelated local skill + external symlink
    claude_skills = home / ".claude" / "skills"
    cloud_skill = claude_skills / "synced" / "cloud-builtin" / "SKILL.md"
    cloud_skill.parent.mkdir(parents=True)
    cloud_skill.write_text("# Cloud\n", encoding="utf-8")
    _write_skill(claude_skills / "my-claude-local", description="Unrelated local Claude skill.")
    ext_claude = _write_skill(tmp_path / "ext_claude" / "ext-tool", description="External tool.")
    (claude_skills / "ext-tool").symlink_to(ext_claude)

    # Codex built-in .system/ directory and unrelated local skill + external symlink
    codex_skills = home / ".codex" / "skills"
    sys_skill = codex_skills / ".system" / "codex-core" / "SKILL.md"
    sys_skill.parent.mkdir(parents=True)
    sys_skill.write_text("# System\n", encoding="utf-8")
    _write_skill(codex_skills / "my-codex-local", description="Unrelated local Codex skill.")
    ext_codex = _write_skill(tmp_path / "ext_codex" / "ext-codex-tool", description="External.")
    (codex_skills / "ext-codex-tool").symlink_to(ext_codex)

    assert init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home)]) == 0
    capsys.readouterr()

    assert cloud_skill.read_text(encoding="utf-8") == "# Cloud\n"
    assert (claude_skills / "my-claude-local" / "SKILL.md").is_file()
    assert (claude_skills / "ext-tool").is_symlink()
    assert (claude_skills / "ext-tool").resolve() == ext_claude.resolve()
    assert list((home / ".claude").glob("skills.bak.*")) == []

    assert sys_skill.read_text(encoding="utf-8") == "# System\n"
    assert (codex_skills / "my-codex-local" / "SKILL.md").is_file()
    assert (codex_skills / "ext-codex-tool").is_symlink()
    assert (codex_skills / "ext-codex-tool").resolve() == ext_codex.resolve()
    assert list((home / ".codex").glob("skills.bak.*")) == []

    # Status reports configured without flagging preserved or unrelated entries
    assert init.main(["--repo", str(repo), "status", "--home", str(home)]) == 0
    out = capsys.readouterr().out
    assert "configured (3 skill symlink(s))" in out
    assert "my-claude-local" not in out
    assert "my-codex-local" not in out
    assert "synced" not in out


def test_symlink_farm_conflicts_backup_replace_rollback_and_container_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    repo = _make_multi_harness_repo(tmp_path / "repo")

    # Legacy real directory in ~/.claude/skills/dev-swarm and regular file at review
    claude_skills = home / ".claude" / "skills"
    _write_skill(claude_skills / "dev-swarm", description="Legacy local dev-swarm.")
    (claude_skills / "dev-swarm" / "legacy.txt").write_text("keep-me", encoding="utf-8")
    (claude_skills / "review").write_text("stray file", encoding="utf-8")

    # External symlink in ~/.codex/skills/dev-swarm
    ext_swarm = _write_skill(tmp_path / "claude_role" / "dev-swarm", description="External swarm.")
    (ext_swarm / "ext.txt").write_text("untouched", encoding="utf-8")
    codex_skills = home / ".codex" / "skills"
    codex_skills.mkdir(parents=True)
    (codex_skills / "dev-swarm").symlink_to(ext_swarm)

    # Status reports conflicts
    assert init.main(["--repo", str(repo), "status", "--home", str(home)]) == 0
    out_status = capsys.readouterr().out
    assert "conflict (dev-swarm, review)" in out_status
    assert "conflict (dev-swarm)" in out_status

    # Apply without --replace fails and leaves conflicting paths untouched
    rc_no_rep = init.main(["--repo", str(repo), "apply", "--home", str(home)])
    err_no_rep = capsys.readouterr().err
    assert rc_no_rep == 1
    assert "CONFLICT:" in err_no_rep
    assert not (claude_skills / "dev-swarm").is_symlink()
    assert (claude_skills / "dev-swarm" / "legacy.txt").read_text(encoding="utf-8") == "keep-me"
    assert (claude_skills / "review").read_text(encoding="utf-8") == "stray file"
    assert (codex_skills / "dev-swarm").resolve() == ext_swarm.resolve()

    # Dry-run --replace mutates nothing
    before_dry = _snapshot_tree(home)
    assert (
        init.main(["--repo", str(repo), "apply", "--dry-run", "--replace", "--home", str(home)])
        == 0
    )
    assert _snapshot_tree(home) == before_dry

    # Rollback restores ~/.claude/skills/dev-swarm if symlink_to fails
    with monkeypatch.context() as m:
        real_symlink = Path.symlink_to

        def fail_claude_swarm_link(self: Path, target: Path) -> None:
            if self == claude_skills / "dev-swarm":
                raise OSError("simulated symlink error")
            real_symlink(self, target)

        m.setattr(Path, "symlink_to", fail_claude_swarm_link)
        rc_rb = init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home)])
        assert rc_rb == 1
        assert not (claude_skills / "dev-swarm").is_symlink()
        assert (claude_skills / "dev-swarm" / "legacy.txt").read_text(encoding="utf-8") == "keep-me"

    # Re-stage review conflict to verify single backup directory per harness on live --replace
    assert (claude_skills / "review").is_symlink()
    (claude_skills / "review").unlink()
    (claude_skills / "review").write_text("stray file 2", encoding="utf-8")
    assert (codex_skills / "dev-swarm").is_symlink()
    (codex_skills / "dev-swarm").unlink()
    (codex_skills / "dev-swarm").symlink_to(ext_swarm)

    # Clear prior backup dirs from partial rollback run to verify clean single-dir backup below
    for bak in list((home / ".claude").glob("skills.bak.*")) + list(
        (home / ".codex").glob("skills.bak.*")
    ):
        init._rmtree_owned_dir(bak)

    assert init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home)]) == 0
    assert (claude_skills / "dev-swarm").resolve() == (
        repo / "harnesses" / "claude" / "dev-swarm"
    ).resolve()
    assert (claude_skills / "review").resolve() == (
        repo / "harnesses" / "claude" / "review"
    ).resolve()
    claude_baks = list((home / ".claude").glob("skills.bak.*"))
    assert len(claude_baks) == 1
    assert (claude_baks[0] / "dev-swarm" / "legacy.txt").read_text(encoding="utf-8") == "keep-me"
    assert (claude_baks[0] / "review").read_text(encoding="utf-8") == "stray file 2"

    assert (codex_skills / "dev-swarm").resolve() == (
        repo / "harnesses" / "codex" / "dev-swarm"
    ).resolve()
    assert (ext_swarm / "ext.txt").read_text(encoding="utf-8") == "untouched"
    codex_baks = list((home / ".codex").glob("skills.bak.*"))
    assert len(codex_baks) == 1
    assert (codex_baks[0] / "dev-swarm").is_symlink()

    # Container conflict: symlink or regular file at ~/.claude/skills or ~/.codex/skills
    home_cont = tmp_path / "home_cont"
    (home_cont / ".claude").mkdir(parents=True)
    ext_dir = tmp_path / "ext_skills_dir"
    ext_dir.mkdir()
    (ext_dir / "keep.txt").write_text("safe", encoding="utf-8")
    (home_cont / ".claude" / "skills").symlink_to(ext_dir)
    (home_cont / ".codex").mkdir(parents=True)
    (home_cont / ".codex" / "skills").write_text("not-a-dir", encoding="utf-8")

    capsys.readouterr()
    assert init.main(["--repo", str(repo), "status", "--home", str(home_cont)]) == 0
    out_cont = capsys.readouterr().out
    assert "conflict (symlink ->" in out_cont
    assert "conflict (non-directory file;" in out_cont

    assert init.main(["--repo", str(repo), "apply", "--home", str(home_cont)]) == 1
    assert (home_cont / ".claude" / "skills").is_symlink()
    assert (home_cont / ".codex" / "skills").read_text(encoding="utf-8") == "not-a-dir"

    before_cont_dry = _snapshot_tree(home_cont)
    assert (
        init.main(
            ["--repo", str(repo), "apply", "--dry-run", "--replace", "--home", str(home_cont)]
        )
        == 0
    )
    assert _snapshot_tree(home_cont) == before_cont_dry

    # Container replacement rolls back original file if mkdir fails
    with monkeypatch.context() as m:
        real_mkdir = Path.mkdir

        def fail_codex_mkdir(
            self: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
        ) -> None:
            if self == home_cont / ".codex" / "skills":
                raise OSError("simulated mkdir failure")
            real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

        m.setattr(Path, "mkdir", fail_codex_mkdir)
        assert init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home_cont)]) == 1
        assert (home_cont / ".codex" / "skills").read_text(encoding="utf-8") == "not-a-dir"

    assert init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home_cont)]) == 0
    claude_cont_dir = home_cont / ".claude" / "skills"
    codex_cont_dir = home_cont / ".codex" / "skills"
    assert claude_cont_dir.is_dir() and not claude_cont_dir.is_symlink()
    assert (claude_cont_dir / "hey").resolve() == (repo / "shared" / "hey").resolve()
    assert (ext_dir / "keep.txt").read_text(encoding="utf-8") == "safe"
    assert codex_cont_dir.is_dir() and not codex_cont_dir.is_symlink()
    assert (codex_cont_dir / "hey").resolve() == (repo / "shared" / "hey").resolve()
    codex_file_baks = list((home_cont / ".codex").glob("skills.bak.*"))
    assert len(codex_file_baks) == 1
    assert codex_file_baks[0].read_text(encoding="utf-8") == "not-a-dir"


def test_symlink_farm_prunes_only_stale_repo_symlinks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    repo = _make_multi_harness_repo(tmp_path / "repo")
    assert init.main(["--repo", str(repo), "apply", "--home", str(home)]) == 0

    claude_skills = home / ".claude" / "skills"
    codex_skills = home / ".codex" / "skills"

    # Stale broken symlink pointing into <repo>/shared/removed-skill
    (claude_skills / "removed-skill").symlink_to(repo / "shared" / "removed-skill")
    # Stale relative symlink pointing into <repo>/harnesses/codex/old-skill
    rel_to_repo = os.path.relpath(repo / "harnesses" / "codex" / "old-skill", codex_skills)
    (codex_skills / "old-skill").symlink_to(rel_to_repo)

    # External symlinks (live, broken, and sibling-prefix repo-extra) must NOT be pruned
    outside_live = _write_skill(tmp_path / "repo-extra" / "keep-sibling", description="Sibling.")
    (claude_skills / "keep-sibling").symlink_to(outside_live)
    (claude_skills / "broken-external").symlink_to(tmp_path / "outside" / "gone")
    # Hidden symlink pointing into repo is preserved (hidden entries untouched)
    (codex_skills / ".hidden-link").symlink_to(repo / "shared" / "removed-skill")

    capsys.readouterr()
    assert init.main(["--repo", str(repo), "status", "--home", str(home)]) == 0
    out_st = capsys.readouterr().out
    assert "stale repo symlinks (removed-skill)" in out_st
    assert "stale repo symlinks (old-skill)" in out_st
    assert "keep-sibling" not in out_st
    assert "broken-external" not in out_st

    # Dry-run previews removal without mutating anything
    before_dry = _snapshot_tree(home)
    assert init.main(["--repo", str(repo), "apply", "--dry-run", "--home", str(home)]) == 0
    out_dry = capsys.readouterr().out
    assert _snapshot_tree(home) == before_dry
    assert "Would remove stale symlink" in out_dry

    # Live apply prunes only stale repo symlinks
    assert init.main(["--repo", str(repo), "apply", "--home", str(home)]) == 0
    assert not (claude_skills / "removed-skill").is_symlink()
    assert not (codex_skills / "old-skill").is_symlink()
    assert (claude_skills / "keep-sibling").is_symlink()
    assert (claude_skills / "broken-external").is_symlink()
    assert (codex_skills / ".hidden-link").is_symlink()

    capsys.readouterr()
    assert init.main(["--repo", str(repo), "status", "--home", str(home)]) == 0
    out_clean = capsys.readouterr().out
    assert "stale repo symlinks" not in out_clean
    assert "configured (3 skill symlink(s))" in out_clean


def test_no_global_agents_skips_all_three_harnesses_even_under_replace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    repo = _make_multi_harness_repo(tmp_path / "repo")
    targets = (
        home / ".gemini" / "config" / "AGENTS.md",
        home / ".claude" / "CLAUDE.md",
        home / ".codex" / "AGENTS.md",
    )

    # Fresh home: --no-global-agents and --skip-global-agents create none of the 3 files
    for flag in ("--no-global-agents", "--skip-global-agents"):
        assert init.main(["--repo", str(repo), "apply", flag, "--home", str(home)]) == 0
        for path in targets:
            assert not path.exists() and not path.is_symlink()

    # Pre-existing custom files at all 3 locations remain completely untouched under --replace
    for idx, path in enumerate(targets):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# Custom rules {idx}\n", encoding="utf-8")

    # Without --no-global-agents and without --replace, all 3 fail with CONFLICT
    assert init.main(["--repo", str(repo), "apply", "--home", str(home)]) == 1
    capsys.readouterr()

    for flag in ("--no-global-agents", "--skip-global-agents"):
        rc = init.main(["--repo", str(repo), "apply", "--replace", flag, "--home", str(home)])
        assert rc == 0
        for idx, path in enumerate(targets):
            assert not path.is_symlink()
            assert path.read_text(encoding="utf-8") == f"# Custom rules {idx}\n"
            assert list(path.parent.glob(f"{path.name}.bak.*")) == []

    # Live --replace without opt-out backs up and symlinks all 3
    expected_global = (repo / "global" / "AGENTS.md").resolve()
    assert init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home)]) == 0
    for idx, path in enumerate(targets):
        assert path.is_symlink()
        assert path.resolve() == expected_global
        baks = list(path.parent.glob(f"{path.name}.bak.*"))
        assert len(baks) == 1
        assert baks[0].read_text(encoding="utf-8") == f"# Custom rules {idx}\n"


def test_adopt_claude_codex_and_reserved_synced_guards(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path / "repo")
    (repo / "core" / "review").mkdir(parents=True)
    (repo / "core" / "review" / "algorithm.md").write_text("# Algorithm\n", encoding="utf-8")

    # Adopting same skill name ('review') into claude and codex coexists with harnesses/agy/review
    src_claude = _write_skill(tmp_path / "src_claude" / "review", description="Claude review.")
    (src_claude / "core").symlink_to("../../../core/review")
    src_codex = _write_skill(tmp_path / "src_codex" / "review", description="Codex review.")
    (src_codex / "core").symlink_to("../../../core/review")

    assert init.main(["--repo", str(repo), "adopt", str(src_claude), "--scope", "claude"]) == 0
    assert init.main(["--repo", str(repo), "adopt", str(src_codex), "--scope", "codex"]) == 0
    assert (repo / "harnesses" / "claude" / "review" / "SKILL.md").is_file()
    assert (repo / "harnesses" / "codex" / "review" / "SKILL.md").is_file()
    assert init.cmd_doctor(repo) == 0

    # Cross-scope duplicate with shared/hey is rejected for claude and codex
    dup_hey = _write_skill(tmp_path / "dup" / "hey", description="Duplicate hey.")
    assert init.main(["--repo", str(repo), "adopt", str(dup_hey), "--scope", "claude"]) == 1
    assert init.main(["--repo", str(repo), "adopt", str(dup_hey), "--scope", "codex"]) == 1

    # Reserved skill name 'synced' is rejected by adopt and doctor for shared/ and harnesses/claude/
    synced_src = _write_skill(tmp_path / "res" / "synced", description="Collides with synced.")
    assert init.main(["--repo", str(repo), "adopt", str(synced_src), "--scope", "shared"]) == 1
    assert init.main(["--repo", str(repo), "adopt", str(synced_src), "--scope", "claude"]) == 1

    _write_skill(repo / "harnesses" / "claude" / "synced", description="Bad synced.")
    assert init.cmd_doctor(repo) == 1
    init._rmtree_owned_dir(repo / "harnesses" / "claude" / "synced")

    _write_skill(repo / "shared" / "synced", description="Bad shared synced.")
    assert init.cmd_doctor(repo) == 1
    init._rmtree_owned_dir(repo / "shared" / "synced")
    assert init.cmd_doctor(repo) == 0


def test_symlink_farm_validation_overlap_and_bad_ancestor_guards(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 1. Invalid repo skill or duplicate name does not strand existing user skill
    home = tmp_path / "home"
    repo = _make_multi_harness_repo(tmp_path / "repo")
    (repo / "harnesses" / "claude" / "review" / "SKILL.md").write_text(
        "---\nname: review\ndescription:\n---\n", encoding="utf-8"
    )
    _write_skill(repo / "harnesses" / "claude" / "hey", description="Duplicate hey.")
    claude_skills = home / ".claude" / "skills"
    _write_skill(claude_skills / "review", description="Existing user review.")
    _write_skill(claude_skills / "hey", description="Existing user hey.")

    rc = init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "duplicated in both shared/ and harnesses/claude/" in err
    assert "failed validation" in err
    assert (claude_skills / "review" / "SKILL.md").is_file()
    assert not (claude_skills / "review").is_symlink()
    assert (claude_skills / "hey" / "SKILL.md").is_file()
    assert not (claude_skills / "hey").is_symlink()
    assert list((home / ".claude").glob("skills.bak.*")) == []

    # 2. Repo cloned inside ~/.claude/skills/agent-skills is rejected; ~/.claude/agent-skills works
    home_ov = tmp_path / "home_ov"
    repo_ov = _make_multi_harness_repo(home_ov / ".claude" / "skills" / "agent-skills")
    assert init.main(["--repo", str(repo_ov), "apply", "--replace", "--home", str(home_ov)]) == 1
    assert (repo_ov / "shared" / "hey" / "SKILL.md").is_file()

    home_sib = tmp_path / "home_sib"
    repo_sib = _make_multi_harness_repo(home_sib / ".claude" / "agent-skills")
    assert init.main(["--repo", str(repo_sib), "apply", "--home", str(home_sib)]) == 0
    assert (home_sib / ".claude" / "skills" / "hey").resolve() == (
        repo_sib / "shared" / "hey"
    ).resolve()

    # 3. Broken ancestor at ~/.gemini does not hide claude/codex status; bad ~/.claude fails apply
    home_bad = tmp_path / "home_bad"
    home_bad.mkdir()
    (home_bad / ".gemini").write_text("file", encoding="utf-8")
    (home_bad / ".claude").write_text("file", encoding="utf-8")
    good_repo = _make_multi_harness_repo(tmp_path / "good_repo")
    capsys.readouterr()
    assert init.main(["--repo", str(good_repo), "status", "--home", str(home_bad)]) == 0
    out_bad = capsys.readouterr().out
    assert "~/.claude/skills" in out_bad and "invalid (non-directory ancestor" in out_bad
    assert "~/.codex/skills" in out_bad and "missing symlinks" in out_bad
    assert init.main(["--repo", str(good_repo), "apply", "--dry-run", "--home", str(home_bad)]) == 1
    assert init.main(["--repo", str(good_repo), "apply", "--home", str(home_bad)]) == 1


def test_symlink_farm_symlinked_config_dir_and_repo_root_not_pruned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = _make_multi_harness_repo(home / "agent-skills")

    # Dotfile-symlinked ~/.claude -> <dotfiles>/claude with relative external symlinks
    dotfiles = tmp_path / "dotfiles"
    claude_real = dotfiles / "claude"
    claude_skills = claude_real / "skills"
    claude_skills.mkdir(parents=True)
    ext_skill = _write_skill(dotfiles / "agent-skills" / "ext", description="External dotfile.")
    (claude_skills / "ext").symlink_to("../../agent-skills/ext")
    (claude_skills / "ext-broken").symlink_to("../../agent-skills/ext-missing")
    # Symlink pointing to repo_root itself (not a descendant skill) must not be pruned
    (claude_skills / "repo-root-link").symlink_to(repo)
    (home / ".claude").symlink_to(claude_real)

    assert init.main(["--repo", str(repo), "apply", "--home", str(home)]) == 0
    capsys.readouterr()
    assert (claude_skills / "ext").is_symlink()
    assert (claude_skills / "ext").resolve() == ext_skill.resolve()
    assert (claude_skills / "ext-broken").is_symlink()
    assert (claude_skills / "repo-root-link").is_symlink()
    assert (claude_skills / "repo-root-link").resolve() == repo.resolve()

    assert init.main(["--repo", str(repo), "status", "--home", str(home)]) == 0
    out = capsys.readouterr().out
    assert "stale repo symlinks" not in out
    assert "configured (3 skill symlink(s))" in out


def test_symlink_farm_dry_run_parity_via_virtual_agents_skills(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _make_multi_harness_repo(tmp_path / "repo")

    # Case 1: ~/.agents/skills is a real dir with an unmigrated skill referenced by ~/.claude/skills
    home1 = tmp_path / "home1"
    _write_skill(home1 / ".agents" / "skills" / "unmigrated", description="Unmigrated local.")
    claude1 = home1 / ".claude" / "skills"
    claude1.mkdir(parents=True)
    (claude1 / "unmigrated").symlink_to("../../.agents/skills/unmigrated")

    # Without --replace, ~/.agents/skills conflicts so neither dry-run nor live prunes unmigrated
    before1 = _snapshot_tree(home1)
    assert init.main(["--repo", str(repo), "apply", "--dry-run", "--home", str(home1)]) == 1
    out1_no_rep = capsys.readouterr().out
    assert "Would remove stale symlink" not in out1_no_rep
    assert _snapshot_tree(home1) == before1

    # With --replace, dry-run predicts pruning unmigrated (matching live --replace)
    assert (
        init.main(["--repo", str(repo), "apply", "--dry-run", "--replace", "--home", str(home1)])
        == 0
    )
    out1_dry_rep = capsys.readouterr().out
    assert "Would remove stale symlink" in out1_dry_rep and "unmigrated" in out1_dry_rep
    assert _snapshot_tree(home1) == before1

    assert init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home1)]) == 0
    assert not (claude1 / "unmigrated").is_symlink()

    # Case 2: ~/.agents/skills absent, ~/.claude/skills/hey -> ../../.agents/skills/hey (in shared/)
    home2 = tmp_path / "home2"
    claude2 = home2 / ".claude" / "skills"
    claude2.mkdir(parents=True)
    (claude2 / "hey").symlink_to("../../.agents/skills/hey")

    before2 = _snapshot_tree(home2)
    assert init.main(["--repo", str(repo), "apply", "--dry-run", "--home", str(home2)]) == 0
    err2_dry = capsys.readouterr().err
    assert "CONFLICT:" not in err2_dry
    assert _snapshot_tree(home2) == before2

    assert init.main(["--repo", str(repo), "apply", "--home", str(home2)]) == 0
    assert (claude2 / "hey").is_symlink()
    assert (claude2 / "hey").resolve() == (repo / "shared" / "hey").resolve()
    assert list((home2 / ".claude").glob("skills.bak.*")) == []


def test_symlink_farm_refuses_container_replace_on_invalid_repo_and_handles_empty_or_unreadable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 1. Invalid or duplicate repo skill prevents replacing conflicting skills_dir container
    home = tmp_path / "home"
    repo = _make_multi_harness_repo(tmp_path / "repo")
    (repo / "harnesses" / "claude" / "review" / "SKILL.md").write_text(
        "---\nname: review\ndescription:\n---\n", encoding="utf-8"
    )
    _write_skill(repo / "harnesses" / "codex" / "hey", description="Duplicate hey.")

    ext_dir = tmp_path / "ext_working_skills"
    ext_dir.mkdir()
    (ext_dir / "keep.txt").write_text("intact", encoding="utf-8")
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "skills").symlink_to(ext_dir)
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "skills").write_text("existing-file", encoding="utf-8")

    before_dry = _snapshot_tree(home)
    assert (
        init.main(["--repo", str(repo), "apply", "--dry-run", "--replace", "--home", str(home)])
        == 1
    )
    err_dry = capsys.readouterr().err
    assert "refusing to replace" in err_dry
    assert _snapshot_tree(home) == before_dry

    assert init.main(["--repo", str(repo), "apply", "--replace", "--home", str(home)]) == 1
    err_live = capsys.readouterr().err
    assert "refusing to replace" in err_live
    assert (home / ".claude" / "skills").is_symlink()
    assert (home / ".claude" / "skills").resolve() == ext_dir.resolve()
    assert (home / ".codex" / "skills").read_text(encoding="utf-8") == "existing-file"
    assert list((home / ".claude").glob("skills.bak.*")) == []
    assert list((home / ".codex").glob("skills.bak.*")) == []

    # 2. Container replacement with 0 desired skills does not print "already links all 0 skill(s)"
    empty_repo = tmp_path / "empty_repo"
    (empty_repo / "shared").mkdir(parents=True)
    (empty_repo / "global").mkdir(parents=True)
    (empty_repo / "global" / "AGENTS.md").write_text("# Global\n", encoding="utf-8")
    home_empty = tmp_path / "home_empty"
    (home_empty / ".claude").mkdir(parents=True)
    (home_empty / ".claude" / "skills").write_text("file", encoding="utf-8")

    capsys.readouterr()
    assert (
        init.main(["--repo", str(empty_repo), "apply", "--replace", "--home", str(home_empty)])
        == 0
    )
    out_empty = capsys.readouterr().out
    assert (home_empty / ".claude" / "skills").is_dir()
    assert f"OK: {home_empty / '.claude' / 'skills'} already links all 0 skill(s)" not in out_empty

    # 3. Unreadable shared/ reports invalid repo target in status rather than stale repo symlinks
    if os.geteuid() != 0:
        home_unr = tmp_path / "home_unr"
        repo_unr = _make_multi_harness_repo(tmp_path / "repo_unr")
        assert init.main(["--repo", str(repo_unr), "apply", "--home", str(home_unr)]) == 0
        os.chmod(repo_unr / "shared", 0o000)
        try:
            capsys.readouterr()
            assert init.main(["--repo", str(repo_unr), "status", "--home", str(home_unr)]) == 0
            out_unr = capsys.readouterr().out
            assert "invalid repo target" in out_unr
            assert "stale repo symlinks" not in out_unr
        finally:
            os.chmod(repo_unr / "shared", 0o755)


def test_real_repo_apply_and_status_exposes_all_12_skills_and_core_symlinks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    assert init.main(["--repo", str(REAL_REPO), "apply", "--home", str(home)]) == 0
    capsys.readouterr()

    assert (home / ".agents" / "skills").is_symlink()
    assert (home / ".agents" / "skills").resolve() == (REAL_REPO / "shared").resolve()

    expected_claude = {
        "hey": REAL_REPO / "shared" / "hey",
        "mujoco-deformables": REAL_REPO / "shared" / "mujoco-deformables",
        "dev-swarm": REAL_REPO / "harnesses" / "claude" / "dev-swarm",
        "dev-swarm-zircon": REAL_REPO / "harnesses" / "claude" / "dev-swarm-zircon",
        "review": REAL_REPO / "harnesses" / "claude" / "review",
    }
    expected_codex = {
        "hey": REAL_REPO / "shared" / "hey",
        "mujoco-deformables": REAL_REPO / "shared" / "mujoco-deformables",
        "claude-review": REAL_REPO / "harnesses" / "codex" / "claude-review",
        "dev-swarm": REAL_REPO / "harnesses" / "codex" / "dev-swarm",
        "dev-swarm-zircon": REAL_REPO / "harnesses" / "codex" / "dev-swarm-zircon",
        "review": REAL_REPO / "harnesses" / "codex" / "review",
    }

    for harness, expected in (("claude", expected_claude), ("codex", expected_codex)):
        skills_dir = home / f".{harness}" / "skills"
        entries = sorted(p.name for p in skills_dir.iterdir())
        assert entries == sorted(expected)
        for name, target in expected.items():
            link = skills_dir / name
            assert link.is_symlink()
            assert link.resolve(strict=True) == target.resolve()
            if name in {"dev-swarm", "dev-swarm-zircon", "review"}:
                assert (link / "core").is_symlink()
                assert (link / "core").resolve(strict=True) == (REAL_REPO / "core" / name).resolve()

    expected_global = (REAL_REPO / "global" / "AGENTS.md").resolve()
    for link in (
        home / ".gemini" / "config" / "AGENTS.md",
        home / ".claude" / "CLAUDE.md",
        home / ".codex" / "AGENTS.md",
    ):
        assert link.is_symlink()
        assert link.resolve(strict=True) == expected_global

    assert init.main(["--repo", str(REAL_REPO), "status", "--home", str(home)]) == 0
    out = capsys.readouterr().out
    assert "configured (5 skill symlink(s))" in out
    assert "configured (6 skill symlink(s))" in out
    assert f"~/.claude/CLAUDE.md ({home / '.claude' / 'CLAUDE.md'}): ok (symlink ->" in out
    assert f"~/.codex/AGENTS.md ({home / '.codex' / 'AGENTS.md'}): ok (symlink ->" in out


def test_doctor_scans_toml_files_for_hardcoded_user_paths(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path / "repo")
    toml_file = repo / "shared" / "hey" / "config.toml"
    bad_path = "/" + "Users" + "/alice/private/tool.py"
    toml_file.write_text(f'path = "{bad_path}"\n', encoding="utf-8")
    assert init.cmd_doctor(repo) == 1

    toml_file.write_text('path = "~/.codex/skills/hey/tool.py"\n', encoding="utf-8")
    assert init.cmd_doctor(repo) == 0
