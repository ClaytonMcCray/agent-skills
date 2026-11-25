"""Unit and integration tests for core/review/scripts/collect_review_context.py and review skill."""

from __future__ import annotations

import contextlib
import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest

import init

REAL_REPO = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REAL_REPO / "core" / "review" / "scripts" / "collect_review_context.py"


def _load_collector_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("collect_review_context", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = prev
    return mod


crc = _load_collector_module()


def _git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "author@example.test",
            "GIT_COMMITTER_NAME": "Test Committer",
            "GIT_COMMITTER_EMAIL": "committer@example.test",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ATTR_NOSYSTEM": "1",
        }
    )
    if extra:
        env.update(extra)
    return env


def _git(
    repo: Path,
    *args: str,
    check: bool = True,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env=_git_env(env_extra),
        capture_output=True,
        text=True,
        check=check,
    )


def _init_git_repo(repo: Path, *, branch: str = "main") -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", branch)
    return repo


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _parse_summary_kv(summary_text: str) -> dict[str, str]:
    kv: dict[str, str] = {}
    for line in summary_text.splitlines():
        if line == "" or line.startswith("--- "):
            break
        if "=" in line:
            key, val = line.split("=", 1)
            kv[key] = val
    return kv


def _run_collector(
    args: Sequence[str],
    *,
    out_parent: Path | None = None,
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
    executable: str = sys.executable,
) -> subprocess.CompletedProcess[str]:
    cmd = [executable, str(SCRIPT_PATH)]
    if out_parent is not None:
        cmd.extend(["--out-parent", str(out_parent)])
    cmd.extend(args)
    env = _git_env(env_extra)
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_ok(
    args: Sequence[str],
    *,
    tmp_path: Path,
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
    executable: str = sys.executable,
) -> tuple[Path, dict[str, str], str]:
    out_parent = tmp_path / "collector_out"
    out_parent.mkdir(exist_ok=True)
    proc = _run_collector(
        args,
        out_parent=out_parent,
        cwd=cwd,
        env_extra=env_extra,
        executable=executable,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr!r}\nstdout={proc.stdout!r}"
    kv = _parse_summary_kv(proc.stdout)
    summary_dir = Path(kv["SUMMARY_DIR"])
    assert summary_dir.is_dir()
    assert (summary_dir / "summary.txt").read_text(encoding="utf-8") == proc.stdout
    return (summary_dir, kv, proc.stdout)


# ---------------------------------------------------------------------------
# 1. Host interpreter runtime smoke tests (Python 3.9 & 3.11+)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("executable", ["/usr/bin/python3", "/usr/local/bin/python3.11"])
def test_host_interpreters_runtime_smoke_and_dash_target(tmp_path: Path, executable: str) -> None:
    if not Path(executable).is_file():
        pytest.skip(f"{executable} not installed on this host")

    repo = _init_git_repo(tmp_path / "repo")
    (repo / "lib.rs").write_text("pub fn safe() -> i32 { 1 }\n", encoding="utf-8")
    (repo / "-weird.rs").write_text("pub fn dash() -> i32 { 2 }\n", encoding="utf-8")
    _commit_all(repo, "initial")

    (repo / "lib.rs").write_text(
        'extern "C" fn cb() {}\n'
        "pub fn danger(p: *const i32) -> i32 { unsafe { *p } }\n"
        "use std::cell::UnsafeCell;\n",
        encoding="utf-8",
    )
    (repo / "-weird.rs").write_text(
        "pub fn dash(p: *const i32) -> i32 { unsafe { *p } }\n",
        encoding="utf-8",
    )
    (repo / "notes.txt").write_text("new untracked file\n", encoding="utf-8")

    summary_dir, kv, _ = _run_ok(
        ["--repo", str(repo)],
        tmp_path=tmp_path,
        executable=executable,
    )
    assert kv["IS_GIT_REPO"] == "1"
    assert os.path.realpath(kv["REPO_ROOT"]) == os.path.realpath(str(repo))
    assert kv["CURRENT_BRANCH"] == "main"
    assert kv["HAS_RUST"] == "1"
    assert kv["HAS_RUST_UNSAFE"] == "1"
    assert kv["HAS_FFI"] == "1"
    assert kv["HAS_CONCURRENCY_HAZARDS"] == "1"
    assert "=== UNTRACKED NEW FILE: notes.txt ===" in (summary_dir / "diff.patch").read_text(
        encoding="utf-8"
    )

    dash_dir, dash_kv, _ = _run_ok(
        ["--repo", str(repo), "--", "-weird.rs"],
        tmp_path=tmp_path,
        executable=executable,
    )
    assert dash_kv["REVIEW_SCOPE"] == "paths:-weird.rs"
    assert dash_kv["HAS_RUST"] == "1"
    assert dash_kv["HAS_RUST_UNSAFE"] == "1"
    assert (dash_dir / "changed_files.txt").read_text(encoding="utf-8").splitlines() == [
        "-weird.rs"
    ]


# ---------------------------------------------------------------------------
# 2. CLI validation, env sanitization, BrokenPipe & leak-free temp dirs
# ---------------------------------------------------------------------------


def test_cli_errors_do_not_leak_temp_dirs(tmp_path: Path) -> None:
    out_parent = tmp_path / "empty_out"
    out_parent.mkdir()
    reg_file = tmp_path / "not_a_dir.txt"
    reg_file.write_text("x", encoding="utf-8")

    for bad_args in (
        ["--repo", str(tmp_path / "missing")],
        ["-C", str(tmp_path / "missing")],
        ["--repo", str(reg_file)],
        ["--repo", ""],
    ):
        proc = _run_collector(bad_args, out_parent=out_parent)
        assert proc.returncode == 1
        assert "ERROR:" in proc.stderr
        assert list(out_parent.iterdir()) == []

    for bad_out in (str(tmp_path / "no_out_dir"), str(reg_file), ""):
        proc = _run_collector(["--repo", str(tmp_path), "--out-parent", bad_out])
        assert proc.returncode == 1
        assert "ERROR:" in proc.stderr

    assert crc.main(["--repo", "bad\x00path", "--out-parent", str(out_parent)]) == 1
    assert list(out_parent.iterdir()) == []

    for bad_cli in (["--bad-flag"], ["--staged", "--working"]):
        proc = _run_collector(bad_cli, out_parent=out_parent)
        assert proc.returncode == 2
        assert list(out_parent.iterdir()) == []


def test_default_tmp_bundle_permissions_error_cleanup_and_broken_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_git_repo(tmp_path / "repo")
    summary_dir: Path | None = None
    try:
        proc = _run_collector(["--repo", str(repo)])
        kv = _parse_summary_kv(proc.stdout)
        summary_dir_str = kv.get("SUMMARY_DIR", "")
        if summary_dir_str:
            summary_dir = Path(summary_dir_str)
        assert proc.returncode == 0, proc.stderr
        assert summary_dir_str.startswith("/tmp/agy-review.")
        assert summary_dir is not None and summary_dir.is_dir()
        assert stat.S_IMODE(os.lstat(summary_dir).st_mode) == 0o700
        for expected_file in (
            "diff.patch",
            "changed_files.txt",
            "repo_files.txt",
            "untracked_files.txt",
            "hazard_hits.txt",
            "summary.txt",
        ):
            assert (summary_dir / expected_file).is_file()
    finally:
        if summary_dir is not None:
            shutil.rmtree(summary_dir, ignore_errors=True)

    # BrokenPipeError on real closed OS pipe during in-process main() MUST preserve SUMMARY_DIR
    # and MUST NOT mutate the host pipe FD via os.dup2
    bp_parent = tmp_path / "bp_out"
    bp_parent.mkdir()
    r_fd, w_fd = os.pipe()
    os.close(r_fd)
    pipe_writer = os.fdopen(w_fd, "w", encoding="utf-8", closefd=False)
    try:
        with monkeypatch.context() as mp:
            mp.setattr(sys, "stdout", pipe_writer)
            assert crc.main(["--repo", str(repo), "--out-parent", str(bp_parent)]) == 0
        assert stat.S_ISFIFO(os.fstat(w_fd).st_mode)
    finally:
        with contextlib.suppress(BrokenPipeError, OSError):
            pipe_writer.close()
        os.close(w_fd)
    created_bp_dirs = list(bp_parent.glob("agy-review.*"))
    assert len(created_bp_dirs) == 1
    assert (created_bp_dirs[0] / "summary.txt").is_file()

    # Mid-bundle exception cleans up out_dir cleanly
    out_parent = tmp_path / "abort_out"
    out_parent.mkdir()

    def boom(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("simulated mid-run failure")

    monkeypatch.setattr(crc, "_write_git_bundle", boom)
    with pytest.raises(RuntimeError, match="simulated mid-run failure"):
        crc.main(["--repo", str(repo), "--out-parent", str(out_parent)])
    assert list(out_parent.iterdir()) == []


def test_sanitized_git_env_ceiling_dirs_and_dubious_ownership_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_git_repo(tmp_path / "repo")
    (repo / "sub").mkdir()
    (repo / "sub" / "app.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "init")

    marker = tmp_path / "env_ext_diff_ran"
    evil_script = tmp_path / "env_evil.sh"
    evil_script.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    os.chmod(evil_script, 0o755)

    _, kv, _ = _run_ok(
        ["--repo", str(repo / "sub")],
        tmp_path=tmp_path,
        env_extra={
            "GIT_DIR": str(tmp_path / "nonexistent_git_dir"),
            "GIT_WORK_TREE": str(tmp_path / "nonexistent_work_tree"),
            "GIT_INDEX_FILE": str(tmp_path / "nonexistent_index"),
            "GIT_CEILING_DIRECTORIES": str(repo),
            "GIT_EXTERNAL_DIFF": str(evil_script),
            "GIT_DIFF_OPTS": "--output=/tmp/should_never_exist",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "diff.external",
            "GIT_CONFIG_VALUE_0": str(evil_script),
            "GIT_CONFIG_PARAMETERS": f"'diff.external={evil_script}'",
        },
    )
    assert kv["IS_GIT_REPO"] == "1"
    assert os.path.realpath(kv["REPO_ROOT"]) == os.path.realpath(str(repo))
    assert not marker.exists()

    out_parent = tmp_path / "own_out"
    out_parent.mkdir()
    fake_err = (
        b"fatal: detected dubious ownership in repository at '/repo'\x1b[31m\n"
        b"To add an exception for this directory, call:\n"
        b"\tgit config --global --add safe.directory /repo\n"
    )
    monkeypatch.setattr(
        crc,
        "_run_git",
        lambda _cwd, _args, **_kw: crc.GitResult(returncode=128, stdout=b"", stderr=fake_err),
    )
    rc = crc.main(["--repo", str(repo), "--out-parent", str(out_parent)])
    captured = capsys.readouterr()
    assert rc == 0
    assert "dubious ownership" in captured.err
    assert "\x1b[" not in captured.err
    assert "IS_GIT_REPO=0" in captured.out


# ---------------------------------------------------------------------------
# 3. Non-git directory mode, depth limits, confinement, pruning & bare/.git
# ---------------------------------------------------------------------------


def test_non_git_directory_depths_pruning_confinement_and_external_targets(
    tmp_path: Path,
) -> None:
    root = tmp_path / "plain_dir"
    (root / "d1" / "d2" / "d3").mkdir(parents=True)
    (root / "root.py").write_text("a = 1\n", encoding="utf-8")
    (root / "d1" / "l2.py").write_text("b = 2\n", encoding="utf-8")
    (root / "d1" / "d2" / "l3.py").write_text("c = 3\n", encoding="utf-8")
    (root / "d1" / "d2" / "d3" / "l4.py").write_text("d = 4\n", encoding="utf-8")
    (root / ".hidden").write_text("secret\n", encoding="utf-8")
    (root / ".ssh").mkdir()
    (root / ".ssh" / "id_rsa").write_text("private\n", encoding="utf-8")
    for excluded in ("Library", "node_modules", "target", "build"):
        (root / excluded).mkdir()
        (root / excluded / "ignored.py").write_text("skip\n", encoding="utf-8")

    outside = tmp_path / "outside_dir"
    (outside / "a" / "b" / "c" / "d").mkdir(parents=True)
    (outside / "top.py").write_text("top\n", encoding="utf-8")
    (outside / "a" / "b" / "c" / "d4.py").write_text("d4\n", encoding="utf-8")
    (outside / "a" / "b" / "c" / "d" / "d5.py").write_text("d5\n", encoding="utf-8")
    (outside / "node_modules").mkdir()
    (outside / "node_modules" / "skip.js").write_text("skip\n", encoding="utf-8")

    (root / "link_file.py").symlink_to(outside / "top.py")
    (root / "link_dir").symlink_to(outside)

    # External symlink alias pointing back into in-root `.ssh` must be blocked
    ext_alias = tmp_path / "ext_ssh_alias"
    ext_alias.symlink_to(root / ".ssh")

    sdir, kv, _ = _run_ok(["--repo", str(root)], tmp_path=tmp_path)
    assert kv["IS_GIT_REPO"] == "0"
    assert kv["REVIEW_SCOPE"] == "non-git-directory"
    assert os.path.realpath(kv["REPO_ROOT"]) == os.path.realpath(str(root))
    files = (sdir / "changed_files.txt").read_text(encoding="utf-8").splitlines()
    assert files == ["d1/d2/l3.py", "d1/l2.py", "root.py"]
    assert kv["CHANGED_FILES_COUNT"] == "3"

    # Explicit relative target (maxdepth 4 under target) + external absolute target
    (root / "d1" / "d2" / "d3" / "d4" / "d5").mkdir(parents=True)
    (root / "d1" / "d2" / "d3" / "d4" / "l5.py").write_text("e = 5\n", encoding="utf-8")
    (root / "d1" / "d2" / "d3" / "d4" / "d5" / "l6.py").write_text("f = 6\n", encoding="utf-8")
    sdir2, kv2, _ = _run_ok(
        ["--repo", str(root), "d1", str(outside)],
        tmp_path=tmp_path,
    )
    files2 = (sdir2 / "changed_files.txt").read_text(encoding="utf-8").splitlines()
    assert "d1/d2/d3/d4/l5.py" in files2
    assert "d1/d2/d3/d4/d5/l6.py" not in files2
    assert f"{outside}/top.py" in files2
    assert f"{outside}/a/b/c/d4.py" in files2
    assert f"{outside}/a/b/c/d/d5.py" not in files2
    assert not any("node_modules" in f for f in files2)
    assert kv2["IS_GIT_REPO"] == "0"

    # Confinement: reject `..`, intermediate symlinks, in-root `.ssh`, and external alias -> `.ssh`
    sdir3, _, _ = _run_ok(
        [
            "--repo",
            str(root),
            "../outside_dir",
            "link_dir/top.py",
            str(root / ".ssh" / "id_rsa"),
            str(root / "node_modules" / "ignored.py"),
            str(root / "link_dir" / "top.py"),
            str(ext_alias),
            str(ext_alias / "id_rsa"),
        ],
        tmp_path=tmp_path,
    )
    assert (sdir3 / "changed_files.txt").read_text(encoding="utf-8").splitlines() == []


def test_bare_repo_dot_git_and_hostile_core_worktree_escape_fallback(tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    bare.mkdir()
    _git(bare, "init", "-q", "--bare")
    _, kv_bare, _ = _run_ok(["--repo", str(bare)], tmp_path=tmp_path)
    assert kv_bare["IS_GIT_REPO"] == "0"
    assert kv_bare["REVIEW_SCOPE"] == "non-git-directory"

    repo = _init_git_repo(tmp_path / "normal_repo")
    _, kv_dotgit, _ = _run_ok(["--repo", str(repo / ".git")], tmp_path=tmp_path)
    assert kv_dotgit["IS_GIT_REPO"] == "0"
    assert kv_dotgit["REVIEW_SCOPE"] == "non-git-directory"

    # Hostile core.worktree pointing upward to outer dir MUST fall back to non-git confined to sub
    outer = tmp_path / "outer_parent"
    outer.mkdir()
    (outer / "outer_secret.txt").write_text("DO_NOT_EXFILTRATE\n", encoding="utf-8")
    evil_sub = _init_git_repo(outer / "evil_sub")
    (evil_sub / "inside.py").write_text("ok = 1\n", encoding="utf-8")
    _git(evil_sub, "config", "core.worktree", "../..")

    sdir_esc, kv_esc, _ = _run_ok(["--repo", str(evil_sub)], tmp_path=tmp_path)
    assert kv_esc["IS_GIT_REPO"] == "0"
    assert os.path.realpath(kv_esc["REPO_ROOT"]) == os.path.realpath(str(evil_sub))
    esc_files = (sdir_esc / "changed_files.txt").read_text(encoding="utf-8").splitlines()
    assert esc_files == ["inside.py"]
    assert "DO_NOT_EXFILTRATE" not in (sdir_esc / "diff.patch").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 4. All git scopes: empty, unborn, root, merges, tags, subdir, modes & paths
# ---------------------------------------------------------------------------


def test_empty_repo_unborn_branch_and_root_commit(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "repo")

    sdir_empty, kv_empty, _ = _run_ok(["--repo", str(repo)], tmp_path=tmp_path)
    assert kv_empty["REVIEW_SCOPE"] == "empty-repo"
    assert kv_empty["CHANGED_FILES_COUNT"] == "0"
    assert (sdir_empty / "diff.patch").read_bytes() == b""

    (repo / "staged.py").write_text("a = 1\n", encoding="utf-8")
    _git(repo, "add", "staged.py")
    (repo / "untracked.py").write_text("b = 2\n", encoding="utf-8")

    sdir_unborn, kv_unborn, _ = _run_ok(["--repo", str(repo)], tmp_path=tmp_path)
    assert kv_unborn["REVIEW_SCOPE"].startswith("working-tree")
    assert (sdir_unborn / "changed_files.txt").read_text(encoding="utf-8").splitlines() == [
        "staged.py",
        "untracked.py",
    ]
    assert "=== UNTRACKED NEW FILE: untracked.py ===" in (sdir_unborn / "diff.patch").read_text(
        encoding="utf-8"
    )

    _, kv_working, _ = _run_ok(["--repo", str(repo), "--working"], tmp_path=tmp_path)
    assert kv_working["REVIEW_SCOPE"] == "working-tree"
    assert kv_working["CHANGED_FILES_COUNT"] == "2"

    root_sha = _commit_all(repo, "root commit")
    sdir_init, kv_init, _ = _run_ok(["--repo", str(repo)], tmp_path=tmp_path)
    assert kv_init["REVIEW_SCOPE"] == "initial-commit (HEAD)"
    assert (sdir_init / "changed_files.txt").read_text(encoding="utf-8").splitlines() == [
        "staged.py",
        "untracked.py",
    ]

    sdir_commit, kv_commit, _ = _run_ok(["--repo", str(repo), root_sha], tmp_path=tmp_path)
    assert kv_commit["REVIEW_SCOPE"] == f"commit:{root_sha}"
    assert (sdir_commit / "changed_files.txt").read_text(encoding="utf-8").splitlines() == [
        "staged.py",
        "untracked.py",
    ]


def test_merge_commits_partial_conflict_clean_noff_annotated_tag_and_detached(
    tmp_path: Path,
) -> None:
    repo = _init_git_repo(tmp_path / "repo")
    base_lines = [f"const L{i}: i32 = {i};" for i in range(1, 70)]
    base_lines[5] = "const CONFLICT_LINE: i32 = 0;"
    base_lines[60] = "pub fn tail() { unsafe { std::hint::black_box(1); } }"
    (repo / "multi.rs").write_text("\n".join(base_lines) + "\n", encoding="utf-8")
    (repo / "clean.rs").write_text("pub fn old() { unsafe { } }\n", encoding="utf-8")
    (repo / "main_only.txt").write_text("v1\n", encoding="utf-8")
    _commit_all(repo, "root")

    # Branch 1: partial-conflict file (conflict at line 6 + clean removal of unsafe at line 61)
    # plus cleanly added feat_only.py
    _git(repo, "checkout", "-q", "-b", "feat")
    feat_lines = list(base_lines)
    feat_lines[5] = "const CONFLICT_LINE: i32 = 2;"
    feat_lines[60] = "pub fn tail() { let _safe = 1; }"
    (repo / "multi.rs").write_text("\n".join(feat_lines) + "\n", encoding="utf-8")
    (repo / "feat_only.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "feat commit")

    # Main diverges at line 6 only and touches unrelated main_only.txt
    _git(repo, "checkout", "-q", "main")
    main_lines = list(base_lines)
    main_lines[5] = "const CONFLICT_LINE: i32 = 1;"
    (repo / "multi.rs").write_text("\n".join(main_lines) + "\n", encoding="utf-8")
    (repo / "main_only.txt").write_text("v2 on main\n", encoding="utf-8")
    _commit_all(repo, "main commit")

    merge_proc = _git(repo, "merge", "--no-commit", "feat", check=False)
    assert merge_proc.returncode != 0
    resolved_lines = list(feat_lines)
    resolved_lines[5] = "const CONFLICT_LINE: i32 = 3;"
    (repo / "multi.rs").write_text("\n".join(resolved_lines) + "\n", encoding="utf-8")
    conflict_merge_sha = _commit_all(repo, "merge feat with partial conflict")

    sdir, kv, _ = _run_ok(["--repo", str(repo), conflict_merge_sha], tmp_path=tmp_path)
    assert kv["REVIEW_SCOPE"] == f"commit:{conflict_merge_sha}"
    assert kv["HAS_RUST"] == "1"
    assert kv["HAS_RUST_UNSAFE"] == "1"
    diff_text = (sdir / "diff.patch").read_text(encoding="utf-8")
    assert "diff --git a/multi.rs b/multi.rs" in diff_text
    assert "diff --git a/feat_only.py b/feat_only.py" in diff_text
    assert "main_only.txt" not in diff_text
    assert (sdir / "changed_files.txt").read_text(encoding="utf-8").splitlines() == [
        "feat_only.py",
        "multi.rs",
    ]

    # Branch 2: clean --no-ff merge deleting `unsafe` in clean.rs while main edits main_only.txt
    _git(repo, "checkout", "-q", "-b", "clean-feat")
    (repo / "clean.rs").write_text("pub fn old() {}\n", encoding="utf-8")
    _commit_all(repo, "remove unsafe")

    _git(repo, "checkout", "-q", "main")
    (repo / "main_only.txt").write_text("v3 on main\n", encoding="utf-8")
    _commit_all(repo, "main unrelated")
    _git(repo, "merge", "-q", "--no-ff", "-m", "clean no-ff merge", "clean-feat")
    clean_merge_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    sdir_clean, kv_clean, _ = _run_ok(["--repo", str(repo), clean_merge_sha], tmp_path=tmp_path)
    assert kv_clean["REVIEW_SCOPE"] == f"commit:{clean_merge_sha}"
    assert kv_clean["HAS_RUST"] == "1"
    assert kv_clean["HAS_RUST_UNSAFE"] == "1"
    assert (sdir_clean / "changed_files.txt").read_text(encoding="utf-8").splitlines() == [
        "clean.rs"
    ]
    clean_diff = (sdir_clean / "diff.patch").read_text(encoding="utf-8")
    assert "diff --git a/clean.rs b/clean.rs" in clean_diff
    assert "main_only.txt" not in clean_diff

    # Annotated tag with spoofed diff header in tag body must NOT inject fake unsafe hunk
    (repo / "plain.txt").write_text("hello\n", encoding="utf-8")
    _commit_all(repo, "plain txt commit")
    tag_msg = "Release tag\n\n--- a/spoof.rs\n+++ b/spoof.rs\n@@ -1 +1 @@\n+unsafe {}\n"
    _git(repo, "tag", "-a", "v1.0-spoof", "-m", tag_msg)

    sdir_tag, kv_tag, _ = _run_ok(["--repo", str(repo), "v1.0-spoof"], tmp_path=tmp_path)
    assert kv_tag["REVIEW_SCOPE"] == "commit:v1.0-spoof"
    assert kv_tag["HAS_RUST_UNSAFE"] == "0"
    assert "--- a/spoof.rs" not in (sdir_tag / "diff.patch").read_text(encoding="utf-8")

    # Detached HEAD reports CURRENT_BRANCH=DETACHED
    _git(repo, "checkout", "-q", "--detach", clean_merge_sha)
    _, kv_det, _ = _run_ok(["--repo", str(repo)], tmp_path=tmp_path)
    assert kv_det["CURRENT_BRANCH"] == "DETACHED"


def test_all_git_modes_subdir_targets_path_modes_and_worktree_gitfile(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "-q", "--bare", "-b", "trunk-base")

    repo = _init_git_repo(tmp_path / "repo", branch="trunk-base")
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("a = 1\n", encoding="utf-8")
    (repo / "src" / "b.py").write_text("b = 1\n", encoding="utf-8")
    (repo / "other").mkdir()
    (repo / "other" / "c.py").write_text("c = 1\n", encoding="utf-8")
    c1 = _commit_all(repo, "base commit")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "trunk-base")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk-base")

    _git(repo, "checkout", "-q", "-b", "feature/work")
    (repo / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
    c2 = _commit_all(repo, "feat 1")
    (repo / "src" / "b.py").write_text("b = 2\n", encoding="utf-8")
    c3 = _commit_all(repo, "feat 2")

    _, kv_branch, _ = _run_ok(["--repo", str(repo)], tmp_path=tmp_path)
    assert kv_branch["BASE_REF"] == "origin/trunk-base"
    assert kv_branch["BRANCH_COMMITS_AHEAD"] == "2"
    assert kv_branch["REVIEW_SCOPE"].startswith("branch (")
    assert kv_branch["CHANGED_FILES_COUNT"] == "2"

    _, kv_range2, _ = _run_ok(["--repo", str(repo), f"{c1}..{c2}"], tmp_path=tmp_path)
    assert kv_range2["REVIEW_SCOPE"] == f"range:{c1}..{c2}"
    assert kv_range2["CHANGED_FILES_COUNT"] == "1"

    _, kv_range3, _ = _run_ok(["--repo", str(repo), f"{c1}...{c3}"], tmp_path=tmp_path)
    assert kv_range3["REVIEW_SCOPE"] == f"range:{c1}...{c3}"
    assert kv_range3["CHANGED_FILES_COUNT"] == "2"

    _, kv_bvs, _ = _run_ok(["--repo", str(repo), "trunk-base"], tmp_path=tmp_path)
    assert kv_bvs["REVIEW_SCOPE"].startswith("branch-vs:trunk-base ")
    assert kv_bvs["CHANGED_FILES_COUNT"] == "2"

    # Unrelated dirty file must NOT blind path-scoped review
    (repo / "unrelated_dirty.txt").write_text("noise\n", encoding="utf-8")
    sdir_pbr, kv_pbr, _ = _run_ok(["--repo", str(repo), "src/a.py"], tmp_path=tmp_path)
    assert kv_pbr["REVIEW_SCOPE"] == "paths:src/a.py"
    assert (sdir_pbr / "changed_files.txt").read_text(encoding="utf-8").splitlines() == ["src/a.py"]

    sdir_pclean_dir, _, _ = _run_ok(["--repo", str(repo), "other"], tmp_path=tmp_path)
    assert (sdir_pclean_dir / "changed_files.txt").read_text(encoding="utf-8").splitlines() == [
        "other/c.py"
    ]
    (repo / "unrelated_dirty.txt").unlink()

    # Subdirectory --repo AND subdirectory CWD targets stay scoped to `src/`
    sdir_sub_dot, _, _ = _run_ok(["--repo", str(repo / "src"), "."], tmp_path=tmp_path)
    assert (sdir_sub_dot / "changed_files.txt").read_text(encoding="utf-8").splitlines() == [
        "src/a.py",
        "src/b.py",
    ]
    sdir_sub_cwd, _, _ = _run_ok(["a.py"], tmp_path=tmp_path, cwd=repo / "src")
    assert (sdir_sub_cwd / "changed_files.txt").read_text(encoding="utf-8").splitlines() == [
        "src/a.py"
    ]

    # Working edits that revert branch edits back to merge_base still fall back to working-tree
    (repo / "src" / "a.py").write_text("a = 1\n", encoding="utf-8")
    (repo / "src" / "b.py").write_text("b = 1\n", encoding="utf-8")
    sdir_rev, kv_rev, _ = _run_ok(["--repo", str(repo)], tmp_path=tmp_path)
    assert kv_rev["REVIEW_SCOPE"].startswith("working-tree")
    assert (sdir_rev / "changed_files.txt").read_text(encoding="utf-8").splitlines() == [
        "src/a.py",
        "src/b.py",
    ]

    # Add staged + unstaged + untracked changes -> auto selects branch-and-working
    (repo / "src" / "a.py").write_text("a = 3\n", encoding="utf-8")
    _git(repo, "add", "src/a.py")
    (repo / "src" / "b.py").write_text("b = 3\n", encoding="utf-8")
    (repo / "src" / "new_untracked.py").write_text("c = 9\n", encoding="utf-8")

    _, kv_auto_bw, _ = _run_ok(["--repo", str(repo)], tmp_path=tmp_path)
    assert kv_auto_bw["REVIEW_SCOPE"].startswith("branch-and-working (")
    assert kv_auto_bw["CHANGED_FILES_COUNT"] == "3"

    _, kv_flag_bw, _ = _run_ok(
        ["--repo", str(repo), "--branch-and-working"], tmp_path=tmp_path
    )
    assert kv_flag_bw["REVIEW_SCOPE"].startswith("branch-and-working (")
    assert kv_flag_bw["CHANGED_FILES_COUNT"] == "3"

    sdir_staged_path, kv_staged_path, _ = _run_ok(
        ["--repo", str(repo), "--staged", "src"], tmp_path=tmp_path
    )
    assert kv_staged_path["REVIEW_SCOPE"] == "paths:src (staged)"
    assert (sdir_staged_path / "changed_files.txt").read_text(encoding="utf-8").splitlines() == [
        "src/a.py"
    ]

    sdir_work_path, kv_work_path, _ = _run_ok(
        ["--repo", str(repo), "--working", "src/b.py"], tmp_path=tmp_path
    )
    assert kv_work_path["REVIEW_SCOPE"] == "paths:src/b.py (working-tree)"
    assert (sdir_work_path / "changed_files.txt").read_text(encoding="utf-8").splitlines() == [
        "src/b.py"
    ]

    abs_target = str(repo / "src" / "new_untracked.py")
    sdir_pdirty, kv_pdirty, _ = _run_ok(
        ["--repo", str(repo), "src/a.py", abs_target], tmp_path=tmp_path
    )
    assert kv_pdirty["REVIEW_SCOPE"].startswith("paths:")
    assert (sdir_pdirty / "changed_files.txt").read_text(encoding="utf-8").splitlines() == [
        "src/a.py",
        "src/new_untracked.py",
    ]
    assert "=== UNTRACKED NEW FILE: src/new_untracked.py ===" in (
        sdir_pdirty / "diff.patch"
    ).read_text(encoding="utf-8")

    # Clean worktree with `.git` gitfile and case-variant `.GIT` exclusion
    _git(repo, "reset", "-q", "--hard", "HEAD")
    (repo / "src" / "new_untracked.py").unlink()
    _git(repo, "checkout", "-q", "trunk-base")
    (repo / "src" / "a.py").write_text("a = 10\n", encoding="utf-8")
    _commit_all(repo, "second commit on base")
    _git(repo, "push", "-q", "origin", "trunk-base")

    _, kv_latest, _ = _run_ok(["--repo", str(repo)], tmp_path=tmp_path)
    assert kv_latest["REVIEW_SCOPE"] == "latest-commit (HEAD~1..HEAD)"

    wt_dir = tmp_path / "linked_wt"
    _git(repo, "worktree", "add", "-q", "-b", "wt-clean", str(wt_dir), "trunk-base")
    assert (wt_dir / ".git").is_file()
    sdir_wt, _, _ = _run_ok(["--repo", str(wt_dir), "."], tmp_path=tmp_path)
    wt_files = (sdir_wt / "changed_files.txt").read_text(encoding="utf-8").splitlines()
    assert "src/a.py" in wt_files
    assert ".git" not in wt_files
    # Direct `.GIT` pathspec must also be excluded case-insensitively
    sdir_ci_git, _, _ = _run_ok(["--repo", str(wt_dir), ".GIT"], tmp_path=tmp_path)
    assert (sdir_ci_git / "changed_files.txt").read_text(encoding="utf-8").splitlines() == []


def test_deleted_dotdot_ref_named_mixed_outside_and_literal_pathspecs(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "repo")
    (repo / "sub").mkdir()
    (repo / "v1..v2.rs").write_text("pub fn a() { unsafe {} }\n", encoding="utf-8")
    (repo / "deleted.rs").write_text("pub fn b() { unsafe {} }\n", encoding="utf-8")
    (repo / "main").write_text("ref-named file\n", encoding="utf-8")
    (repo / "[id].rs").write_text("pub fn c() -> i32 { 1 }\n", encoding="utf-8")
    (repo / "other.rs").write_text("pub fn d() -> i32 { 2 }\n", encoding="utf-8")
    _commit_all(repo, "init")

    (repo / "v1..v2.rs").unlink()
    (repo / "deleted.rs").unlink()
    _git(repo, "rm", "-q", "main")
    (repo / "[id].rs").write_text("pub fn c() -> i32 { 99 }\n", encoding="utf-8")
    (repo / "other.rs").write_text("pub fn d() -> i32 { 99 }\n", encoding="utf-8")

    # Deleted file containing `..` is classified as `paths:`, not `range:`
    sdir1, kv1, _ = _run_ok(["--repo", str(repo), "v1..v2.rs"], tmp_path=tmp_path)
    assert kv1["REVIEW_SCOPE"] == "paths:v1..v2.rs"
    assert kv1["HAS_RUST_UNSAFE"] == "1"
    assert (sdir1 / "changed_files.txt").read_text(encoding="utf-8").splitlines() == ["v1..v2.rs"]

    # Deleted `sub/../deleted.rs` + out-of-repo sibling `../outside.rs` still succeeds
    sdir2, kv2, _ = _run_ok(
        ["--repo", str(repo), "sub/../deleted.rs", "../outside.rs"],
        tmp_path=tmp_path,
    )
    assert kv2["REVIEW_SCOPE"] == "paths:sub/../deleted.rs ../outside.rs"
    assert kv2["HAS_RUST_UNSAFE"] == "1"
    assert (sdir2 / "changed_files.txt").read_text(encoding="utf-8").splitlines() == ["deleted.rs"]

    # Explicit `--staged main` after `git rm main` reviews path `main`, not branch `main`
    sdir_ref_file, kv_ref_file, _ = _run_ok(
        ["--repo", str(repo), "--staged", "main"],
        tmp_path=tmp_path,
    )
    assert kv_ref_file["REVIEW_SCOPE"] == "paths:main (staged)"
    assert (sdir_ref_file / "changed_files.txt").read_text(encoding="utf-8").splitlines() == [
        "main"
    ]

    # Literal pathspecs: `[id].rs` matches literally; `:!other.rs` does not trigger exclude magic
    sdir3, _, _ = _run_ok(["--repo", str(repo), "[id].rs"], tmp_path=tmp_path)
    assert (sdir3 / "changed_files.txt").read_text(encoding="utf-8").splitlines() == ["[id].rs"]

    sdir4, _, _ = _run_ok(["--repo", str(repo), ":!other.rs"], tmp_path=tmp_path)
    assert (sdir4 / "changed_files.txt").read_text(encoding="utf-8").splitlines() == []


# ---------------------------------------------------------------------------
# 5. Security, git-config/submodule/promisor adversaries, hunk bounds & encoding
# ---------------------------------------------------------------------------


def test_git_config_adversaries_nonutf8_filter_fifos_global_ignore_and_cquoted_paths(
    tmp_path: Path,
) -> None:
    repo = _init_git_repo(tmp_path / "repo")
    marker = tmp_path / "evil_ran_marker"
    evil_script = tmp_path / "evil_driver.sh"
    evil_script.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    os.chmod(evil_script, 0o755)

    order_fifo = tmp_path / "order.fifo"
    excl_fifo = tmp_path / "excludes.fifo"
    attr_fifo = tmp_path / "attrs.fifo"
    mailmap_fifo = tmp_path / "mailmap.fifo"
    for fifo in (order_fifo, excl_fifo, attr_fifo, mailmap_fifo):
        os.mkfifo(fifo)

    # Configure a SAFE global gitignore + global git config to verify global excludes still work
    global_ignore = tmp_path / "global_ignore"
    global_ignore.write_text("*.globally_ignored\n", encoding="utf-8")
    global_cfg = tmp_path / "global_gitconfig"
    global_cfg.write_text(f"[core]\n\texcludesFile = {global_ignore}\n", encoding="utf-8")

    (repo / ".gitattributes").write_bytes(
        b"*.rs diff=evil filter=a=b\n*.txt filter=bad\xffname\n"
    )
    special_names = ["a\tb.rs", 'quote"name.rs', "back\\slash.rs", "space name.rs"]
    for name in special_names:
        (repo / name).write_text("pub fn f() { unsafe { } }\n", encoding="utf-8")
    (repo / "sample.txt").write_text("line1\n", encoding="utf-8")
    root_sha = _commit_all(repo, "init special rs files")

    cfg_path = repo / ".git" / "config"
    cfg_bytes = cfg_path.read_bytes()
    cfg_bytes += (
        f'\n[filter "a=b"]\n\tclean = "{evil_script}"\n\tsmudge = "{evil_script}"\n'
        f'\tprocess = "{evil_script}"\n\trequired = true\n'
    ).encode("utf-8")
    cfg_bytes += (
        b'\n[filter "bad\xffname"]\n\tclean = "'
        + str(evil_script).encode("utf-8")
        + b'"\n\trequired\n'
    )
    cfg_bytes += (
        f'[core]\n\texcludesFile = "{excl_fifo}"\n\tattributesFile = "{attr_fifo}"\n'
        f'[diff]\n\tnoprefix = true\n\texternal = "{evil_script}"\n\torderFile = "{order_fifo}"\n'
        f'[diff "evil"]\n\ttextconv = "{evil_script}"\n'
        f"[color]\n\tui = always\n"
        f"[log]\n\tshowSignature = true\n\tmailmap = true\n"
        f'[mailmap]\n\tfile = "{mailmap_fifo}"\n'
        f'[gpg]\n\tprogram = "{evil_script}"\n'
        "[format]\n\tpretty = format:--- a/spoof.rs%n+++ b/spoof.rs%n@@ -1 +1 @@%n+unsafe {}%n\n"
    ).encode("utf-8")
    cfg_path.write_bytes(cfg_bytes)

    # Also test GIT_CONFIG=/dev/null bypass attempt + preserved safe global excludesFile
    (repo / "skip.globally_ignored").write_text("should stay ignored\n", encoding="utf-8")
    env_override = {
        "GIT_CONFIG": os.devnull,
        "GIT_CONFIG_GLOBAL": str(global_cfg),
    }

    sdir_init, kv_init, _ = _run_ok(
        ["--repo", str(repo)], tmp_path=tmp_path, env_extra=env_override
    )
    assert not marker.exists(), "hostile filter, diff.external, textconv, or gpg.program executed"
    assert kv_init["REVIEW_SCOPE"] == "initial-commit (HEAD)"
    assert "--- a/spoof.rs".encode("utf-8") not in (sdir_init / "diff.patch").read_bytes()
    assert "skip.globally_ignored" not in (sdir_init / "untracked_files.txt").read_text(
        encoding="utf-8"
    )

    _run_ok(["--repo", str(repo), root_sha], tmp_path=tmp_path, env_extra=env_override)
    assert not marker.exists()

    # Delete `unsafe` from special .rs files & touch sample.txt (tests non-UTF-8 filter override)
    for name in special_names:
        (repo / name).write_text("pub fn f() {}\n", encoding="utf-8")
    (repo / "sample.txt").write_text("line2\n", encoding="utf-8")

    sdir, kv, _ = _run_ok(["--repo", str(repo)], tmp_path=tmp_path, env_extra=env_override)
    assert not marker.exists(), "hostile repo git config or non-UTF-8 filter executed unexpectedly"
    assert kv["HAS_RUST"] == "1"
    assert kv["HAS_RUST_UNSAFE"] == "1"
    assert b"\x1b[" not in (sdir / "diff.patch").read_bytes()
    assert "skip.globally_ignored" not in (sdir / "changed_files.txt").read_text(encoding="utf-8")


def test_submodule_child_git_and_partial_clone_promisor_do_not_execute(tmp_path: Path) -> None:
    marker = tmp_path / "sub_or_promisor_ran"
    evil_script = tmp_path / "evil_exec.sh"
    evil_script.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    os.chmod(evil_script, 0o755)

    # 1. Submodule with diff.submodule=diff AND same-size dirty file + submodule clean filter
    sub_src = _init_git_repo(tmp_path / "sub_src")
    (sub_src / ".gitattributes").write_text("*.txt filter=subevil\n", encoding="utf-8")
    (sub_src / "data.txt").write_text("aaaa\n", encoding="utf-8")
    _commit_all(sub_src, "sub init")

    parent = _init_git_repo(tmp_path / "parent_repo")
    _git(parent, "-c", "protocol.file.allow=always", "submodule", "-q", "add", str(sub_src), "sub")
    _commit_all(parent, "add submodule")

    _git(parent, "config", "diff.submodule", "diff")
    _git(parent / "sub", "config", "filter.subevil.clean", str(evil_script))
    _git(parent / "sub", "config", "diff.external", str(evil_script))
    # Same-size modification in submodule triggers `filter.*.clean` if parent spawns `git status`
    (parent / "sub" / "data.txt").write_text("bbbb\n", encoding="utf-8")
    (parent / "root_edit.py").write_text("x = 1\n", encoding="utf-8")

    _run_ok(["--repo", str(parent)], tmp_path=tmp_path)
    assert not marker.exists(), "submodule diff.external or clean filter executed!"

    # 2. Partial-clone promisor repository with missing blob must NOT invoke uploadpack
    origin_repo = _init_git_repo(tmp_path / "origin_repo")
    (origin_repo / "file.txt").write_text("v1 content\n", encoding="utf-8")
    _commit_all(origin_repo, "c1")
    (origin_repo / "file.txt").write_text("v2 content\n", encoding="utf-8")
    _commit_all(origin_repo, "c2")

    partial_repo = tmp_path / "partial_repo"
    _git(
        tmp_path,
        "clone",
        "-q",
        "--filter=blob:none",
        "--no-checkout",
        f"file://{origin_repo}",
        str(partial_repo),
    )
    _git(partial_repo, "config", "remote.origin.uploadpack", str(evil_script))
    _git(partial_repo, "config", "core.sshCommand", str(evil_script))

    _run_ok(["--repo", str(partial_repo), "HEAD~1..HEAD"], tmp_path=tmp_path)
    assert not marker.exists(), "partial-clone promisor lazy fetch executed uploadpack!"


def test_hunk_boundaries_bare_cr_sql_comment_combined_cc_and_double_plus(tmp_path: Path) -> None:
    sql_repo = _init_git_repo(tmp_path / "sql_repo")
    (sql_repo / "query.sql").write_text("-- x\nSELECT 1;\n", encoding="utf-8")
    _commit_all(sql_repo, "init sql")
    (sql_repo / "query.sql").write_text("-- unsafe note\nSELECT 1;\n", encoding="utf-8")

    _, kv_sql, _ = _run_ok(["--repo", str(sql_repo)], tmp_path=tmp_path)
    assert kv_sql["HAS_RUST_UNSAFE"] == "0"
    assert "(+1 / -1)" in kv_sql["DIFF_PATCH_LINES"]

    rs_repo = _init_git_repo(tmp_path / "rs_repo")
    (rs_repo / "lib.rs").write_bytes(
        b"fn demo() {\n"
        b'    let _s = "old";\n'
        b"    let _k = 1;\n"
        b"    unsafe { std::hint::black_box(1); }\n"
        b"}\n"
    )
    _commit_all(rs_repo, "init rs")
    (rs_repo / "lib.rs").write_bytes(
        b"fn demo() {\n"
        b'    let _s = "foo\rbar\n'
        b"++ b/not_rust.md\n"
        b'";\n'
        b"    let _k = 1;\n"
        b"    let _safe = 1;\n"
        b"}\n"
    )

    sdir_rs, kv_rs, _ = _run_ok(["--repo", str(rs_repo)], tmp_path=tmp_path)
    diff_raw = (sdir_rs / "diff.patch").read_bytes()
    assert b"\n+++ b/not_rust.md\n" in diff_raw
    assert b"foo\rbar" in diff_raw
    assert kv_rs["HAS_RUST"] == "1"
    assert kv_rs["HAS_RUST_UNSAFE"] == "1"

    # Direct unit tests of _analyze_diff_bytes (bare \r, +++/---, combined diff --cc/@@@, bad hdr)
    synthetic_rs = (
        b"diff --git a/src/lib.rs b/src/lib.rs\n"
        b"--- a/src/lib.rs\n"
        b"+++ b/src/lib.rs\n"
        b"@@ -1,4 +1,4 @@\n"
        b" fn demo() {\n"
        b'+let x = "a\rb";\n'
        b"+++ b/fake.md\n"
        b"--- a/fake.md\n"
        b"-    unsafe { boom(); }\n"
        b" }\n"
    )
    res_rs = crc._analyze_diff_bytes(synthetic_rs)
    assert res_rs.has_rust_unsafe is True
    assert res_rs.added_lines == 2
    assert res_rs.removed_lines == 2

    synthetic_cc = (
        b"diff --cc core.rs\n"
        b"index 1111111,2222222..3333333\n"
        b"--- a/core.rs\n"
        b"+++ b/core.rs\n"
        b"@@@ -1,1 -1,1 +1,1 @@@\n"
        b"- fn run() { let v = 1; }\n"
        b" -fn run() { unsafe { } }\n"
        b"++fn run() { let v = 1; }\n"
    )
    res_cc = crc._analyze_diff_bytes(synthetic_cc)
    assert res_cc.has_rust_unsafe is True
    assert res_cc.added_lines == 1
    assert res_cc.removed_lines == 2

    synthetic_sql = (
        b"diff --git a/q.sql b/q.sql\n"
        b"--- a/q.sql\n"
        b"+++ b/q.sql\n"
        b"@@ -1,2 +1,2 @@\n"
        b"--- x\n"
        b"+-- unsafe comment\n"
        b" SELECT 1;\n"
    )
    res_sql = crc._analyze_diff_bytes(synthetic_sql)
    assert res_sql.has_rust_unsafe is False
    assert res_sql.added_lines == 1
    assert res_sql.removed_lines == 1

    for bad_header in (
        b"diff --git a/foo.md b/foo.md\n@@ -1 +1 @@\n+unsafe {}\n",
        b"diff --git a/foo.md b/foo.md\n--- no_prefix.md\n+++ no_prefix.md\n@@ -1 +1 @@\n+unsafe\n",
        b'diff --git a/x b/x\n--- "a/unterminated.md\n+++ b/x.md\n@@ -1 +1 @@\n-unsafe\n',
    ):
        assert crc._analyze_diff_bytes(bad_header).has_rust_unsafe is True


def test_option_injection_symlinks_fifo_binary_truncation_control_chars_and_ascii_locale(
    tmp_path: Path,
) -> None:
    repo = _init_git_repo(tmp_path / "repo")
    (repo / "latin1.txt").write_bytes(b"caf\xe9\n")
    (repo / "caf\u00e9.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "init")

    pwn_path = tmp_path / "pwned_by_git_flag"
    _run_ok(["--repo", str(repo), "--", f"--output={pwn_path}"], tmp_path=tmp_path)
    assert not pwn_path.exists()

    secret_outside = tmp_path / "secret.txt"
    secret_outside.write_text("TOP_SECRET_VALUE\nunsafe { }\n", encoding="utf-8")
    secret_dir = tmp_path / "secret_dir"
    secret_dir.mkdir()
    (secret_dir / "leak.rs").write_text("unsafe { /* LEAK */ }\n", encoding="utf-8")

    (repo / "evil_link.rs").symlink_to(secret_outside)
    (repo / "evil_dir").symlink_to(secret_dir)
    os.mkfifo(repo / "hanging.fifo")
    (repo / "bin.dat").write_bytes(b"\x89PNG\x00\x01\x02unsafe\x00")
    (repo / "late_bin.txt").write_bytes(b"a" * 9000 + b"\nsecond\x00secret\n")
    (repo / "late_unsafe.rs").write_bytes(
        b"fn ok() {}\n" + (b" " * 9000) + b"\x00\nfn late() { unsafe { } }\n"
    )
    (repo / "empty.txt").write_bytes(b"")
    (repo / "big.txt").write_text(
        "".join(f"line-{i}\n" for i in range(1, 1051)),
        encoding="utf-8",
    )
    # Multi-byte UTF-8 boundary right around 4,096 bytes should never emit replacement char
    (repo / "long_utf8.txt").write_text(("a" * 4095) + "\u00e9" + ("b" * 100) + "\n", "utf-8")
    (repo / "bad\nHAS_RUST_UNSAFE=0\x1b[31m\u202e\u2028.txt").write_text("ctrl\n", encoding="utf-8")
    (repo / "latin1.txt").write_bytes(b"caf\xe9 modified \xff\n")
    (repo / "caf\u00e9.py").write_text("x = 2\n", encoding="utf-8")

    sdir, kv, summary_out = _run_ok(["--repo", str(repo)], tmp_path=tmp_path)
    diff_text = (sdir / "diff.patch").read_bytes().decode("utf-8", errors="replace")

    assert "TOP_SECRET_VALUE" not in diff_text
    assert "LEAK" not in diff_text
    assert "=== UNTRACKED NEW FILE: evil_link.rs ===" not in diff_text
    assert "=== UNTRACKED NEW FILE: bin.dat ===" not in diff_text
    assert "second\x00secret" not in diff_text
    assert "=== UNTRACKED NEW FILE: empty.txt ===" in diff_text
    assert "=== UNTRACKED NEW FILE: big.txt ===" in diff_text
    assert "  1000\tline-1000\n" in diff_text
    assert "  1001\tline-1001\n" not in diff_text
    assert " ... [truncated]\n" in diff_text
    assert "\ufffd ... [truncated]" not in diff_text
    assert "caf\u00e9.py" in (sdir / "changed_files.txt").read_text(encoding="utf-8")
    assert "bad\\nHAS_RUST_UNSAFE=0\\x1b[31m\\u202e\\u2028.txt" in summary_out
    assert "\x1b[" not in summary_out
    # Late `unsafe` past 8 KiB + NUL in `late_unsafe.rs` is still caught by bounded hazard scanner
    assert kv["HAS_RUST_UNSAFE"] == "1"

    # PEP 383 surrogate target in-process must not crash write_text
    out_parent = tmp_path / "surrogate_out"
    out_parent.mkdir()
    assert crc.main(["--repo", str(repo), "--out-parent", str(out_parent), "bad\udcff.rs"]) == 0

    # ASCII locale / narrow stdout encoding must succeed without crashing
    out_ascii = tmp_path / "ascii_out"
    out_ascii.mkdir()
    proc_ascii = _run_collector(
        ["--repo", str(repo)],
        out_parent=out_ascii,
        env_extra={"LC_ALL": "C", "LANG": "C", "PYTHONIOENCODING": "ascii"},
    )
    assert proc_ascii.returncode == 0, proc_ascii.stderr
    assert "IS_GIT_REPO=1" in proc_ascii.stdout


# ---------------------------------------------------------------------------
# 6. Languages, hazards, 80-line cap & skill artifact integrity
# ---------------------------------------------------------------------------


def test_cpp_suffixes_rust_unsafe_scoping_and_hazard_cap(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "repo")
    cpp_exts = [
        ".c", ".cc", ".cpp", ".cxx", ".c++", ".h", ".hh", ".hpp",
        ".hxx", ".ipp", ".inl", ".cu", ".cuh", ".m", ".mm",
    ]
    for idx, ext in enumerate(cpp_exts):
        (repo / f"f{idx}{ext}").write_text("int x = 0;\n", encoding="utf-8")

    (repo / "README.md").write_text(
        'Do not use unsafe operations or extern "C" or AtomicUsize or transmute.\n',
        encoding="utf-8",
    )
    (repo / "config.json").write_text(
        '{"note": "UnsafeCell ctypes pthread_create"}\n',
        encoding="utf-8",
    )
    (repo / "note.py").write_text('MSG = "unsafe pattern"\n', encoding="utf-8")
    _commit_all(repo, "init")

    sdir, kv, _ = _run_ok(["--repo", str(repo)], tmp_path=tmp_path)
    assert kv["HAS_CPP"] == "1"
    assert kv["HAS_RUST"] == "0"
    assert kv["HAS_RUST_UNSAFE"] == "0"
    assert kv["HAS_FFI"] == "0"
    assert kv["HAS_CONCURRENCY_HAZARDS"] == "0"
    repo_files = (sdir / "repo_files.txt").read_text(encoding="utf-8").splitlines()
    assert "README.md" in repo_files and "note.py" in repo_files
    summary_text = (sdir / "summary.txt").read_text(encoding="utf-8")
    for idx, ext in enumerate(cpp_exts):
        assert f"f{idx}{ext}" in summary_text

    # Oversized single line (>64 KiB) with `unsafe` & `AtomicUsize` past the 64 KiB boundary
    boundary_line = (" " * 65533) + "unsafe { let _ = AtomicUsize::new(0); }"
    hazard_lines = [
        'extern "C-unwind" fn combo() { let _ = core::mem::transmute::<u8, u8>(0); }',
        '#[unsafe(no_mangle)] pub extern "C" fn exported() {}',
        "unsafe impl Send for Foo {}",
        "co_await task();",
        boundary_line,
    ]
    hazard_lines.extend(f"let _a{i} = AtomicUsize::new({i});" for i in range(90))
    (repo / "hazards.rs").write_text("\n".join(hazard_lines) + "\n", encoding="utf-8")

    sdir2, kv2, summary2 = _run_ok(["--repo", str(repo)], tmp_path=tmp_path)
    assert kv2["HAS_RUST"] == "1"
    assert kv2["HAS_RUST_UNSAFE"] == "1"
    assert kv2["HAS_FFI"] == "1"
    assert kv2["HAS_CONCURRENCY_HAZARDS"] == "1"

    all_hits = (sdir2 / "hazard_hits.txt").read_text(encoding="utf-8").splitlines()
    assert len(all_hits) == 95
    assert len([h for h in all_hits if "hazards.rs:1:" in h]) == 1
    assert any("hazards.rs:5:" in h and " ... [truncated]" in h for h in all_hits)

    marker_header = "--- LOW-LEVEL / SAFETY / CONCURRENCY MARKERS (FIRST 80) ---"
    assert marker_header in summary2
    summary_marker_lines = summary2.split(marker_header + "\n", 1)[1].splitlines()
    assert len(summary_marker_lines) == 80


def test_review_skill_artifacts_token_isolation_and_adapter_contract() -> None:
    core_dir = REAL_REPO / "core" / "review"
    adapter_dir = REAL_REPO / "harnesses" / "agy" / "review"
    adapter_skill = adapter_dir / "SKILL.md"
    core_link = adapter_dir / "core"

    assert SCRIPT_PATH.is_file()
    assert os.access(SCRIPT_PATH, os.X_OK)

    assert adapter_skill.is_file()
    adapter_text = adapter_skill.read_text(encoding="utf-8")
    adapter_lines = adapter_text.splitlines()
    assert 60 <= len(adapter_lines) <= 100
    assert init.validate_skill_dir(adapter_dir) == []

    # Verify adapter explicitly binds all required abstract core concepts & AGY rules
    for required_binding in (
        "MODEL_MAP",
        "SPAWN_WORKER",
        "WAIT_FOR_WORKERS",
        "SWITCH_MODEL",
        "HANDOFF",
        "READ_ARTIFACT",
        "KILL_WORKER",
        "CARBON_HIGH",
        "NEON_XHIGH",
        'workspace: "inherit"',
        "tasks:",
        "swarm_wait",
        "read_board",
        "post_message",
        "mktemp -d /tmp/agy-collect.XXXXXX",
        "1601..2400",
    ):
        assert required_binding in adapter_text, f"Missing adapter binding: {required_binding!r}"

    assert core_link.is_symlink()
    assert os.readlink(core_link) == "../../../core/review"
    assert core_link.resolve(strict=True) == core_dir.resolve(strict=True)

    at_coord = "@" + "coordinator"
    forbidden_patterns: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("swarm_wait", re.compile(r"\bswarm_wait\b")),
        ("read_board", re.compile(r"\bread_board\b")),
        ("post_message", re.compile(r"\bpost_message\b")),
        ("invoke_subagent", re.compile(r"\binvoke_subagent\b")),
        ("open_board", re.compile(r"\bopen_board\b")),
        ("swarm{", re.compile(r"swarm\{")),
        ("artifactDir", re.compile(r"\bartifactDir\b")),
        ("customization://", re.compile(r"customization://")),
        ("#seq", re.compile(r"#seq\b")),
        ("Carbon High", re.compile(r"\bCarbon\s*\(?High\)?", re.IGNORECASE)),
        ("Neon xhigh", re.compile(r"\bNeon\s*\(?(?:xhigh|Extra\s+High)\)?", re.IGNORECASE)),
        ("Opus 5.5", re.compile(r"\bOpus\s*5\.5", re.IGNORECASE)),
        ("CARBON_HIGH", re.compile(r"\bCARBON_HIGH\b")),
        ("NEON_XHIGH", re.compile(r"\bNEON_XHIGH\b")),
        ("flash_lite", re.compile(r"\bflash_lite\b")),
        ("at_coordinator", re.compile(re.escape(at_coord))),
    )

    scanned_files = sorted(
        p for p in core_dir.rglob("*") if p.is_file() and "__pycache__" not in p.parts
    )
    expected_core = {
        core_dir / "algorithm.md",
        core_dir / "playbooks" / "correctness-style.md",
        core_dir / "playbooks" / "security-adversarial.md",
        core_dir / "playbooks" / "safety-cpp-rust.md",
        core_dir / "playbooks" / "verifier.md",
        SCRIPT_PATH,
    }
    assert set(scanned_files) == expected_core

    # Verify scope-discipline, threat-model calibration, and right-sized tiering invariants
    algo_text = (core_dir / "algorithm.md").read_text(encoding="utf-8")
    assert "Out-of-Scope Notes" in algo_text
    assert "Small Diff" in algo_text and "Standard Diff" in algo_text and "Large Diff" in algo_text
    assert "Direct Delta Check" in algo_text and "No Goalpost-Moving" in algo_text
    sec_adv_text = (core_dir / "playbooks" / "security-adversarial.md").read_text(encoding="utf-8")
    assert "Local Developer CLI" in sec_adv_text and "Proportional" in sec_adv_text
    for path in scanned_files:
        text = path.read_text(encoding="utf-8").replace("agy-review.", "<TMP_PREFIX>")
        for label, pattern in forbidden_patterns:
            assert pattern.search(text) is None, (
                f"Forbidden harness/model token {label!r} found in {path.relative_to(REAL_REPO)}"
            )

    # Verify collect_review_context.py fits within SKILL.md's 3x800-line paginated read budget
    script_lines = SCRIPT_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    assert len(script_lines) <= 2400
    for start in (0, 800, 1600):
        slice_bytes = sum(len(line.encode("utf-8")) for line in script_lines[start : start + 800])
        assert slice_bytes < 46080


def test_global_relative_and_superseded_excludes_preview_crlf_utf8_chunk_split_and_upper_exts(
    tmp_path: Path,
) -> None:
    # 1. Global relative excludesFile pointing to in-repo FIFO + superseded safe global excludes
    repo = _init_git_repo(tmp_path / "repo")
    os.mkfifo(repo / "rel_excl.fifo")
    (repo / "keep.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "init")
    (repo / "skip.globally_ignored").write_text("untracked\n", encoding="utf-8")

    safe_ignore = tmp_path / "safe_global_ignore"
    safe_ignore.write_text("*.globally_ignored\n", encoding="utf-8")

    # Case 1a: Global relative path pointing at in-repo FIFO -> neutralized to devnull (no hang)
    rel_global_cfg = tmp_path / "rel_global_cfg"
    rel_global_cfg.write_text(
        "[core]\n\texcludesFile = rel_excl.fifo\n\tattributesFile = %(prefix)/etc/attrs\n",
        encoding="utf-8",
    )
    sdir_rel, _, _ = _run_ok(
        ["--repo", str(repo)],
        tmp_path=tmp_path,
        env_extra={"GIT_CONFIG_GLOBAL": str(rel_global_cfg)},
    )
    assert "skip.globally_ignored" in (sdir_rel / "untracked_files.txt").read_text(encoding="utf-8")

    # Case 1b: Earlier safe global excludesFile superseded by later unsafe relative excludesFile
    # MUST NOT resurrect the earlier safe_global_ignore
    sup_global_cfg = tmp_path / "sup_global_cfg"
    sup_global_cfg.write_text(
        f"[core]\n\texcludesFile = {safe_ignore}\n\texcludesFile = rel_excl.fifo\n",
        encoding="utf-8",
    )
    sdir_sup, _, _ = _run_ok(
        ["--repo", str(repo)],
        tmp_path=tmp_path,
        env_extra={"GIT_CONFIG_GLOBAL": str(sup_global_cfg)},
    )
    assert "skip.globally_ignored" in (sdir_sup / "untracked_files.txt").read_text(encoding="utf-8")

    # 2. Exact 4,096-byte CRLF untracked line NOT truncated; 4,097-byte LF IS truncated
    (repo / "exact_4096_crlf.txt").write_bytes(b"A" * 4096 + b"\r\n")
    (repo / "over_4097_lf.txt").write_bytes(b"B" * 4097 + b"\n")
    sdir_prev, _, _ = _run_ok(["--repo", str(repo)], tmp_path=tmp_path)
    diff_prev = (sdir_prev / "diff.patch").read_text(encoding="utf-8")
    assert f"     1\t{'A' * 4096}\n" in diff_prev
    assert f"     1\t{'B' * 4096} ... [truncated]\n" in diff_prev

    # 3. _trim_incomplete_utf8 next_byte & RFC 3629 lead-byte unit checks
    assert crc._trim_incomplete_utf8(b"abc\xc0", next_byte=ord("x")) == b"abc\xc0"
    assert crc._trim_incomplete_utf8(b"abc\xf5", next_byte=0x80) == b"abc\xf5"
    assert crc._trim_incomplete_utf8(b"abc\xe2\x82", next_byte=0xAC) == b"abc"
    assert crc._trim_incomplete_utf8(b"abc\xc3\xa9", next_byte=ord("z")) == b"abc\xc3\xa9"

    # 4. 64 KiB chunk-boundary identifier split: `unsafe_fn` must NOT trigger; `unsafe {}` MUST
    chunk_repo = _init_git_repo(tmp_path / "chunk_repo")
    (chunk_repo / "false_split.rs").write_bytes((b" " * 65530) + b"unsafe_fn();\n")
    _, kv_false, _ = _run_ok(["--repo", str(chunk_repo)], tmp_path=tmp_path)
    assert kv_false["HAS_RUST"] == "1"
    assert kv_false["HAS_RUST_UNSAFE"] == "0"

    (chunk_repo / "true_split.rs").write_bytes((b" " * 65530) + b"unsafe {}\n")
    _, kv_true, _ = _run_ok(["--repo", str(chunk_repo)], tmp_path=tmp_path)
    assert kv_true["HAS_RUST_UNSAFE"] == "1"

    # 5. Uppercase C/C++/Rust suffixes (.CPP, .H, .RS) in files and diffs
    upper_repo = _init_git_repo(tmp_path / "upper_repo")
    (upper_repo / "MOD.CPP").write_text("int main() { return 0; }\n", encoding="utf-8")
    (upper_repo / "HDR.H").write_text("#pragma once\n", encoding="utf-8")
    (upper_repo / "LIB.RS").write_text("pub fn f() { unsafe { } }\n", encoding="utf-8")
    _commit_all(upper_repo, "init upper")

    # Delete `unsafe` from LIB.RS so diff-only detection verifies case-insensitive `.RS` header
    (upper_repo / "LIB.RS").write_text("pub fn f() {}\n", encoding="utf-8")
    (upper_repo / "MOD.CPP").write_text("int main() { return 1; }\n", encoding="utf-8")
    _, kv_up, _ = _run_ok(["--repo", str(upper_repo)], tmp_path=tmp_path)
    assert kv_up["HAS_CPP"] == "1"
    assert kv_up["HAS_RUST"] == "1"
    assert kv_up["HAS_RUST_UNSAFE"] == "1"
