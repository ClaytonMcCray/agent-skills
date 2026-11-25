"""Unit, integration, and contract tests for manage_worktree.py and the dev-swarm skill."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType

import pytest

import init

REAL_REPO = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REAL_REPO / "core" / "dev-swarm" / "scripts" / "manage_worktree.py"


def _load_worktree_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("manage_worktree", SCRIPT_PATH)
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


mwt = _load_worktree_module()

_HERMETIC_GIT_CONFIG_ENV: dict[str, str] = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_ATTR_NOSYSTEM": "1",
}
_TEST_GIT_IDENTITY_ENV: dict[str, str] = {
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "author@example.test",
    "GIT_COMMITTER_NAME": "Test Committer",
    "GIT_COMMITTER_EMAIL": "committer@example.test",
}


@pytest.fixture(autouse=True)
def _isolate_default_tmp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep default worktree bases and in-process git identity strictly hermetic."""
    isolated_tmp = tmp_path / "isolated_sys_tmp"
    isolated_tmp.mkdir()
    monkeypatch.setattr(mwt, "_DEFAULT_TMP_ROOT", isolated_tmp)
    monkeypatch.delenv("EMAIL", raising=False)
    for k, v in (*_HERMETIC_GIT_CONFIG_ENV.items(), *_TEST_GIT_IDENTITY_ENV.items()):
        monkeypatch.setenv(k, v)
    return isolated_tmp


def _git_env(
    extra: dict[str, str] | None = None,
    *,
    include_identity: bool = True,
) -> dict[str, str]:
    env = os.environ.copy()
    for key in (*_TEST_GIT_IDENTITY_ENV, "EMAIL"):
        env.pop(key, None)
    env.update(_HERMETIC_GIT_CONFIG_ENV)
    if include_identity:
        env.update(_TEST_GIT_IDENTITY_ENV)
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


def _init_repo(repo: Path, *, branch: str = "main") -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", branch)
    return repo


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _seed_repo(repo: Path, files: Mapping[str, str], message: str = "initial") -> str:
    """Create a git repository populated with files and an initial commit."""
    _init_repo(repo)
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return _commit_all(repo, message)


def _write_executable(path: Path, body: str) -> Path:
    """Write a POSIX 0o755 shell script."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o755)
    return path


def _parse_kv(output: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, val = line.split("=", 1)
            parsed[key] = val
    return parsed


def _run_cli(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
    include_identity: bool = True,
    executable: str = sys.executable,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [executable, str(SCRIPT_PATH), *args],
        cwd=str(cwd) if cwd is not None else None,
        env=_git_env(env_extra, include_identity=include_identity),
        capture_output=True,
        text=True,
        check=False,
    )


def _create_wt(
    repo: Path,
    wt_base: Path,
    slice_id: str,
    base_ref: str | None = None,
) -> tuple[Path, str, str]:
    cmd = ["--repo", str(repo), "--wt-base", str(wt_base), "create", slice_id]
    if base_ref is not None:
        cmd.append(base_ref)
    proc = _run_cli(cmd)
    assert proc.returncode == 0, f"stderr={proc.stderr!r}\nstdout={proc.stdout!r}"
    kv = _parse_kv(proc.stdout)
    return (Path(kv["WORKTREE_PATH"]), kv["WORKTREE_BRANCH"], kv["BASE_SHA"])


# ---------------------------------------------------------------------------
# 1. Host interpreter smoke tests & CLI flag placement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "executable",
    [sys.executable, "/usr/bin/python3", "/usr/local/bin/python3.11"],
)
def test_host_interpreters_lifecycle_and_flag_placement(
    tmp_path: Path, executable: str
) -> None:
    if not Path(executable).is_file():
        pytest.skip(f"{executable} not installed on this host")

    repo = tmp_path / "repo"
    base_sha = _seed_repo(repo, {"base.txt": "v1\n"})
    wt_base = tmp_path / "wts"

    proc_create = _run_cli(
        ["--repo", str(repo), "--wt-base", str(wt_base), "create", "iter-1"],
        executable=executable,
    )
    assert proc_create.returncode == 0, proc_create.stderr
    assert proc_create.stderr == ""
    kv = _parse_kv(proc_create.stdout)
    wt_path = Path(kv["WORKTREE_PATH"])
    wt_branch = kv["WORKTREE_BRANCH"]
    assert wt_path.is_dir() and wt_path.is_absolute()
    assert wt_branch.startswith("dev-swarm/iter-1-")
    assert kv["BASE_SHA"] == base_sha
    assert list(kv.keys()) == ["WORKTREE_PATH", "WORKTREE_BRANCH", "BASE_SHA"]

    (wt_path / "base.txt").write_text("v2\n", encoding="utf-8")
    (wt_path / "added.py").write_text("x = 1\n", encoding="utf-8")

    proc_status = _run_cli(["status", str(wt_path), "-C", str(repo)], executable=executable)
    assert proc_status.returncode == 0, proc_status.stderr
    assert "base.txt" in proc_status.stdout and "added.py" in proc_status.stdout

    proc_apply = _run_cli(["apply", str(wt_path), "--repo", str(repo)], executable=executable)
    assert proc_apply.returncode == 0, proc_apply.stderr
    assert proc_apply.stdout.splitlines()[0] == f"APPLIED_FROM={wt_path} ({wt_branch})"
    assert "Squash commit" not in proc_apply.stdout
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == base_sha
    assert _git(repo, "diff", "--cached", "--name-only").stdout.splitlines() == [
        "added.py",
        "base.txt",
    ]
    # Non-destructive snapshot leaves wt_path HEAD and uncommitted edits untouched
    assert _git(wt_path, "rev-parse", "HEAD").stdout.strip() == base_sha
    assert "added.py" in _git(wt_path, "status", "--short").stdout

    proc_clean = _run_cli(
        ["cleanup", str(wt_path), wt_branch, "--repo", str(repo), "--wt-base", str(wt_base)],
        executable=executable,
    )
    assert proc_clean.returncode == 0, proc_clean.stderr
    assert proc_clean.stdout == f"CLEANED={wt_path}\n"
    assert not wt_path.exists()
    ref_chk = _git(repo, "show-ref", "--verify", f"refs/heads/{wt_branch}", check=False)
    assert ref_chk.returncode != 0


# ---------------------------------------------------------------------------
# 2. create & status edge cases, relative/broad/symlink bases & stream safety
# ---------------------------------------------------------------------------


def test_create_and_status_edge_cases_and_stream_safety(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Unborn HEAD repo must error cleanly before creating wt_base
    empty_repo = _init_repo(tmp_path / "empty_repo")
    empty_wt_base = tmp_path / "never_created"
    proc_empty = _run_cli(
        ["--repo", str(empty_repo), "--wt-base", str(empty_wt_base), "create", "s1"]
    )
    assert proc_empty.returncode == 1 and "ERROR:" in proc_empty.stderr
    assert not empty_wt_base.exists()

    # Non-git, bare, .git, and hostile core.worktree rejections
    plain = tmp_path / "plain_dir"
    plain.mkdir()
    assert _run_cli(["--repo", str(plain), "create", "s1"]).returncode == 1

    bare = tmp_path / "bare.git"
    bare.mkdir()
    _git(bare, "init", "-q", "--bare")
    assert _run_cli(["--repo", str(bare), "create", "s1"]).returncode == 1

    outer = tmp_path / "outer"
    evil_sub = outer / "evil_sub"
    _seed_repo(evil_sub, {"a.txt": "a\n"})
    _git(evil_sub, "config", "core.worktree", "../..")
    assert _run_cli(["--repo", str(evil_sub), "create", "s1"]).returncode == 1

    repo = tmp_path / "my.repo"
    c1 = _seed_repo(repo, {"sub/f.txt": "v1\n"}, "c1")
    _git(repo, "tag", "v1.0")
    _git(repo, "checkout", "-q", "-b", "alt")
    (repo / "sub" / "f.txt").write_text("alt\n", encoding="utf-8")
    alt_sha = _commit_all(repo, "alt commit")
    _git(repo, "checkout", "-q", "main")
    (repo / "sub" / "f.txt").write_text("v2\n", encoding="utf-8")
    _commit_all(repo, "c2")

    assert _run_cli(["--repo", str(repo / ".git"), "create", "s1"]).returncode == 1

    wt_base = tmp_path / "wts"
    wt_tag, br_tag, sha_tag = _create_wt(repo / "sub", wt_base, "feat/foo bar", "v1.0")
    assert sha_tag == c1 and br_tag.startswith("dev-swarm/feat-foo-bar-")
    assert (wt_tag / "sub" / "f.txt").read_text(encoding="utf-8") == "v1\n"

    wt_alt, _, sha_alt = _create_wt(repo, wt_base, "alt-slice", "alt")
    assert sha_alt == alt_sha
    assert (wt_alt / "sub" / "f.txt").read_text(encoding="utf-8") == "alt\n"

    _, _, sha_exact = _create_wt(repo, wt_base, "sha-slice", c1)
    assert sha_exact == c1

    # Relative --wt-base is anchored to CWD (not repo.root) and returns absolute canonical path
    caller_cwd = tmp_path / "caller_cwd"
    caller_cwd.mkdir()
    proc_rel = _run_cli(
        ["--repo", str(repo), "--wt-base", "rel_wts", "create", "rel-slice"],
        cwd=caller_cwd,
    )
    assert proc_rel.returncode == 0, proc_rel.stderr
    rel_wt = Path(_parse_kv(proc_rel.stdout)["WORKTREE_PATH"])
    assert rel_wt.is_absolute() and rel_wt.is_dir()
    assert rel_wt.parent == (caller_cwd / "rel_wts").resolve()
    assert not (repo / "rel_wts").exists()

    # Pre-symlink lexical escape (<sym_to_repo_sub>/../escape_wt) refused BEFORE any mkdir
    sym_to_sub = tmp_path / "sym_to_sub"
    sym_to_sub.symlink_to(repo / "sub")
    proc_lex = _run_cli(
        ["--repo", str(repo), "--wt-base", str(sym_to_sub / ".." / "escape_wt"), "create", "esc"]
    )
    assert proc_lex.returncode == 1 and "ERROR:" in proc_lex.stderr
    assert not (repo / "escape_wt").exists()

    # Broad bases and world-writable explicit bases refused; group-writable (0o775) allowed
    for broad in (Path("/"), Path.home(), Path.home().parent, tmp_path / "isolated_sys_tmp"):
        assert mwt.main(["--repo", str(repo), "--wt-base", str(broad), "create", "b"]) == 1
        assert "ERROR:" in capsys.readouterr().err

    ww_base = tmp_path / "world_writable"
    ww_base.mkdir()
    os.chmod(ww_base, 0o777)
    try:
        assert mwt.main(["--repo", str(repo), "--wt-base", str(ww_base), "create", "ww"]) == 1
        assert "world-writable" in capsys.readouterr().err
    finally:
        os.chmod(ww_base, 0o700)

    gw_base = tmp_path / "group_writable"
    gw_base.mkdir()
    os.chmod(gw_base, 0o775)
    assert mwt.main(["--repo", str(repo), "--wt-base", str(gw_base), "create", "gw"]) == 0
    capsys.readouterr()

    # Rapid same-second in-process create calls must not collide
    assert mwt.main(["--repo", str(repo), "--wt-base", str(wt_base), "create", "dup"]) == 0
    out1 = _parse_kv(capsys.readouterr().out)
    assert mwt.main(["--repo", str(repo), "--wt-base", str(wt_base), "create", "dup"]) == 0
    out2 = _parse_kv(capsys.readouterr().out)
    assert out1["WORKTREE_PATH"] != out2["WORKTREE_PATH"]
    assert out1["WORKTREE_BRANCH"] != out2["WORKTREE_BRANCH"]

    # Invalid slice-id & base-ref rejections
    for bad_args in (
        ["--repo", str(repo), "--wt-base", str(wt_base), "create", ""],
        ["--repo", str(repo), "--wt-base", str(wt_base), "create", "..."],
        ["--repo", str(repo), "--wt-base", str(wt_base), "create", "--", "-evil"],
        ["--repo", str(repo), "--wt-base", str(wt_base), "create", "ok", "--", "-badref"],
        ["--repo", str(repo), "--wt-base", str(wt_base), "create", "ok", "nonexistent-ref"],
        ["--repo", str(repo), "--wt-base", str(repo / "inside"), "create", "ok"],
    ):
        assert _run_cli(bad_args).returncode != 0
    assert mwt.main(["--repo", str(repo), "--wt-base", str(wt_base), "create", "a\x00b"]) == 1
    assert mwt.main(["--repo", str(repo), "--wt-base", str(wt_base), "create", "ok", "a\x00b"]) == 1
    capsys.readouterr()

    # Default _DEFAULT_TMP_ROOT / agy-wt-<safe_repo>: loose pre-existing dir tightened to 0o700
    fake_tmp = tmp_path / "sys_tmp"
    default_base = fake_tmp / "agy-wt-my-repo"
    default_base.mkdir(parents=True)
    os.chmod(default_base, 0o755)
    monkeypatch.setattr(mwt, "_DEFAULT_TMP_ROOT", fake_tmp)
    assert mwt.main(["--repo", str(repo), "create", "def-slice"]) == 0
    def_kv = _parse_kv(capsys.readouterr().out)
    assert Path(def_kv["WORKTREE_PATH"]).parent == default_base.resolve()
    assert stat.S_IMODE(os.lstat(default_base).st_mode) == 0o700

    # Pre-existing symlink at default wt_base MUST be refused
    symlink_tmp = tmp_path / "sym_tmp"
    symlink_tmp.mkdir()
    decoy = tmp_path / "decoy_target"
    decoy.mkdir()
    (symlink_tmp / "agy-wt-my-repo").symlink_to(decoy)
    monkeypatch.setattr(mwt, "_DEFAULT_TMP_ROOT", symlink_tmp)
    assert mwt.main(["--repo", str(repo), "create", "sym-slice"]) == 1
    assert "ERROR:" in capsys.readouterr().err

    # Rollback on worktree add failure cleans up partially created branch & dir
    real_run_git = mwt._run_git

    def fail_wt_add(
        cwd: Path, args: Sequence[str], *, extra_env: Mapping[str, str] | None = None
    ) -> object:
        if list(args[:2]) == ["worktree", "add"]:
            br = args[args.index("-b") + 1]
            p = Path(args[args.index("--") + 1])
            p.mkdir(parents=True, exist_ok=True)
            real_run_git(cwd, ["branch", br, "HEAD"])
            return mwt.GitResult(128, b"", b"simulated worktree add failure")
        return real_run_git(cwd, args, extra_env=extra_env)

    with monkeypatch.context() as mp:
        mp.setattr(mwt, "_run_git", fail_wt_add)
        assert mwt.main(["--repo", str(repo), "--wt-base", str(wt_base), "create", "rb"]) == 1
    assert "simulated worktree add failure" in capsys.readouterr().err
    assert not list(wt_base.glob("rb-*"))
    assert "dev-swarm/rb-" not in _git(repo, "branch", "--list").stdout

    # status rejects missing dir, non-worktree dir, invalid --repo, and unrelated --repo
    other_repo = tmp_path / "other_repo"
    _seed_repo(other_repo, {"x.txt": "x\n"})
    assert mwt.main(["status", str(tmp_path / "no-such-dir")]) == 1
    assert mwt.main(["status", str(plain)]) == 1
    assert mwt.main(["status", str(wt_tag / "sub")]) == 1
    assert mwt.main(["--repo", str(plain), "status", str(wt_tag)]) == 1
    assert mwt.main(["--repo", str(other_repo), "status", str(wt_tag)]) == 1
    capsys.readouterr()

    # status escapes BiDi/control chars while preserving LF/tab, and handles narrow ASCII stdout
    bidi_name = "safe\u202ebidi.txt"
    (wt_tag / bidi_name).write_text("hi\n", encoding="utf-8")
    assert mwt.main(["status", str(wt_tag)]) == 0
    st_out = capsys.readouterr().out
    assert "\\u202e" in st_out and "\u202e" not in st_out and "\n" in st_out

    ascii_buf = io.BytesIO()
    ascii_out = io.TextIOWrapper(ascii_buf, encoding="ascii", errors="strict")
    with monkeypatch.context() as mp:
        mp.setattr(sys, "stdout", ascii_out)
        mwt._write_stdout_safe("ok-\u00e9\n", redirect_broken_pipe_fd=False)
    assert ascii_buf.getvalue() == b"ok-\\xe9\n"

    # BrokenPipeError safety in-process: returns 0 without mutating pipe FD
    r_fd, w_fd = os.pipe()
    os.close(r_fd)
    pipe_writer = os.fdopen(w_fd, "w", encoding="utf-8", closefd=False)
    try:
        with monkeypatch.context() as mp:
            mp.setattr(sys, "stdout", pipe_writer)
            assert mwt.main(["status", str(wt_tag)]) == 0
        assert stat.S_ISFIFO(os.fstat(w_fd).st_mode)
    finally:
        with contextlib.suppress(BrokenPipeError, OSError):
            pipe_writer.close()
        os.close(w_fd)


# ---------------------------------------------------------------------------
# 3. Single, Multi-Worktree Parallel, Hostile Config & Legacy apply Matrix
# ---------------------------------------------------------------------------


def test_apply_single_rich_changes_and_hostile_merge_config(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _seed_repo(
        repo,
        {
            "keep.txt": "v1\n",
            "remove_me.txt": "bye\n",
            "rename_me.txt": "move content\n",
            "script.sh": "#!/bin/sh\necho hi\n",
            "user_dirty.txt": "original\n",
        },
    )
    _git(repo, "config", "merge.ff", "false")
    _git(repo, "config", "merge.autostash", "true")
    _git(repo, "config", "merge.verifySignatures", "true")
    (repo / "user_dirty.txt").write_text("user uncommitted edit\n", encoding="utf-8")

    wt_base = tmp_path / "wts"
    wt_path, wt_branch, _ = _create_wt(repo, wt_base, "rich")
    (wt_path / "keep.txt").write_text("v2\n", encoding="utf-8")
    wip_sha = _commit_all(wt_path, "wip commit")

    (wt_path / "remove_me.txt").unlink()
    _git(wt_path, "mv", "rename_me.txt", "renamed.txt")
    os.chmod(wt_path / "script.sh", 0o755)
    (wt_path / "link_to_keep").symlink_to("keep.txt")
    (wt_path / "untracked.py").write_text("ok = True\n", encoding="utf-8")

    proc = _run_cli(["--repo", str(repo), "apply", str(wt_path)])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0] == f"APPLIED_FROM={wt_path} ({wt_branch})"
    assert "Squash commit" not in proc.stdout
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == base_sha
    assert _git(wt_path, "rev-parse", "HEAD").stdout.strip() == wip_sha
    assert not (repo / ".git" / "SQUASH_MSG").exists()
    assert _git(repo, "stash", "list").stdout.strip() == ""

    assert (repo / "keep.txt").read_text(encoding="utf-8") == "v2\n"
    assert not (repo / "remove_me.txt").exists()
    assert (repo / "renamed.txt").read_text(encoding="utf-8") == "move content\n"
    assert os.access(repo / "script.sh", os.X_OK)
    assert (repo / "link_to_keep").is_symlink() and os.readlink(repo / "link_to_keep") == "keep.txt"
    assert (repo / "untracked.py").read_text(encoding="utf-8") == "ok = True\n"
    assert (repo / "user_dirty.txt").read_text(encoding="utf-8") == "user uncommitted edit\n"

    wt_noop, wt_noop_br, _ = _create_wt(repo, wt_base, "noop")
    proc_noop = _run_cli(["--repo", str(repo), "apply", str(wt_noop)])
    assert proc_noop.returncode == 0, proc_noop.stderr
    assert proc_noop.stdout.splitlines()[0] == f"APPLIED_FROM={wt_noop} ({wt_noop_br})"
    assert (repo / "keep.txt").read_text(encoding="utf-8") == "v2\n"


def test_apply_parallel_multi_worktree_stationary_and_diverged_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base_sha = _seed_repo(
        repo,
        {
            "dir_swap/only_file.txt": "old\n",
            "mod.txt": "v1\n",
            "to_ren.txt": "move me\n",
            "run.sh": "#!/bin/sh\necho 1\n",
            "user_dirty.txt": "base\n",
        },
        "base",
    )

    wt_base = tmp_path / "wts"
    wt1, _, _ = _create_wt(repo, wt_base, "s1")
    wt2, _, _ = _create_wt(repo, wt_base, "s2")
    wt3, _, _ = _create_wt(repo, wt_base, "s3")
    wt4, _, _ = _create_wt(repo, wt_base, "s4-noop")

    (wt1 / "mod.txt").write_text("v2\n", encoding="utf-8")
    (wt1 / "dir_swap" / "only_file.txt").unlink()
    (wt1 / "pkg").mkdir()
    (wt1 / "pkg" / "a.py").write_text("A = 1\n", encoding="utf-8")

    _git(wt2, "mv", "to_ren.txt", "renamed.txt")
    os.chmod(wt2 / "run.sh", 0o755)
    (wt2 / "pkg").mkdir()
    (wt2 / "pkg" / "b.py").write_text("B = 2\n", encoding="utf-8")

    (wt3 / "sym.txt").symlink_to("mod.txt")
    (wt3 / "dir_swap" / "new_file.txt").write_text("fresh\n", encoding="utf-8")
    (repo / "user_dirty.txt").write_text("user edit\n", encoding="utf-8")

    for wt in (wt1, wt2, wt3, wt4):
        proc = _run_cli(["--repo", str(repo), "apply", str(wt)])
        assert proc.returncode == 0, f"wt={wt}: stderr={proc.stderr!r}"

    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == base_sha
    assert (repo / "mod.txt").read_text(encoding="utf-8") == "v2\n"
    assert not (repo / "dir_swap" / "only_file.txt").exists()
    assert (repo / "dir_swap" / "new_file.txt").read_text(encoding="utf-8") == "fresh\n"
    assert (repo / "pkg" / "a.py").read_text(encoding="utf-8") == "A = 1\n"
    assert (repo / "pkg" / "b.py").read_text(encoding="utf-8") == "B = 2\n"
    assert (repo / "renamed.txt").read_text(encoding="utf-8") == "move me\n"
    assert os.access(repo / "run.sh", os.X_OK)
    assert (repo / "sym.txt").is_symlink() and os.readlink(repo / "sym.txt") == "mod.txt"
    assert (repo / "user_dirty.txt").read_text(encoding="utf-8") == "user edit\n"

    # Diverged-HEAD multi-worktree apply
    div_repo = tmp_path / "div_repo"
    c0 = _seed_repo(div_repo, {"base.txt": "root\n"}, "c0")
    div_wts = tmp_path / "div_wts"
    d1, _, _ = _create_wt(div_repo, div_wts, "d1", c0)
    d2, _, _ = _create_wt(div_repo, div_wts, "d2", c0)

    (div_repo / "main_only.txt").write_text("main advanced\n", encoding="utf-8")
    head_adv = _commit_all(div_repo, "advance main")

    (d1 / "a.py").write_text("a = 1\n", encoding="utf-8")
    (d2 / "b.py").write_text("b = 2\n", encoding="utf-8")
    assert _run_cli(["--repo", str(div_repo), "apply", str(d1)]).returncode == 0
    assert _run_cli(["--repo", str(div_repo), "apply", str(d2)]).returncode == 0
    assert _git(div_repo, "rev-parse", "HEAD").stdout.strip() == head_adv
    assert (div_repo / "a.py").read_text(encoding="utf-8") == "a = 1\n"
    assert (div_repo / "b.py").read_text(encoding="utf-8") == "b = 2\n"


def test_apply_legacy_git_234_porcelain_and_squash_msg_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Version (2, 34) exercises BOTH the < 2.36 line-oriented --porcelain parser and < 2.38 apply
    monkeypatch.setattr(mwt, "_git_version", lambda _cwd: (2, 34))

    repo = tmp_path / "repo"
    _seed_repo(repo, {"shared.txt": "line1\n", ".gitignore": "secret.env\n"}, "init")
    _git(repo, "config", "merge.ff", "false")
    _git(repo, "config", "merge.autostash", "true")
    _git(repo, "config", "merge.verifySignatures", "true")

    wt_base = tmp_path / "wts"
    wt1, _, _ = _create_wt(repo, wt_base, "leg1")
    wt2, _, _ = _create_wt(repo, wt_base, "leg2")
    (wt1 / "a.txt").write_text("a\n", encoding="utf-8")
    (wt2 / "b.txt").write_text("b\n", encoding="utf-8")

    assert mwt.main(["--repo", str(repo), "apply", str(wt1)]) == 0
    assert "Squash commit" not in capsys.readouterr().out
    assert not (repo / ".git" / "SQUASH_MSG").exists()
    assert (repo / "a.txt").read_text(encoding="utf-8") == "a\n"

    # Legacy stacked slice fails cleanly
    assert mwt.main(["--repo", str(repo), "apply", str(wt2)]) == 1
    assert "stacking multiple worktree slices requires git >= 2.38" in capsys.readouterr().err

    # Legacy gitignored file overwrite guard
    ig_repo = tmp_path / "leg_ign"
    _seed_repo(ig_repo, {".gitignore": "secret.env\n"})
    ig_wt, _, _ = _create_wt(ig_repo, wt_base, "leg-ig")
    (ig_wt / "secret.env").write_text("from slice\n", encoding="utf-8")
    _git(ig_wt, "add", "-f", "secret.env")
    _git(ig_wt, "commit", "-q", "-m", "force add secret")
    (ig_repo / "secret.env").write_text("LIVE_SECRET=1\n", encoding="utf-8")
    assert mwt.main(["--repo", str(ig_repo), "apply", str(ig_wt)]) == 1
    assert "refusing to overwrite" in capsys.readouterr().err
    assert (ig_repo / "secret.env").read_text(encoding="utf-8") == "LIVE_SECRET=1\n"

    # Pre-existing SQUASH_MSG preserved inside a linked-worktree repo target
    linked_target = tmp_path / "linked_target"
    _git(repo, "worktree", "add", "-q", "-b", "linked-base", str(linked_target), "HEAD")
    linked_sq = Path(_git(linked_target, "rev-parse", "--git-path", "SQUASH_MSG").stdout.strip())
    if not linked_sq.is_absolute():
        linked_sq = linked_target / linked_sq
    linked_sq.write_text("user pre-existing squash message\n", encoding="utf-8")

    wt_in_linked, _, _ = _create_wt(linked_target, wt_base, "leg-linked")
    (wt_in_linked / "c.txt").write_text("c\n", encoding="utf-8")
    assert mwt.main(["--repo", str(linked_target), "apply", str(wt_in_linked)]) == 0
    capsys.readouterr()
    assert linked_sq.read_text(encoding="utf-8") == "user pre-existing squash message\n"

    # Legacy diverged conflict runs reset --merge BEFORE restoring pre-existing SQUASH_MSG
    c_repo = tmp_path / "leg_conflict"
    c0 = _seed_repo(c_repo, {"file.txt": "v0\n"}, "c0")
    c_wt, _, _ = _create_wt(c_repo, wt_base, "leg-conf", c0)
    (c_repo / "file.txt").write_text("main change\n", encoding="utf-8")
    _commit_all(c_repo, "main change")
    (c_repo / ".git" / "SQUASH_MSG").write_text("keep after reset\n", encoding="utf-8")
    (c_wt / "file.txt").write_text("wt conflicting change\n", encoding="utf-8")

    assert mwt.main(["--repo", str(c_repo), "apply", str(c_wt)]) == 1
    assert "ERROR:" in capsys.readouterr().err
    assert _git(c_repo, "ls-files", "-u").stdout.strip() == ""
    assert (c_repo / "file.txt").read_text(encoding="utf-8") == "main change\n"
    assert (c_repo / ".git" / "SQUASH_MSG").read_text(encoding="utf-8") == "keep after reset\n"

    # Locked worktree detected cleanly via < 2.36 line-oriented --porcelain parser
    _git(c_repo, "worktree", "lock", str(c_wt))
    assert mwt.main(["--repo", str(c_repo), "--wt-base", str(wt_base), "cleanup", str(c_wt)]) == 1
    assert "locked" in capsys.readouterr().err
    assert c_wt.is_dir()
    _git(c_repo, "worktree", "unlock", str(c_wt))


def test_apply_conflicts_gitignored_overwrite_gitlinks_and_active_op_guards(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    base_sha = _seed_repo(
        repo,
        {"shared.py": "VAL = 0\n", "f_or_d": "file\n", ".gitignore": "secret.env\n"},
    )
    wt_base = tmp_path / "wts"
    wt1, _, _ = _create_wt(repo, wt_base, "c1")
    wt2, _, _ = _create_wt(repo, wt_base, "c2")
    wt_fd, _, _ = _create_wt(repo, wt_base, "c-fd")

    (wt1 / "a.py").write_text("A = 1\n", encoding="utf-8")
    (wt1 / "shared.py").write_text("VAL = 1\n", encoding="utf-8")
    (wt1 / "f_or_d").write_text("still a file\n", encoding="utf-8")
    assert _run_cli(["--repo", str(repo), "apply", str(wt1)]).returncode == 0

    # Overlapping conflict fails in-memory; both slice 1 AND wt2 remain untouched
    (wt2 / "shared.py").write_text("VAL = 2\n", encoding="utf-8")
    wt2_head_before = _git(wt2, "rev-parse", "HEAD").stdout.strip()
    proc_conf = _run_cli(["--repo", str(repo), "apply", str(wt2)])
    assert proc_conf.returncode == 1 and "ERROR:" in proc_conf.stderr
    assert _git(repo, "ls-files", "-u").stdout.strip() == ""
    assert "<<<<<<<" not in (repo / "shared.py").read_text(encoding="utf-8")
    assert (repo / "shared.py").read_text(encoding="utf-8") == "VAL = 1\n"
    assert (repo / "a.py").read_text(encoding="utf-8") == "A = 1\n"
    assert _git(wt2, "rev-parse", "HEAD").stdout.strip() == wt2_head_before

    # File/directory collision fails in-memory preserving slice 1
    (wt_fd / "f_or_d").unlink()
    (wt_fd / "f_or_d").mkdir()
    (wt_fd / "f_or_d" / "child.txt").write_text("dir\n", encoding="utf-8")
    assert _run_cli(["--repo", str(repo), "apply", str(wt_fd)]).returncode == 1
    assert (repo / "f_or_d").is_file()

    # Gitignored file overwrite guard on >= 2.38
    wt_ig, _, _ = _create_wt(repo, wt_base, "c-ig")
    (wt_ig / "secret.env").write_text("OVERWRITE\n", encoding="utf-8")
    _git(wt_ig, "add", "-f", "secret.env")
    _git(wt_ig, "commit", "-q", "-m", "force add ignored")
    (repo / "secret.env").write_text("KEEP_LOCAL_SECRET\n", encoding="utf-8")
    proc_ig = _run_cli(["--repo", str(repo), "apply", str(wt_ig)])
    assert proc_ig.returncode == 1 and "refusing to overwrite" in proc_ig.stderr
    assert (repo / "secret.env").read_text(encoding="utf-8") == "KEEP_LOCAL_SECRET\n"
    assert (repo / "a.py").read_text(encoding="utf-8") == "A = 1\n"

    # Untracked file collision in repo_root aborts atomically
    wt_untracked, _, _ = _create_wt(repo, wt_base, "c-untracked")
    (wt_untracked / "collide.txt").write_text("from slice\n", encoding="utf-8")
    (repo / "collide.txt").write_text("user untracked\n", encoding="utf-8")
    proc_coll = _run_cli(["--repo", str(repo), "apply", str(wt_untracked)])
    assert proc_coll.returncode == 1 and "ERROR:" in proc_coll.stderr
    assert (repo / "collide.txt").read_text(encoding="utf-8") == "user untracked\n"
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == base_sha

    # Newly added 160000 gitlink/submodule in worktree MUST be refused
    wt_gl, _, _ = _create_wt(repo, wt_base, "c-gitlink")
    nested = wt_gl / "nested_sub"
    _seed_repo(nested, {"n.txt": "nested\n"})
    proc_gl = _run_cli(["--repo", str(repo), "apply", str(wt_gl)])
    assert proc_gl.returncode == 1 and "gitlink" in proc_gl.stderr.lower()

    # Diverged HEAD conflict preserves slice 1 atomically
    div = tmp_path / "div_conf"
    c0 = _seed_repo(div, {"shared.py": "X = 0\n"}, "c0")
    dw1, _, _ = _create_wt(div, wt_base, "dw1", c0)
    dw2, _, _ = _create_wt(div, wt_base, "dw2", c0)
    (div / "main.py").write_text("M = 1\n", encoding="utf-8")
    _commit_all(div, "main advance")
    (dw1 / "a.py").write_text("A = 1\n", encoding="utf-8")
    (dw1 / "shared.py").write_text("X = 1\n", encoding="utf-8")
    (dw2 / "shared.py").write_text("X = 2\n", encoding="utf-8")
    assert _run_cli(["--repo", str(div), "apply", str(dw1)]).returncode == 0
    assert _run_cli(["--repo", str(div), "apply", str(dw2)]).returncode == 1
    assert (div / "shared.py").read_text(encoding="utf-8") == "X = 1\n"

    # Unmerged conflict AND in-progress MERGE_HEAD inside worktree OR repo_root -> refuse
    wt_u_repo = tmp_path / "wt_unmerged_repo"
    _seed_repo(wt_u_repo, {"x.txt": "base\n"})
    _git(wt_u_repo, "checkout", "-q", "-b", "other")
    (wt_u_repo / "x.txt").write_text("other\n", encoding="utf-8")
    _commit_all(wt_u_repo, "other")
    _git(wt_u_repo, "checkout", "-q", "main")
    wt_u, _, _ = _create_wt(wt_u_repo, wt_base, "wt-u")
    (wt_u / "x.txt").write_text("mine\n", encoding="utf-8")
    _commit_all(wt_u, "mine")
    assert _git(wt_u, "merge", "other", check=False).returncode != 0
    proc_wtu = _run_cli(["--repo", str(wt_u_repo), "apply", str(wt_u)])
    assert proc_wtu.returncode == 1 and "unmerged" in proc_wtu.stderr.lower()

    # Clean MERGE_HEAD inside wt_res (e.g. git merge --no-commit) refused before snapshot
    wt_mh, _, _ = _create_wt(wt_u_repo, wt_base, "wt-mh")
    wt_mh_git = Path(_git(wt_mh, "rev-parse", "--git-path", "MERGE_HEAD").stdout.strip())
    if not wt_mh_git.is_absolute():
        wt_mh_git = wt_mh / wt_mh_git
    wt_mh_git.write_text(_git(wt_u_repo, "rev-parse", "other").stdout, encoding="utf-8")
    proc_wt_mh = _run_cli(["--repo", str(wt_u_repo), "apply", str(wt_mh)])
    assert proc_wt_mh.returncode == 1 and "MERGE_HEAD" in proc_wt_mh.stderr

    # In-progress MERGE_HEAD in repo_root refused fast
    (wt_u_repo / ".git" / "MERGE_HEAD").write_text(
        _git(wt_u_repo, "rev-parse", "HEAD").stdout, encoding="utf-8"
    )
    wt_ok, _, _ = _create_wt(wt_u_repo, wt_base, "wt-ok")
    (wt_ok / "ok.txt").write_text("ok\n", encoding="utf-8")
    proc_mh = _run_cli(["--repo", str(wt_u_repo), "apply", str(wt_ok)])
    assert proc_mh.returncode == 1 and "MERGE_HEAD" in proc_mh.stderr


def test_apply_detached_blank_identity_and_filters_vs_hooks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _seed_repo(repo, {"base.txt": "base\n", ".gitattributes": "*.filt filter=upper\n"})

    local_excludes = tmp_path / "repo_local_excludes"
    local_excludes.write_text("*.env.local\n", encoding="utf-8")
    _git(repo, "config", "core.excludesFile", str(local_excludes))
    _git(repo, "config", "filter.upper.clean", "tr 'a-z' 'A-Z'")
    _git(repo, "config", "filter.upper.smudge", "tr 'A-Z' 'a-z'")

    marker = tmp_path / "evil_ran"
    evil_body = f"#!/bin/sh\ntouch '{marker}'\nexit 1\n"
    evil_sh = _write_executable(tmp_path / "evil.sh", evil_body)
    hooks_dir = tmp_path / "hooks"
    _write_executable(hooks_dir / "pre-commit", evil_body)
    _write_executable(repo / ".git" / "hooks" / "pre-commit", evil_body)

    _git(repo, "config", "core.hooksPath", str(hooks_dir))
    _git(repo, "config", "commit.gpgsign", "true")
    _git(repo, "config", "gpg.program", str(evil_sh))
    _git(repo, "config", "diff.external", str(evil_sh))
    _git(repo, "config", "user.useConfigOnly", "true")

    wt_base = tmp_path / "wts"
    wt1, _, _ = _create_wt(repo, wt_base, "det")
    wt2, _, _ = _create_wt(repo, wt_base, "stack")
    pre_det_sha = _git(wt1, "rev-parse", "HEAD").stdout.strip()
    _git(wt1, "checkout", "-q", "--detach", pre_det_sha)

    (wt1 / "sample.filt").write_text("hello world\n", encoding="utf-8")
    (wt1 / "secret.env.local").write_text("SECRET=1\n", encoding="utf-8")
    (wt2 / "second.txt").write_text("two\n", encoding="utf-8")

    # Whitespace-only identity env vars stripped so fallback identity succeeds
    blank_id = {"GIT_AUTHOR_NAME": "   ", "GIT_AUTHOR_EMAIL": "\t", "EMAIL": ""}
    proc_st = _run_cli(["status", str(wt1)], include_identity=False, env_extra=blank_id)
    assert proc_st.returncode == 0

    proc1 = _run_cli(
        ["--repo", str(repo), "apply", str(wt1)], include_identity=False, env_extra=blank_id
    )
    assert proc1.returncode == 0, proc1.stderr
    assert proc1.stdout.splitlines()[0] == f"APPLIED_FROM={wt1} (detached@{pre_det_sha[:12]})"
    assert _git(wt1, "rev-parse", "HEAD").stdout.strip() == pre_det_sha

    proc2 = _run_cli(
        ["--repo", str(repo), "apply", str(wt2)], include_identity=False, env_extra=blank_id
    )
    assert proc2.returncode == 0, proc2.stderr

    assert _git(repo, "show", ":sample.filt").stdout == "HELLO WORLD\n"
    assert (repo / "sample.filt").read_text(encoding="utf-8") == "hello world\n"
    assert not (repo / "secret.env.local").exists()
    assert "secret.env.local" not in _git(repo, "ls-files").stdout
    assert (repo / "second.txt").read_text(encoding="utf-8") == "two\n"
    assert not marker.exists()


# ---------------------------------------------------------------------------
# 4. cleanup confinement, APFS casing, locks, wiped-parent & cross-repo tests
# ---------------------------------------------------------------------------


def test_cleanup_normal_idempotent_readonly_symlink_parent_detached_and_wiped_parent(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _seed_repo(repo, {"f.txt": "x\n"})

    real_base = tmp_path / "real_wts"
    real_base.mkdir()
    sym_base = tmp_path / "sym_wts"
    sym_base.symlink_to(real_base)

    # Symlink leaf --wt-base refused on create; symlinked parent dir accepted on cleanup
    assert _run_cli(
        ["--repo", str(repo), "--wt-base", str(sym_base), "create", "bad-sym-base"]
    ).returncode == 1

    wt_path, wt_branch, _ = _create_wt(repo, real_base, "clean-ok")
    wt_via_sym_parent = sym_base / wt_path.name
    ro_dir = wt_path / "ro_sub"
    ro_dir.mkdir()
    (ro_dir / "locked.txt").write_text("locked\n", encoding="utf-8")
    os.chmod(ro_dir, 0o500)

    proc1 = _run_cli(
        ["--repo", str(repo), "--wt-base", str(real_base), "cleanup",
         str(wt_via_sym_parent), wt_branch]
    )
    assert proc1.returncode == 0, proc1.stderr
    assert proc1.stdout == f"CLEANED={wt_via_sym_parent}\n"
    assert not wt_path.exists()
    ref_chk = _git(repo, "show-ref", "--verify", f"refs/heads/{wt_branch}", check=False)
    assert ref_chk.returncode != 0

    # Idempotent second cleanup
    proc2 = _run_cli(
        ["--repo", str(repo), "--wt-base", str(real_base), "cleanup",
         str(wt_via_sym_parent), wt_branch]
    )
    assert proc2.returncode == 0 and proc2.stdout == f"CLEANED={wt_via_sym_parent}\n"

    # Detached dev-swarm worktree inside wt_base remains cleanable
    wt_det, wt_det_br, _ = _create_wt(repo, real_base, "det-clean")
    _git(wt_det, "checkout", "-q", "--detach", "HEAD")
    proc_det = _run_cli(
        ["--repo", str(repo), "--wt-base", str(real_base), "cleanup", str(wt_det), wt_det_br]
    )
    assert proc_det.returncode == 0, proc_det.stderr
    assert not wt_det.exists()

    # Wiped-parent prunable recovery: deleting wt_base still lets cleanup prune & delete branch
    wiped_base = tmp_path / "wiped_wts"
    wt_wiped, br_wiped, _ = _create_wt(repo, wiped_base, "wiped")
    shutil.rmtree(wiped_base)
    proc_wiped = _run_cli(
        ["--repo", str(repo), "--wt-base", str(wiped_base), "cleanup", str(wt_wiped), br_wiped]
    )
    assert proc_wiped.returncode == 0, proc_wiped.stderr
    assert _git(repo, "show-ref", "--verify", f"refs/heads/{br_wiped}", check=False).returncode != 0
    assert br_wiped not in _git(repo, "worktree", "list", "--porcelain").stdout

    # Unregistered orphan dir with no .git inside wt_base is removable
    orphan = real_base / "leftover-dir"
    orphan.mkdir()
    (orphan / "junk.txt").write_text("junk\n", encoding="utf-8")
    assert _run_cli(
        ["--repo", str(repo), "--wt-base", str(real_base), "cleanup", str(orphan)]
    ).returncode == 0
    assert not orphan.exists()

    # Orphan dir containing top-level .git dir, nested .git, or forged gitdir MUST be refused
    orphan_repo = tmp_path / "real_wts" / "unrelated-git-repo"
    _seed_repo(orphan_repo, {"keep.txt": "keep\n"})
    assert _run_cli(
        ["--repo", str(repo), "--wt-base", str(real_base), "cleanup", str(orphan_repo)]
    ).returncode == 1
    assert (orphan_repo / "keep.txt").is_file()

    nested_git_orphan = real_base / "nested-git-orphan"
    _seed_repo(nested_git_orphan / "inner", {"precious.txt": "keep\n"})
    proc_nest = _run_cli(
        ["--repo", str(repo), "--wt-base", str(real_base), "cleanup", str(nested_git_orphan)]
    )
    assert proc_nest.returncode == 1 and "nested .git" in proc_nest.stderr
    assert (nested_git_orphan / "inner" / "precious.txt").is_file()

    for bad_gitdir in (
        f"gitdir: {repo / '.git' / 'worktrees'}\n",
        f"gitdir: {repo / '.git' / 'worktrees' / 'a' / 'b'}\n",
    ):
        forged = real_base / f"forged-{abs(hash(bad_gitdir))}"
        forged.mkdir()
        (forged / ".git").write_text(bad_gitdir, encoding="utf-8")
        assert _run_cli(
            ["--repo", str(repo), "--wt-base", str(real_base), "cleanup", str(forged)]
        ).returncode == 1
        assert forged.is_dir()


def test_cleanup_security_refusals_locks_sibling_branch_and_apfs_casing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "CaseRepo"
    _seed_repo(repo, {"src/f.txt": "safe\n"})
    _git(repo, "branch", "feature/foo")

    wt_base = tmp_path / "wts"
    wt_path, wt_branch, _ = _create_wt(repo, wt_base, "legit")

    outside = tmp_path / "outside_precious"
    outside.mkdir()
    (outside / "data.txt").write_text("precious\n", encoding="utf-8")
    sym_escape = wt_base / "sym_to_outside"
    sym_escape.symlink_to(outside)

    for bad_target in (
        str(repo),
        str(repo / "src"),
        str(outside),
        str(wt_base),
        str(wt_base / ".." / "outside_precious"),
        str(sym_escape),
        f"{sym_escape}/",
        str(tmp_path / "missing_outside"),
    ):
        proc = _run_cli(["--repo", str(repo), "--wt-base", str(wt_base), "cleanup", bad_target])
        assert proc.returncode == 1 and "ERROR:" in proc.stderr
    assert (outside / "data.txt").read_text(encoding="utf-8") == "precious\n"
    assert sym_escape.is_symlink() and (repo / "src" / "f.txt").is_file()

    # APFS case-insensitive bypass checks (only meaningful when filesystem folds CaseRepo/CASEREPO)
    upper_repo = tmp_path / "CASEREPO"
    if upper_repo.exists():
        assert mwt.main(
            ["--repo", str(repo), "--wt-base", str(upper_repo),
             "cleanup", str(upper_repo / "src")]
        ) == 1
        capsys.readouterr()
        assert (repo / "src" / "f.txt").read_text(encoding="utf-8") == "safe\n"

        assert mwt.main(
            ["--repo", str(repo), "--wt-base", str(upper_repo / "nested_wts"), "create", "c"]
        ) == 1
        capsys.readouterr()
        assert not (repo / "nested_wts").exists()

        # Differently-cased valid worktree still matches registered worktree in apply & cleanup
        mixed_wt = wt_path.parent / wt_path.name.upper()
        if mixed_wt.exists():
            (wt_path / "case_ok.txt").write_text("ok\n", encoding="utf-8")
            assert mwt.main(["--repo", str(repo), "apply", str(mixed_wt)]) == 0
            capsys.readouterr()

    # User linked worktree outside wt_base AND inside wt_base on non-dev-swarm branch -> refused
    user_wt_out = tmp_path / "user_worktree"
    _git(repo, "worktree", "add", "-q", "-b", "feature/user-out", str(user_wt_out), "HEAD")
    (user_wt_out / "notes.txt").write_text("user wip\n", encoding="utf-8")
    assert mwt.main(["--repo", str(repo), "cleanup", str(user_wt_out)]) == 1
    assert "non-dev-swarm" in capsys.readouterr().err
    assert (user_wt_out / "notes.txt").read_text(encoding="utf-8") == "user wip\n"

    user_wt_in = wt_base / "user_inside_wt_base"
    _git(repo, "worktree", "add", "-q", "-b", "feature/user-in", str(user_wt_in), "HEAD")
    assert mwt.main(
        ["--repo", str(repo), "--wt-base", str(wt_base), "cleanup", str(user_wt_in)]
    ) == 1
    assert "non-dev-swarm" in capsys.readouterr().err
    assert user_wt_in.is_dir()

    # Missing-path idempotent arm also refuses prunable non-dev-swarm user worktree
    user_missing_wt = tmp_path / "user_missing_wt"
    _git(repo, "worktree", "add", "-q", "-b", "feature/user-missing", str(user_missing_wt), "HEAD")
    shutil.rmtree(user_missing_wt)
    assert mwt.main(["--repo", str(repo), "cleanup", str(user_missing_wt)]) == 1
    assert "non-dev-swarm" in capsys.readouterr().err
    assert "feature/user-missing" in _git(repo, "worktree", "list", "--porcelain").stdout

    # Locked dev-swarm worktree refused upfront
    wt_lock, br_lock, _ = _create_wt(repo, wt_base, "locked-wt")
    _git(repo, "worktree", "lock", str(wt_lock))
    assert mwt.main(
        ["--repo", str(repo), "--wt-base", str(wt_base), "cleanup", str(wt_lock), br_lock]
    ) == 1
    assert "locked" in capsys.readouterr().err
    assert wt_lock.is_dir()
    _git(repo, "worktree", "unlock", str(wt_lock))

    # Sibling worktree branch precheck refuses BEFORE deleting wt_path
    wt_sib, br_sib, _ = _create_wt(repo, wt_base, "sib-wt")
    assert mwt.main(
        ["--repo", str(repo), "--wt-base", str(wt_base), "cleanup", str(wt_path), br_sib]
    ) == 1
    assert "another worktree" in capsys.readouterr().err
    assert wt_path.is_dir() and wt_sib.is_dir()

    # Non-dev-swarm branch deletions -> refused before touching worktree
    for bad_branch in ("main", "feature/foo", "dev-swarm/", "-D"):
        proc_br = _run_cli(
            ["--repo", str(repo), "--wt-base", str(wt_base), "cleanup",
             str(wt_path), "--", bad_branch]
        )
        assert proc_br.returncode != 0
    assert wt_path.is_dir()

    # Currently checked-out dev-swarm/* branch -> refused
    _git(repo, "checkout", "-q", "-b", "dev-swarm/active-head")
    assert mwt.main(
        ["--repo", str(repo), "--wt-base", str(wt_base), "cleanup",
         str(wt_path), "dev-swarm/active-head"]
    ) == 1
    assert "currently checked-out" in capsys.readouterr().err
    assert wt_path.is_dir()
    _git(repo, "checkout", "-q", "main")

    # Same-basename sibling repos sharing default _DEFAULT_TMP_ROOT/agy-wt-app
    shared_tmp = tmp_path / "shared_tmp"
    shared_tmp.mkdir()
    monkeypatch.setattr(mwt, "_DEFAULT_TMP_ROOT", shared_tmp)

    repo_a = tmp_path / "p1" / "app"
    _seed_repo(repo_a, {"a.txt": "a\n"})
    repo_b = tmp_path / "p2" / "app"
    _seed_repo(repo_b, {"b.txt": "b\n"})

    assert mwt.main(["--repo", str(repo_b), "create", "b-slice"]) == 0
    b_wt = Path(_parse_kv(capsys.readouterr().out)["WORKTREE_PATH"])
    (b_wt / "precious_b.txt").write_text("belongs to repo b\n", encoding="utf-8")

    assert mwt.main(["--repo", str(repo_a), "cleanup", str(b_wt)]) == 1
    assert "another repository" in capsys.readouterr().err
    assert (b_wt / "precious_b.txt").read_text(encoding="utf-8") == "belongs to repo b\n"

    assert mwt.main(
        ["--repo", str(repo), "--wt-base", str(wt_base), "cleanup", str(wt_path), wt_branch]
    ) == 0


def test_snapshot_preserves_live_index_and_split_index_and_annotated_tag(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    c1 = _seed_repo(
        repo,
        {
            "local.secret": "LIVE_SECRET=999\n",
            "old.txt": "keep content\n",
            "foo.txt": "case file\n",
            "exec.sh": "#!/bin/sh\necho hi\n",
            ".gitignore": "*.env\n",
        },
    )
    _git(repo, "tag", "-a", "v2.0-ann", "-m", "annotated release tag")
    tag_obj_sha = _git(repo, "rev-parse", "v2.0-ann").stdout.strip()
    assert tag_obj_sha != c1

    wt_base = tmp_path / "wts"
    # Annotated tag base_ref outputs peeled commit SHA, not tag object SHA
    wt, _, peeled_sha = _create_wt(repo, wt_base, "live-idx", "v2.0-ann")
    assert peeled_sha == c1

    # 1. Uncommitted `git rm --cached local.secret` + adding to .gitignore must NOT resurrect secret
    (wt / ".gitignore").write_text("*.env\nlocal.secret\n", encoding="utf-8")
    _git(wt, "rm", "--cached", "local.secret")
    assert (wt / "local.secret").read_text(encoding="utf-8") == "LIVE_SECRET=999\n"

    # 2. Uncommitted `git add -f forced.env` and `git mv -f old.txt secret.env` preserved
    (wt / "forced.env").write_text("FORCED=1\n", encoding="utf-8")
    _git(wt, "add", "-f", "forced.env")
    _git(wt, "mv", "-f", "old.txt", "secret.env")

    # 3. Uncommitted `git update-index --chmod=+x` preserved under core.filemode=false
    _git(wt, "config", "core.filemode", "false")
    _git(wt, "update-index", "--chmod=+x", "exec.sh")

    # 4. Uncommitted APFS case-only `git mv foo.txt Foo.txt` preserved
    if (wt / "FOO.TXT").exists():
        _git(wt, "mv", "foo.txt", "Foo.txt")

    proc = _run_cli(["--repo", str(repo), "apply", str(wt)])
    assert proc.returncode == 0, proc.stderr

    staged_files = set(_git(repo, "ls-files").stdout.splitlines())
    assert "local.secret" not in staged_files
    assert "forced.env" in staged_files
    assert "secret.env" in staged_files and "old.txt" not in staged_files
    assert (repo / "secret.env").read_text(encoding="utf-8") == "keep content\n"
    mode_line = _git(repo, "ls-files", "--stage", "exec.sh").stdout.strip()
    assert mode_line.startswith("100755 ")
    if (wt / "FOO.TXT").exists():
        assert "Foo.txt" in staged_files and "foo.txt" not in staged_files

    # 5. core.splitIndex=true + racy same-second same-size edit across >1s delay leaks zero extras
    split_repo = tmp_path / "split_repo"
    _seed_repo(split_repo, {"a.txt": "v1\n", "keep.txt": "stay\n"})
    _git(split_repo, "config", "core.splitIndex", "true")
    wt_split, _, _ = _create_wt(split_repo, wt_base, "split-wt")
    _git(wt_split, "update-index", "--split-index")
    wt_git_dir = Path(_git(wt_split, "rev-parse", "--absolute-git-dir").stdout.strip())
    before_shared = sorted(p.name for p in wt_git_dir.glob("sharedindex.*"))
    (wt_split / "a.txt").write_text("v2\n", encoding="utf-8")
    (wt_split / "new_unstaged.txt").write_text("hello\n", encoding="utf-8")
    time.sleep(1.1)
    proc_sp = _run_cli(["--repo", str(split_repo), "apply", str(wt_split)])
    assert proc_sp.returncode == 0, proc_sp.stderr
    after_shared = sorted(p.name for p in wt_git_dir.glob("sharedindex.*"))
    assert after_shared == before_shared
    assert (split_repo / "a.txt").read_text(encoding="utf-8") == "v2\n"
    assert (split_repo / "new_unstaged.txt").read_text(encoding="utf-8") == "hello\n"
    assert "keep.txt" in _git(split_repo, "ls-files").stdout.splitlines()
    assert (split_repo / "keep.txt").read_text(encoding="utf-8") == "stay\n"


@pytest.mark.parametrize("fake_git_ver", [None, (2, 34)])
def test_apply_file_dir_symlink_and_case_refactors_vs_gitignored_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_git_ver: tuple[int, int] | None,
) -> None:
    if fake_git_ver is not None:
        monkeypatch.setattr(mwt, "_git_version", lambda _cwd: fake_git_ver)

    ext_dir = tmp_path / "ext_symlink_target"
    ext_dir.mkdir()
    (ext_dir / "child.txt").write_text("EXTERNAL_TARGET\n", encoding="utf-8")

    # 1. File->Dir, Symlink->Dir (to external dir), Dir->File, and committed APFS case rename
    repo = tmp_path / "refactors"
    _init_repo(repo)
    (repo / "mod").write_text("file\n", encoding="utf-8")
    (repo / "sym").symlink_to(ext_dir)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "old.py").write_text("old = 1\n", encoding="utf-8")
    (repo / "foo.txt").write_text("case\n", encoding="utf-8")
    (repo / ".gitignore").write_text("*.env\n", encoding="utf-8")
    _commit_all(repo, "init refactors")

    wt_base = tmp_path / "wts"
    wt, _, _ = _create_wt(repo, wt_base, "ref")
    (wt / "mod").unlink()
    (wt / "mod").mkdir()
    (wt / "mod" / "init.py").write_text("mod_dir = True\n", encoding="utf-8")

    (wt / "sym").unlink()
    (wt / "sym").mkdir()
    (wt / "sym" / "child.txt").write_text("new real child\n", encoding="utf-8")

    shutil.rmtree(wt / "pkg")
    (wt / "pkg").write_text("now a file\n", encoding="utf-8")

    if (wt / "FOO.TXT").exists():
        _git(wt, "mv", "foo.txt", "Foo.txt")
    _commit_all(wt, "refactor")

    assert mwt.main(["--repo", str(repo), "apply", str(wt)]) == 0
    capsys.readouterr()
    assert (repo / "mod" / "init.py").read_text(encoding="utf-8") == "mod_dir = True\n"
    assert (repo / "sym" / "child.txt").read_text(encoding="utf-8") == "new real child\n"
    assert (ext_dir / "child.txt").read_text(encoding="utf-8") == "EXTERNAL_TARGET\n"
    assert (repo / "pkg").is_file() and (repo / "pkg").read_text(encoding="utf-8") == "now a file\n"
    if (repo / "FOO.TXT").exists():
        assert "Foo.txt" in _git(repo, "ls-files").stdout.splitlines()

    # 2. Dir->File MUST refuse gitignored files, symlink-to-dirs, empty dirs & 0o500 empty subdirs
    for extra_kind in ("ignored_file", "symlink_dir", "untracked_empty", "ro_empty_subdir"):
        ig_repo = tmp_path / f"dir_refuse_{extra_kind}"
        _seed_repo(ig_repo, {"pkg/old.py": "old\n", "other.txt": "orig\n", ".gitignore": "*.env\n"})
        wt_d, _, _ = _create_wt(ig_repo, wt_base, f"df-{extra_kind}")
        if extra_kind == "untracked_empty":
            (wt_d / "untracked_empty").write_text("file\n", encoding="utf-8")
        else:
            shutil.rmtree(wt_d / "pkg")
            (wt_d / "pkg").write_text("file\n", encoding="utf-8")
            (wt_d / "other.txt").write_text("mutated\n", encoding="utf-8")
        _commit_all(wt_d, f"refactor {extra_kind}")

        ro_parent: Path | None = None
        if extra_kind == "ignored_file":
            (ig_repo / "pkg" / "secret.env").write_text("KEEP=1\n", encoding="utf-8")
        elif extra_kind == "symlink_dir":
            (ig_repo / "pkg" / "linkdir").symlink_to(ext_dir)
        elif extra_kind == "untracked_empty":
            (ig_repo / "untracked_empty" / "sub").mkdir(parents=True)
        else:
            ro_parent = ig_repo / "pkg" / "sub"
            (ro_parent / "empty_ro").mkdir(parents=True)
            os.chmod(ro_parent, 0o500)
        try:
            assert mwt.main(["--repo", str(ig_repo), "apply", str(wt_d)]) == 1
            assert "refusing to overwrite" in capsys.readouterr().err
            if extra_kind == "ignored_file":
                assert (ig_repo / "pkg" / "secret.env").read_text(encoding="utf-8") == "KEEP=1\n"
            elif extra_kind == "symlink_dir":
                assert (ig_repo / "pkg" / "linkdir").is_symlink()
            elif extra_kind == "untracked_empty":
                assert (ig_repo / "untracked_empty" / "sub").is_dir()
            else:
                assert (ig_repo / "other.txt").read_text(encoding="utf-8") == "orig\n"
                assert (ig_repo / "pkg" / "old.py").read_text(encoding="utf-8") == "old\n"
                assert (ig_repo / "pkg" / "sub" / "empty_ro").is_dir()
        finally:
            if ro_parent is not None:
                os.chmod(ro_parent, 0o700)

    # 3. Hardlink inode aliasing (gitignored secret.env hardlinked to deleted tracked old.txt)
    hl_repo = tmp_path / "hardlink_refuse"
    _seed_repo(hl_repo, {"old.txt": "original\n", ".gitignore": "*.env\n"})
    wt_hl, _, _ = _create_wt(hl_repo, wt_base, "hl")
    (wt_hl / "old.txt").unlink()
    (wt_hl / "secret.env").write_text("OVERWRITTEN\n", encoding="utf-8")
    _git(wt_hl, "add", "-A")
    _git(wt_hl, "add", "-f", "secret.env")
    _git(wt_hl, "commit", "-q", "-m", "replace via hardlink name")
    os.link(hl_repo / "old.txt", hl_repo / "secret.env")
    assert mwt.main(["--repo", str(hl_repo), "apply", str(wt_hl)]) == 1
    assert "refusing to overwrite" in capsys.readouterr().err
    assert (hl_repo / "secret.env").read_text(encoding="utf-8") == "original\n"


def test_cleanup_registered_dotgit_apfs_branch_unreadable_orphan_missing_dotdot_and_deep_rm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    _seed_repo(repo, {"a.txt": "a\n"})
    wt_base = tmp_path / "wts"

    # 1. Nested .git inside a REGISTERED worktree refused upfront
    wt_reg, br_reg, _ = _create_wt(repo, wt_base, "reg-nested")
    _seed_repo(wt_reg / "sub_repo", {"s.txt": "precious\n"})
    assert mwt.main(
        ["--repo", str(repo), "--wt-base", str(wt_base), "cleanup", str(wt_reg), br_reg]
    ) == 1
    assert "nested .git" in capsys.readouterr().err
    assert (wt_reg / "sub_repo" / "s.txt").is_file()

    # 2. Outside-wt-base registered worktree with missing .git file refused
    outside_wt = tmp_path / "outside_dev_swarm_wt"
    _git(repo, "worktree", "add", "-q", "-b", "dev-swarm/outside-1", str(outside_wt), "HEAD")
    (outside_wt / ".git").unlink()
    (outside_wt / "keep.txt").write_text("keep\n", encoding="utf-8")
    assert mwt.main(["--repo", str(repo), "cleanup", str(outside_wt)]) == 1
    assert "unverified worktree" in capsys.readouterr().err
    assert (outside_wt / "keep.txt").is_file()

    # 3. APFS case-variant branch ref (`dev-swarm/ACTIVE-HEAD`) & active/sibling HEAD protection
    wt_case, _, _ = _create_wt(repo, wt_base, "case-br")
    _git(repo, "checkout", "-q", "-b", "dev-swarm/active-head")
    if (repo / ".git" / "refs" / "heads" / "dev-swarm" / "ACTIVE-HEAD").exists():
        assert mwt.main(
            ["--repo", str(repo), "--wt-base", str(wt_base),
             "cleanup", str(wt_case), "dev-swarm/ACTIVE-HEAD"]
        ) == 1
        assert "case-mismatched" in capsys.readouterr().err
        assert (
            _git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip() == "dev-swarm/active-head"
        )
        assert (repo / ".git" / "refs" / "heads" / "dev-swarm" / "active-head").is_file()
        assert wt_case.is_dir()

        for upper_head in ("refs/heads/DEV-SWARM/active-head", "refs/heads/dev-swarm/ACTIVE-HEAD"):
            _git(repo, "symbolic-ref", "HEAD", upper_head)
            assert mwt.main(
                ["--repo", str(repo), "--wt-base", str(wt_base),
                 "cleanup", str(wt_case), "dev-swarm/active-head"]
            ) == 1
            assert "checked-out" in capsys.readouterr().err
            assert _git(repo, "rev-parse", "--verify", "HEAD").returncode == 0
            assert (repo / ".git" / "refs" / "heads" / "dev-swarm" / "active-head").is_file()

        _git(repo, "checkout", "-q", "main")
        wt_sib = wt_base / "sib-case-wt"
        _git(repo, "worktree", "add", "-q", "-b", "dev-swarm/sib", str(wt_sib), "HEAD")
        _git(wt_sib, "symbolic-ref", "HEAD", "refs/heads/dev-swarm/SIB")
        assert mwt.main(
            ["--repo", str(repo), "--wt-base", str(wt_base),
             "cleanup", str(wt_case), "dev-swarm/sib"]
        ) == 1
        assert "another worktree" in capsys.readouterr().err
        assert _git(wt_sib, "rev-parse", "--verify", "HEAD").returncode == 0

        up_repo = tmp_path / "upper_ns_repo"
        _seed_repo(up_repo, {"u.txt": "u\n"})
        (up_repo / ".git" / "refs" / "heads" / "DEV-SWARM").mkdir(parents=True)
        assert mwt.main(
            ["--repo", str(up_repo), "--wt-base", str(wt_base), "create", "iter-up"]
        ) == 1
        assert "case-mismatched" in capsys.readouterr().err
    _git(repo, "checkout", "-q", "main")

    # 4. Unreadable 0o111 subdir hiding .git inside orphan fails closed without deleting siblings
    unreadable_orphan = wt_base / "orphan_unreadable"
    hidden_repo = unreadable_orphan / "locked_sub"
    _seed_repo(hidden_repo, {"inner.txt": "keep\n"})
    sibling = unreadable_orphan / "sibling.txt"
    sibling.write_text("must survive\n", encoding="utf-8")
    os.chmod(hidden_repo, 0o111)
    try:
        assert mwt.main(
            ["--repo", str(repo), "--wt-base", str(wt_base), "cleanup", str(unreadable_orphan)]
        ) == 1
        assert "ERROR:" in capsys.readouterr().err
        assert sibling.read_text(encoding="utf-8") == "must survive\n"
    finally:
        os.chmod(hidden_repo, 0o700)

    # 5. `--wt-base <missing>/../<symlink>` and `cleanup <wt_base>/<missing>/../<live_wt>` refused
    outside_dir = tmp_path / "outside_target"
    outside_dir.mkdir()
    sym_dir = tmp_path / "sym_target"
    sym_dir.symlink_to(outside_dir)
    bad_missing_base = tmp_path / "does_not_exist" / ".." / "sym_target"
    assert mwt.main(
        ["--repo", str(repo), "--wt-base", str(bad_missing_base), "create", "bad-md"]
    ) == 1
    assert "ERROR:" in capsys.readouterr().err

    wt_dd, br_dd, _ = _create_wt(repo, wt_base, "live-dd")
    _git(wt_dd, "checkout", "--detach", "-q")
    bad_wt_arg = wt_base / "does_not_exist" / ".." / wt_dd.name
    assert mwt.main(
        ["--repo", str(repo), "--wt-base", str(wt_base), "cleanup", str(bad_wt_arg), br_dd]
    ) == 1
    dd_cap = capsys.readouterr()
    assert "ERROR:" in dd_cap.err and "CLEANED=" not in dd_cap.out
    assert wt_dd.is_dir()
    assert _git(repo, "rev-parse", "--verify", f"refs/heads/{br_dd}").returncode == 0

    # 6. Bounded iterative `_rmtree_owned_dir` cleans a 255-deep dir tree (beyond PATH_MAX & FD cap)
    deep_root = tmp_path / "deep_tree"
    deep_root.mkdir()
    dir_fd = os.open(str(deep_root), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(255):
            os.mkdir("d", dir_fd=dir_fd)
            nxt_fd = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = nxt_fd
        leaf_fd = os.open(
            "leaf.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=dir_fd
        )
        try:
            os.write(leaf_fd, b"deep\n")
        finally:
            os.close(leaf_fd)
    finally:
        os.close(dir_fd)
    mwt._rmtree_owned_dir(deep_root)
    assert not deep_root.exists()

    # 7. `_run_git` timeout AND KeyboardInterrupt terminate process group & close pipes cleanly
    real_popen = subprocess.Popen
    spawned_pids: list[int] = []

    def sleep_popen(*_a: object, **_kw: object) -> subprocess.Popen[bytes]:
        p = real_popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        spawned_pids.append(p.pid)
        return p

    with monkeypatch.context() as mp:
        mp.setattr(subprocess, "Popen", sleep_popen)
        mp.setattr(mwt, "_GIT_SUBPROCESS_TIMEOUT_SECONDS", 0.2)
        res = mwt._run_git(repo, ["status"])
    assert res.returncode == 124
    assert spawned_pids
    with pytest.raises(ProcessLookupError):
        os.kill(spawned_pids[-1], 0)

    def interrupt_popen(*_a: object, **_kw: object) -> subprocess.Popen[bytes]:
        p = sleep_popen()

        def raise_ki(
            input: bytes | None = None, timeout: float | None = None
        ) -> tuple[bytes, bytes]:
            del input, timeout
            raise KeyboardInterrupt()

        p.communicate = raise_ki  # type: ignore[method-assign]
        return p

    with monkeypatch.context() as mp:
        mp.setattr(subprocess, "Popen", interrupt_popen)
        with pytest.raises(KeyboardInterrupt):
            mwt._run_git(repo, ["status"])
    with pytest.raises(ProcessLookupError):
        os.kill(spawned_pids[-1], 0)

    # 8. `_terminate_proc_group` kills SIGTERM-ignoring grandchildren; `_read_squash_msg` no FD leak
    gc_pid_file = tmp_path / "gc_pid.txt"
    leader_code = (
        "import subprocess, sys, time; "
        "gc = subprocess.Popen([sys.executable, '-c', "
        "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)']); "
        f"open({str(gc_pid_file)!r}, 'w').write(str(gc.pid)); "
        "time.sleep(30)"
    )
    leader = real_popen(
        [sys.executable, "-c", leader_code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not (gc_pid_file.is_file() and gc_pid_file.read_text()):
        time.sleep(0.02)
    gc_pid = int(gc_pid_file.read_text().strip())
    mwt._terminate_proc_group(leader)
    wait_dead = time.monotonic() + 2.0
    while time.monotonic() < wait_dead:
        try:
            os.kill(gc_pid, 0)
            time.sleep(0.02)
        except ProcessLookupError:
            break
    with pytest.raises(ProcessLookupError):
        os.kill(gc_pid, 0)

    sq_file = tmp_path / "SQUASH_MSG"
    sq_file.write_text("saved\n", encoding="utf-8")
    opened_fds: set[int] = set()
    real_open, real_close, real_fstat = os.open, os.close, os.fstat

    def track_open(path: object, flags: int, *a: object, **kw: object) -> int:
        fd = real_open(path, flags, *a, **kw)  # type: ignore[arg-type]
        if str(path) == str(sq_file):
            opened_fds.add(fd)
        return fd

    def track_close(fd: int) -> None:
        opened_fds.discard(fd)
        real_close(fd)

    def boom_fstat(fd: int) -> os.stat_result:
        if fd in opened_fds:
            raise OSError("fstat failed")
        return real_fstat(fd)

    with monkeypatch.context() as mp:
        mp.setattr(os, "open", track_open)
        mp.setattr(os, "close", track_close)
        mp.setattr(os, "fstat", boom_fstat)
        assert mwt._read_squash_msg_nofollow(sq_file) == (True, None)
    assert opened_fds == set()


# ---------------------------------------------------------------------------
# 5. Skill artifact, token-isolation, symlink, init.py integration & doctor
# ---------------------------------------------------------------------------


def test_dev_swarm_skill_artifacts_token_isolation_and_init_integration(tmp_path: Path) -> None:
    core_dir = REAL_REPO / "core" / "dev-swarm"
    adapter_dir = REAL_REPO / "harnesses" / "agy" / "dev-swarm"
    adapter_skill = adapter_dir / "SKILL.md"
    core_link = adapter_dir / "core"

    assert SCRIPT_PATH.is_file() and os.access(SCRIPT_PATH, os.X_OK)
    script_text = SCRIPT_PATH.read_text(encoding="utf-8")
    script_lines = script_text.splitlines(keepends=True)
    assert len(script_lines) <= 1600
    assert len(script_text.encode("utf-8")) < 65536
    for start in (0, 800):
        slice_bytes = sum(len(line.encode("utf-8")) for line in script_lines[start : start + 800])
        assert slice_bytes < 46080

    assert adapter_skill.is_file()
    adapter_text = adapter_skill.read_text(encoding="utf-8")
    assert 60 <= len(adapter_text.splitlines()) <= 100
    assert init.validate_skill_dir(adapter_dir) == []

    for required_binding in (
        "MODEL_MAP",
        "SPAWN_WORKER",
        "WAIT_FOR_WORKERS",
        "SWITCH_MODEL",
        "KILL_WORKER",
        "HANDOFF",
        "READ_ARTIFACT",
        "CLEANROOM_BARRIER",
        "DECISION_LEDGER",
        "CARBON_HIGH",
        "NEON_XHIGH",
        "OPUS_HIGH",
        "MIN_ALLOWED_SEQ",
        "<= MIN_ALLOWED_SEQ",
        "last_seen_seq",
        "<SKILLS_ROOT>",
        "<CORE_DEV_SWARM_DIR>",
        "<CORE_REVIEW_DIR>",
        "fork",
        "archive",
        "dev_swarm_decisions.md",
        "swarm_monitor",
        "rev-k-seam",
        "coder-k-fix",
        "@rev-k-seam [fixed]",
        "r<k>-<j>-p<pass>-<role>",
        "r<k>s-p<pass>-<role>",
        "Append=True",
        "mktemp -d /tmp/agy-wt-helper.XXXXXX",
        "git reflog",
        "commit-rev-k-p<r>",
    ):
        assert required_binding in adapter_text, f"Missing adapter binding: {required_binding!r}"

    # Verify core markdown protocols retain key invariants, caps, and canonical normpath links
    algo_text = (core_dir / "algorithm.md").read_text(encoding="utf-8")
    assert '--repo "<REPO_ROOT>"' in algo_text
    assert "<CORE_REVIEW_DIR>" in algo_text
    assert "3 rounds" in algo_text and "4 fix cycles" in algo_text
    assert "2 disputed items" in algo_text and "[hygiene-fix-needed]" in algo_text
    assert 'HANDOFF(SEAM_REVIEWER, "[fixed]"' in algo_text

    pc_text = (core_dir / "personas" / "planner-critic.md").read_text(encoding="utf-8")
    assert "3 `Planner` <-> `Critic` rounds" in pc_text and "post-escalation" in pc_text

    cr_text = (core_dir / "personas" / "coder-reviewer.md").read_text(encoding="utf-8")
    assert "Multi-Coder Seam Reviewer" in cr_text and "4 review cycles" in cr_text
    assert "### Context Collection" in cr_text
    assert cr_text.index("### Context Collection") < cr_text.index(
        "1. **Pass 1 — Right-Sized Multi-Agent `/review` Gate"
    )
    assert "Pass 2+ — Direct Delta Verification" in cr_text
    assert "No Goalpost-Moving" in cr_text

    cc_text = (core_dir / "personas" / "cleanroom-commit.md").read_text(encoding="utf-8")
    assert "git reflog" in cc_text and "mktemp /tmp/commit-msg.XXXXXX" in cc_text

    # Canonical dereferenced core paths resolve relative markdown links cleanly under normpath
    link_re = re.compile(r"\[[^\]]+\]\((\.\.?/[^)#?]+)\)")
    for md_path in sorted(core_dir.rglob("*.md")):
        for rel_target in link_re.findall(md_path.read_text(encoding="utf-8")):
            joined = Path(os.path.normpath(str(md_path.parent / rel_target)))
            assert joined.exists(), f"Broken normpath link {rel_target!r} in {md_path}"

    assert core_link.is_symlink()
    assert os.readlink(core_link) == "../../../core/dev-swarm"
    assert core_link.resolve(strict=True) == core_dir.resolve(strict=True)

    at_coord = "@" + "coordinator"
    forbidden_patterns: tuple[tuple[str, re.Pattern[str]], ...] = (
        (
            "model_or_harness_word",
            re.compile(
                r"\b(?:carbon|neon|opus|xhigh|flash(?:_lite)?|antigravity)\b",
                re.IGNORECASE,
            ),
        ),
        ("swarm_wait", re.compile(r"\bswarm_wait\b")),
        ("read_board", re.compile(r"\bread_board\b")),
        ("post_message", re.compile(r"\bpost_message\b")),
        ("invoke_subagent", re.compile(r"\binvoke_subagent\b")),
        ("open_board", re.compile(r"\bopen_board\b")),
        ("swarm{", re.compile(r"swarm\{")),
        ("swarm_monitor", re.compile(r"\bswarm_monitor\b")),
        ("board.jsonl", re.compile(r"board\.jsonl")),
        ("transcript.jsonl", re.compile(r"transcript\.jsonl")),
        ("artifactDir", re.compile(r"\bartifactDir\b")),
        ("customization://", re.compile(r"customization://")),
        ("#seq", re.compile(r"#seq\b")),
        ("#plan_seq", re.compile(r"#plan_seq\b")),
        ("MIN_ALLOWED_SEQ", re.compile(r"\bMIN_ALLOWED_SEQ\b")),
        ("LAUNCH_SEQ", re.compile(r"\bLAUNCH_SEQ\b")),
        ("#commit-", re.compile(r"#commit-")),
        (".gemini", re.compile(r"\.gemini\b")),
        ("at_coordinator", re.compile(re.escape(at_coord))),
    )

    scanned_files = sorted(
        p for p in core_dir.rglob("*") if p.is_file() and "__pycache__" not in p.parts
    )
    expected_core = {
        core_dir / "algorithm.md",
        core_dir / "personas" / "researcher.md",
        core_dir / "personas" / "planner-critic.md",
        core_dir / "personas" / "coder-reviewer.md",
        core_dir / "personas" / "cleanroom-commit.md",
        SCRIPT_PATH,
    }
    assert expected_core.issubset(set(scanned_files))
    for path in scanned_files:
        text = path.read_text(encoding="utf-8")
        for label, pattern in forbidden_patterns:
            assert pattern.search(text) is None, (
                f"Forbidden harness/model token {label!r} found in {path.relative_to(REAL_REPO)}"
            )

    # Real-repo end-to-end init.py apply --replace + status against isolated tmp_home
    tmp_home = tmp_path / "isolated_apply_home"
    tmp_home.mkdir()
    assert init.main(["--repo", str(REAL_REPO), "apply", "--replace", "--home", str(tmp_home)]) == 0
    assert init.main(["--repo", str(REAL_REPO), "status", "--home", str(tmp_home)]) == 0
    assert (tmp_home / ".agents" / "skills").resolve() == (REAL_REPO / "shared").resolve()
    assert init.cmd_doctor(REAL_REPO) == 0
