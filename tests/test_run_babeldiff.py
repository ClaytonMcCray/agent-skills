"""Unit and integration tests for core/dev-swarm-zircon/scripts/run_babeldiff.py."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import init

REAL_REPO = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REAL_REPO / "core" / "dev-swarm-zircon" / "scripts" / "run_babeldiff.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_babeldiff", SCRIPT_PATH)
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


rb = _load_module()


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


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env=_git_env(),
        capture_output=True,
        text=True,
        check=check,
    )


def _init_git_repo(repo: Path) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    return repo


def _install_fake_babeldiff(
    fuchsia_dir: Path,
    *,
    exit_code: int = 0,
    stdout_payload: str | None = None,
) -> Path:
    """Create a fake local/babeldiff repo marker and executable python babeldiff stub."""
    bd_repo = fuchsia_dir / "local" / "babeldiff"
    (bd_repo / ".git").mkdir(parents=True, exist_ok=True)
    bin_path = bd_repo / "target" / "release" / "babeldiff"
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    script = (
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "stdin_data = '' if sys.stdin.isatty() else sys.stdin.read()\n"
        "payload = {\n"
        "    'argv': sys.argv[1:],\n"
        "    'cwd': os.getcwd(),\n"
        "    'stdin': stdin_data,\n"
        "}\n"
        f"custom_out = {stdout_payload!r}\n"
        "if custom_out is not None:\n"
        "    sys.stdout.write(custom_out)\n"
        "else:\n"
        "    sys.stdout.write(json.dumps(payload))\n"
        f"sys.exit({exit_code})\n"
    )
    bin_path.write_text(script, encoding="utf-8")
    bin_path.chmod(0o755)
    return bin_path


def _run_cli(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        cwd=str(cwd) if cwd is not None else None,
        env=_git_env(env_extra),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


class TestPlatformTagMapping:
    @pytest.mark.parametrize(
        ("raw_os", "raw_arch", "expected"),
        [
            ("Linux", "x86_64", "linux-x64"),
            ("linux", "amd64", "linux-x64"),
            ("linux2", "AMD64", "linux-x64"),
            ("Linux", "aarch64", "linux-arm64"),
            ("Linux", "arm64", "linux-arm64"),
            ("Darwin", "arm64", "mac-arm64"),
            ("darwin", "aarch64", "mac-arm64"),
            ("Darwin", "x86_64", "mac-x64"),
            ("mac", "x64", "mac-x64"),
        ],
    )
    def test_valid_platform_tags(self, raw_os: str, raw_arch: str, expected: str) -> None:
        assert rb.resolve_prebuilt_rust_tag(raw_os, raw_arch) == expected

    @pytest.mark.parametrize(
        ("raw_os", "raw_arch"),
        [
            ("Windows", "x86_64"),
            ("FreeBSD", "x86_64"),
            ("Linux", "riscv64"),
            ("Darwin", "ppc64"),
        ],
    )
    def test_unsupported_platform_raises(self, raw_os: str, raw_arch: str) -> None:
        with pytest.raises(ValueError, match="unsupported host"):
            rb.resolve_prebuilt_rust_tag(raw_os, raw_arch)


class TestArgParsing:
    def test_script_is_executable(self) -> None:
        mode = SCRIPT_PATH.stat().st_mode
        assert bool(mode & stat.S_IXUSR)

    def test_help_flag(self) -> None:
        res = _run_cli(["--help"])
        assert res.returncode == 0
        assert "--allow-issues" in res.stdout
        assert "tree" in res.stdout
        assert "commit <rev>" in res.stdout
        assert "raw" in res.stdout

    def test_parse_preserves_flags_after_mode(self) -> None:
        parsed = rb.parse_args(
            [
                "--update",
                "--fuchsia=/tmp/fx",
                "--allow-issues",
                "tree",
                "--summary",
                "--issues-only",
                "--pair",
                "Foo::Bar=foo_bar",
                "--help",
            ]
        )
        assert parsed.update is True
        assert parsed.fuchsia == "/tmp/fx"
        assert parsed.allow_issues is True
        assert parsed.mode == "tree"
        assert parsed.mode_args == (
            "--summary",
            "--issues-only",
            "--pair",
            "Foo::Bar=foo_bar",
            "--help",
        )

    @pytest.mark.parametrize(
        "argv",
        [
            [],
            ["--bogus", "tree"],
            ["--fuchsia"],
            ["unknown-mode"],
            ["commit"],
            ["commit", "   "],
            ["commit", "--summary"],
        ],
    )
    def test_invalid_cli_exits_2(self, argv: list[str]) -> None:
        res = _run_cli(argv)
        assert res.returncode == 2
        assert "error:" in res.stderr

    def test_nonexistent_fuchsia_dir_exits_2(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        res = _run_cli(["--fuchsia", str(missing), "raw", "--version"])
        assert res.returncode == 2
        assert "does not exist" in res.stderr


class TestBootstrapAndUpdate:
    def test_clone_and_build_when_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fuchsia = tmp_path / "fuchsia"
        fuchsia.mkdir()
        calls: list[tuple[list[str], Path, dict[str, str] | None]] = []

        def fake_run(
            cmd: Sequence[str],
            *,
            cwd: Path | None = None,
            env: dict[str, str] | None = None,
            **_kwargs: Any,
        ) -> subprocess.CompletedProcess[str]:
            assert cwd is not None
            calls.append((list(cmd), Path(cwd), env))
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(rb.subprocess, "run", fake_run)
        monkeypatch.setattr(rb, "resolve_prebuilt_rust_tag", lambda: "linux-x64")

        bin_path = rb.ensure_babeldiff(fuchsia, update=False)

        assert bin_path == fuchsia / "local" / "babeldiff" / "target" / "release" / "babeldiff"
        assert (fuchsia / "local").is_dir()
        assert len(calls) == 2

        clone_cmd, clone_cwd, _ = calls[0]
        assert clone_cmd == ["git", "clone", rb.BABELDIFF_REPO_URL, "local/babeldiff"]
        assert clone_cwd == fuchsia

        build_cmd, build_cwd, build_env = calls[1]
        assert build_cmd == [
            "cargo",
            "build",
            "--release",
            "--manifest-path",
            "local/babeldiff/Cargo.toml",
            "--target-dir",
            "local/babeldiff/target",
        ]
        assert build_cwd == fuchsia
        assert build_env is not None
        assert "CARGO_TARGET_DIR" not in build_env
        expected_prebuilt = str(fuchsia / "prebuilt" / "third_party" / "rust" / "linux-x64" / "bin")
        assert build_env["PATH"].split(os.pathsep)[0] == expected_prebuilt

    def test_update_pulls_and_rebuilds_when_repo_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fuchsia = tmp_path / "fuchsia"
        _install_fake_babeldiff(fuchsia)
        calls: list[tuple[list[str], Path, dict[str, str] | None]] = []

        def fake_run(
            cmd: Sequence[str],
            *,
            cwd: Path | None = None,
            env: dict[str, str] | None = None,
            **_kwargs: Any,
        ) -> subprocess.CompletedProcess[str]:
            assert cwd is not None
            calls.append((list(cmd), Path(cwd), env))
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(rb.subprocess, "run", fake_run)
        monkeypatch.setattr(rb, "resolve_prebuilt_rust_tag", lambda: "mac-arm64")
        monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path / "external-target"))
        monkeypatch.setenv("CARGO_BUILD_TARGET", "x86_64-unknown-fuchsia")

        rb.ensure_babeldiff(fuchsia, update=True)

        assert len(calls) == 2
        pull_cmd, pull_cwd, _ = calls[0]
        assert pull_cmd == ["git", "-C", "local/babeldiff", "pull", "--ff-only"]
        assert pull_cwd == fuchsia

        build_cmd, build_cwd, build_env = calls[1]
        assert build_cmd == [
            "cargo",
            "build",
            "--release",
            "--manifest-path",
            "local/babeldiff/Cargo.toml",
            "--target-dir",
            "local/babeldiff/target",
        ]
        assert build_cwd == fuchsia
        assert build_env is not None
        assert "CARGO_TARGET_DIR" not in build_env
        assert "CARGO_BUILD_TARGET" not in build_env
        expected_prebuilt = str(fuchsia / "prebuilt" / "third_party" / "rust" / "mac-arm64" / "bin")
        assert build_env["PATH"].split(os.pathsep)[0] == expected_prebuilt

    def test_skips_bootstrap_when_binary_ready_and_no_update(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fuchsia = tmp_path / "fuchsia"
        expected_bin = _install_fake_babeldiff(fuchsia)

        def fail_if_called(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("bootstrap command should not run when binary is executable")

        monkeypatch.setattr(rb.subprocess, "run", fail_if_called)

        assert rb.ensure_babeldiff(fuchsia, update=False) == expected_bin

    def test_rebuilds_without_pull_when_binary_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fuchsia = tmp_path / "fuchsia"
        (fuchsia / "local" / "babeldiff" / ".git").mkdir(parents=True)
        calls: list[list[str]] = []

        def fake_run(cmd: Sequence[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(rb.subprocess, "run", fake_run)
        monkeypatch.setattr(rb, "resolve_prebuilt_rust_tag", lambda: "linux-x64")

        rb.ensure_babeldiff(fuchsia, update=False)
        assert calls == [
            [
                "cargo",
                "build",
                "--release",
                "--manifest-path",
                "local/babeldiff/Cargo.toml",
                "--target-dir",
                "local/babeldiff/target",
            ]
        ]

    def test_bootstrap_failure_exits_2_even_with_allow_issues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fuchsia = tmp_path / "fuchsia"
        fuchsia.mkdir()

        def failing_run(cmd: Sequence[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 128)

        monkeypatch.setattr(rb.subprocess, "run", failing_run)

        rc = rb.main(["--fuchsia", str(fuchsia), "--allow-issues", "tree", "--summary"])
        captured = capsys.readouterr()
        assert rc == 2
        assert "git clone babeldiff failed" in captured.err

    def test_linked_worktree_uses_main_checkout_prebuilt_and_clears_cargo_build_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        main_repo = _init_git_repo(tmp_path / "fuchsia")
        (main_repo / "README.md").write_text("fuchsia\n", encoding="utf-8")
        _git(main_repo, "add", "README.md")
        _git(main_repo, "commit", "-q", "-m", "init")

        host_tag = rb.resolve_prebuilt_rust_tag()
        main_prebuilt = main_repo / "prebuilt" / "third_party" / "rust" / host_tag / "bin"
        main_prebuilt.mkdir(parents=True)
        marker = tmp_path / "cargo-invoked.json"
        fake_cargo = main_prebuilt / "cargo"
        fake_cargo.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys\n"
            "payload = {\n"
            "    'argv': sys.argv[1:],\n"
            "    'cwd': os.getcwd(),\n"
            "    'target_dir': os.environ.get('CARGO_TARGET_DIR'),\n"
            "    'build_target': os.environ.get('CARGO_BUILD_TARGET'),\n"
            "    'path': os.environ.get('PATH', ''),\n"
            "}\n"
            f"pathlib.Path({str(marker)!r}).write_text(json.dumps(payload), encoding='utf-8')\n"
            "out_bin = (\n"
            "    pathlib.Path(os.getcwd()) / 'local' / 'babeldiff' / 'target'\n"
            "    / 'release' / 'babeldiff'\n"
            ")\n"
            "out_bin.parent.mkdir(parents=True, exist_ok=True)\n"
            "out_bin.write_text('#!/bin/sh\\nexit 0\\n', encoding='utf-8')\n"
            "out_bin.chmod(0o755)\n",
            encoding="utf-8",
        )
        fake_cargo.chmod(0o755)

        wt = main_repo / ".jiri_root" / "worktrees" / "port-wt"
        wt.parent.mkdir(parents=True, exist_ok=True)
        _git(main_repo, "worktree", "add", "-q", "-b", "port-wt", str(wt))
        assert (wt / ".git").is_file()
        assert not (wt / "prebuilt").exists()
        (wt / "local" / "babeldiff" / ".git").mkdir(parents=True)

        monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path / "wrong-target"))
        monkeypatch.setenv("CARGO_BUILD_TARGET", "x86_64-unknown-fuchsia")

        bin_path = rb.ensure_babeldiff(wt, update=False)
        assert bin_path == wt / "local" / "babeldiff" / "target" / "release" / "babeldiff"
        assert marker.is_file()

        data = json.loads(marker.read_text(encoding="utf-8"))
        assert data["target_dir"] is None
        assert data["build_target"] is None
        assert Path(data["cwd"]).resolve() == wt.resolve()
        assert data["path"].split(os.pathsep)[0] == str(main_prebuilt.resolve())
        assert data["argv"] == [
            "build",
            "--release",
            "--manifest-path",
            "local/babeldiff/Cargo.toml",
            "--target-dir",
            "local/babeldiff/target",
        ]

        # If the worktree itself has a prebuilt dir, prefer it over the main checkout fallback.
        wt_prebuilt = wt / "prebuilt" / "third_party" / "rust" / host_tag / "bin"
        wt_prebuilt.mkdir(parents=True)
        assert rb._resolve_prebuilt_rust_bin(wt, host_tag) == wt_prebuilt


class TestModesAndExitCodes:
    def test_tree_mode_streams_git_diff_head(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path / "fx")
        target = repo / "sample.rs"
        target.write_text("fn old() {}\n", encoding="utf-8")
        _git(repo, "add", "sample.rs")
        _git(repo, "commit", "-q", "-m", "initial")
        target.write_text("fn new_impl() {}\n", encoding="utf-8")

        _install_fake_babeldiff(repo, exit_code=0)

        res = _run_cli(
            ["--fuchsia", str(repo), "tree", "--summary", "--issues-only"],
        )
        assert res.returncode == 0
        assert "babeldiff exit 0 (0 clean, 1 issues, 2 error)" in res.stderr

        data = json.loads(res.stdout)
        assert data["argv"] == ["patch", "-C", ".", "--base", "HEAD", "--summary", "--issues-only"]
        assert Path(data["cwd"]).resolve() == repo.resolve()
        assert "-fn old() {}" in data["stdin"]
        assert "+fn new_impl() {}" in data["stdin"]

    def test_tree_mode_includes_new_rs_file_registered_with_git_add_n(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path / "fx")
        (repo / "existing.cc").write_text("int foo() { return 1; }\n", encoding="utf-8")
        _git(repo, "add", "existing.cc")
        _git(repo, "commit", "-q", "-m", "initial")

        new_rs = repo / "new_port.rs"
        new_rs.write_text("pub fn foo() -> i32 { 1 }\n", encoding="utf-8")
        _install_fake_babeldiff(repo, exit_code=0)

        before = _run_cli(["--fuchsia", str(repo), "tree", "--summary"])
        assert before.returncode == 0
        assert "new_port.rs" not in json.loads(before.stdout)["stdin"]

        _git(repo, "add", "-N", "--", "new_port.rs")
        after = _run_cli(["--fuchsia", str(repo), "tree", "--summary"])
        assert after.returncode == 0
        diff_text = json.loads(after.stdout)["stdin"]
        assert "a/new_port.rs" in diff_text or "b/new_port.rs" in diff_text
        assert "+pub fn foo() -> i32 { 1 }" in diff_text
        # Confirm run_babeldiff.py left the index status untouched (still intent-to-add).
        assert _git(repo, "status", "--porcelain", "--", "new_port.rs").stdout == " A new_port.rs\n"

    def test_tree_mode_overrides_corrupting_git_diff_config(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path / "fx")
        target = repo / "sample.rs"
        target.write_text("fn a() {}\n", encoding="utf-8")
        _git(repo, "add", "sample.rs")
        _git(repo, "commit", "-q", "-m", "initial")
        _git(repo, "config", "diff.noprefix", "true")
        _git(repo, "config", "diff.mnemonicPrefix", "true")
        _git(repo, "config", "color.diff", "always")
        _git(repo, "config", "diff.external", "false")
        target.write_text("fn b() {}\n", encoding="utf-8")

        _install_fake_babeldiff(repo, exit_code=0)
        res = _run_cli(["--fuchsia", str(repo), "tree", "--summary"])
        assert res.returncode == 0
        diff_text = json.loads(res.stdout)["stdin"]
        assert "\x1b[" not in diff_text
        assert "diff --git a/sample.rs b/sample.rs" in diff_text

    def test_tree_mode_early_exit_2_on_large_diff_does_not_blame_git(
        self, tmp_path: Path
    ) -> None:
        repo = _init_git_repo(tmp_path / "fx")
        big_file = repo / "large.rs"
        big_file.write_text("line\n", encoding="utf-8")
        _git(repo, "add", "large.rs")
        _git(repo, "commit", "-q", "-m", "initial")
        # Exceed typical 64 KiB pipe buffer so git diff receives SIGPIPE when babeldiff exits early.
        big_file.write_text("pub const X: u64 = 1234567890;\n" * 8000, encoding="utf-8")

        bd_repo = repo / "local" / "babeldiff"
        (bd_repo / ".git").mkdir(parents=True, exist_ok=True)
        bin_path = bd_repo / "target" / "release" / "babeldiff"
        bin_path.parent.mkdir(parents=True, exist_ok=True)
        bin_path.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "sys.stderr.write('invalid flag\\n')\n"
            "sys.exit(2)\n",
            encoding="utf-8",
        )
        bin_path.chmod(0o755)

        res = _run_cli(["--fuchsia", str(repo), "--allow-issues", "tree", "--bogus-flag"])
        assert res.returncode == 2
        assert "invalid flag" in res.stderr
        assert "git diff HEAD failed" not in res.stderr
        assert "babeldiff exit 2 (0 clean, 1 issues, 2 error)" in res.stderr

    def test_commit_mode_passes_rev_and_flags(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path / "fx")
        _install_fake_babeldiff(repo, exit_code=0)

        res = _run_cli(
            [
                "--fuchsia",
                str(repo),
                "commit",
                "HEAD~1..HEAD",
                "--layout",
                "stacked",
                "-U",
                "3",
            ]
        )
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert data["argv"] == ["git", "HEAD~1..HEAD", "--layout", "stacked", "-U", "3"]
        assert "babeldiff exit 0 (0 clean, 1 issues, 2 error)" in res.stderr

    def test_raw_mode_passthrough(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path / "fx")
        _install_fake_babeldiff(repo, exit_code=0)

        res = _run_cli(["--fuchsia", str(repo), "raw", "--help", "--format", "json"])
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert data["argv"] == ["--help", "--format", "json"]

    @pytest.mark.parametrize(
        ("bd_exit", "allow_issues", "expected_rc"),
        [
            (0, False, 0),
            (0, True, 0),
            (1, False, 1),
            (1, True, 0),
            (2, False, 2),
            (2, True, 2),
        ],
    )
    def test_exit_code_matrix(
        self,
        tmp_path: Path,
        bd_exit: int,
        allow_issues: bool,
        expected_rc: int,
    ) -> None:
        repo = _init_git_repo(tmp_path / "fx")
        (repo / "a.txt").write_text("hello\n", encoding="utf-8")
        _git(repo, "add", "a.txt")
        _git(repo, "commit", "-q", "-m", "init")
        _install_fake_babeldiff(repo, exit_code=bd_exit, stdout_payload="summary-out\n")

        args = ["--fuchsia", str(repo)]
        if allow_issues:
            args.append("--allow-issues")
        args.extend(["tree", "--summary"])

        res = _run_cli(args)
        assert res.returncode == expected_rc
        assert res.stdout == "summary-out\n"
        assert f"babeldiff exit {bd_exit} (0 clean, 1 issues, 2 error)" in res.stderr

    @pytest.mark.parametrize("allow_issues", [False, True])
    def test_tree_mode_git_diff_failure_returns_2(
        self, tmp_path: Path, allow_issues: bool
    ) -> None:
        # Non-git directory causes `git diff HEAD` to fail with exit 128 even if babeldiff exits 0.
        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()
        _install_fake_babeldiff(not_a_repo, exit_code=0)

        args = ["--fuchsia", str(not_a_repo)]
        if allow_issues:
            args.append("--allow-issues")
        args.extend(["tree", "--summary"])

        res = _run_cli(args, env_extra={"GIT_CEILING_DIRECTORIES": str(tmp_path)})
        assert res.returncode == 2
        assert "git diff HEAD failed" in res.stderr
        assert "babeldiff exit 2 (0 clean, 1 issues, 2 error)" in res.stderr

    def test_tree_mode_unborn_head_returns_2(self, tmp_path: Path) -> None:
        # Git repo with no commits has no HEAD, so `git diff HEAD` fails.
        repo = _init_git_repo(tmp_path / "empty-repo")
        _install_fake_babeldiff(repo, exit_code=0)

        res = _run_cli(["--fuchsia", str(repo), "--allow-issues", "tree"])
        assert res.returncode == 2
        assert "git diff HEAD failed" in res.stderr

    def test_default_fuchsia_resolves_git_toplevel_from_subdir(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path / "fx")
        subdir = repo / "zircon" / "kernel"
        subdir.mkdir(parents=True)
        _install_fake_babeldiff(repo, exit_code=0)

        res = _run_cli(["raw", "--version"], cwd=subdir)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert Path(data["cwd"]).resolve() == repo.resolve()
        assert data["argv"] == ["--version"]

    def test_default_fuchsia_falls_back_to_cwd_outside_git(self, tmp_path: Path) -> None:
        plain_dir = tmp_path / "plain"
        plain_dir.mkdir()
        _install_fake_babeldiff(plain_dir, exit_code=0)

        res = _run_cli(
            ["raw", "--check"],
            cwd=plain_dir,
            env_extra={"GIT_CEILING_DIRECTORIES": str(tmp_path)},
        )
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert Path(data["cwd"]).resolve() == plain_dir.resolve()
        assert data["argv"] == ["--check"]


def test_dev_swarm_zircon_skill_artifacts_token_isolation_and_adapter_contract() -> None:
    core_dir = REAL_REPO / "core" / "dev-swarm-zircon"
    adapter_dir = REAL_REPO / "harnesses" / "agy" / "dev-swarm-zircon"
    adapter_skill = adapter_dir / "SKILL.md"
    core_link = adapter_dir / "core"

    assert SCRIPT_PATH.is_file() and os.access(SCRIPT_PATH, os.X_OK)
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
        "STATE_LEDGER",
        "DECISION_LEDGER",
        "CLEANROOM_BARRIER",
        "CARBON_HIGH",
        "NEON_XHIGH",
        "OPUS_HIGH",
        "<SKILLS_ROOT>",
        "<CORE_ZIRCON_DIR>",
        "<CORE_DEV_SWARM_DIR>",
        "$PROMPTS",
        "researcher.md",
        "cleanroom-commit.md",
        "mktemp -d /tmp/agy-cleanroom.XXXXXX",
        "commit-rev-k",
        "commit-rev-k-p<r>",
        "commit-writer-k",
        "commit-writer-k-p<r>",
        "dev_swarm_zircon_decisions.md",
        "$PROMPTS/state.md",
    ):
        assert required_binding in adapter_text, f"Missing adapter binding: {required_binding!r}"

    # Verify optional Stage 6 Commit-Reviewer is pinned to CARBON_HIGH, never NEON_XHIGH.
    assert "`commit-rev-k` (`CARBON_HIGH`)" in adapter_text
    assert "`commit-rev-k` (`NEON_XHIGH`)" not in adapter_text
    for line in adapter_text.splitlines():
        if "| **`NEON_XHIGH`**" in line:
            assert "Commit-Reviewer" not in line
        if "| **`CARBON_HIGH`**" in line:
            assert "Commit-Reviewer" in line and "Commit-Writer" in line

    assert core_link.is_symlink()
    assert os.readlink(core_link) == "../../../core/dev-swarm-zircon"
    assert core_link.resolve(strict=True) == core_dir.resolve(strict=True)

    link_re = re.compile(r"\[[^\]]+\]\((\.\.?/[^)#?]+)\)")
    for md_path in sorted(core_dir.rglob("*.md")):
        for rel_target in link_re.findall(md_path.read_text(encoding="utf-8")):
            joined = Path(os.path.normpath(str(md_path.parent / rel_target)))
            assert joined.exists(), f"Broken normpath link {rel_target!r} in {md_path}"

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
        core_dir / "doctrine.md",
        core_dir / "personas" / "zircon-coder.md",
        core_dir / "personas" / "zircon-parity-reviewer.md",
        core_dir / "personas" / "zircon-safety-reviewer.md",
        SCRIPT_PATH,
    }
    assert set(scanned_files) == expected_core
    for path in scanned_files:
        text = path.read_text(encoding="utf-8")
        for label, pattern in forbidden_patterns:
            assert pattern.search(text) is None, (
                f"Forbidden harness/model token {label!r} found in {path.relative_to(REAL_REPO)}"
            )
