#!/usr/bin/env python3
"""Bootstrap and invoke babeldiff against a git-backed Fuchsia checkout.

Clones https://github.com/abarth/babeldiff.git into <fuchsia>/local/babeldiff
on first use (or fast-forward pulls on --update), builds it using Fuchsia's
prebuilt Rust toolchain, and runs it in `tree`, `commit`, or `raw` mode.
"""

from __future__ import annotations

import os
import platform
import signal
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

BABELDIFF_REPO_URL = "https://github.com/abarth/babeldiff.git"
BABELDIFF_REL_DIR = Path("local") / "babeldiff"
BABELDIFF_REL_MANIFEST = BABELDIFF_REL_DIR / "Cargo.toml"
BABELDIFF_REL_BIN = BABELDIFF_REL_DIR / "target" / "release" / "babeldiff"

USAGE_TEXT = """\
Usage:
  run_babeldiff.py [--update] [--fuchsia DIR] [--allow-issues] tree [babeldiff_flags...]
  run_babeldiff.py [--update] [--fuchsia DIR] [--allow-issues] commit <rev> [babeldiff_flags...]
  run_babeldiff.py [--update] [--fuchsia DIR] [--allow-issues] raw [babeldiff_args...]

Options (must appear before mode):
  --update        Clone local/babeldiff if missing or fast-forward pull, then rebuild.
  --fuchsia DIR   Fuchsia checkout root (defaults to `git rev-parse --show-toplevel` or cwd).
  --allow-issues  Map babeldiff exit code 1 (issues found) to 0 while keeping exit 2 as error.
  -h, --help      Show this help message and exit.

Modes:
  tree            Stream `git diff HEAD` into `babeldiff patch -C . --base HEAD`.
                  Read-only on the git index; register newly created files first via
                  `git add -N -- <new-files>` so `git diff HEAD` includes them.
  commit <rev>    Run `babeldiff git <rev>` on a commit or revision range.
  raw             Pass all remaining arguments straight through to `babeldiff`.

Typical flags:
  --summary --issues-only               Stage 1: inventory
  --layout stacked --issues-only -U 3   Stage 2: aligned lines
  --format json --issues-only           Stage 3: structured JSON
  --pair 'Cpp::Fn=rust_fn'              Force a function pairing

Exit status:
  0 = clean (or issues found when --allow-issues is set)
  1 = issues found (without --allow-issues)
  2 = error (invalid arguments, build failure, git diff failure, or babeldiff error)
"""


class BabeldiffError(RuntimeError):
    """Error raised when wrapper setup, argument parsing, or subprocess invocation fails."""


@dataclass(frozen=True)
class ParsedArgs:
    """Parsed CLI options and pass-through arguments for a mode."""

    update: bool = False
    fuchsia: str | None = None
    allow_issues: bool = False
    mode: str = ""
    mode_args: tuple[str, ...] = ()
    help_requested: bool = False


def resolve_prebuilt_rust_tag(
    sys_platform_or_uname: str | None = None,
    machine: str | None = None,
) -> str:
    """Map host OS and CPU architecture to Fuchsia's prebuilt Rust directory tag."""
    raw_os = platform.system() if sys_platform_or_uname is None else sys_platform_or_uname
    raw_arch = platform.machine() if machine is None else machine

    os_norm = raw_os.strip().lower()
    if os_norm.startswith("linux"):
        os_tag = "linux"
    elif os_norm in {"darwin", "mac", "macos"}:
        os_tag = "mac"
    else:
        raise ValueError(f"unsupported host OS for prebuilt Rust toolchain: {raw_os!r}")

    arch_norm = raw_arch.strip().lower()
    if arch_norm in {"x86_64", "amd64", "x64"}:
        arch_tag = "x64"
    elif arch_norm in {"aarch64", "arm64"}:
        arch_tag = "arm64"
    else:
        raise ValueError(f"unsupported host architecture for prebuilt Rust toolchain: {raw_arch!r}")

    return f"{os_tag}-{arch_tag}"


def parse_args(argv: Sequence[str]) -> ParsedArgs:
    """Parse leading wrapper options before mode and preserve all mode args verbatim."""
    update = False
    fuchsia: str | None = None
    allow_issues = False

    idx = 0
    total = len(argv)
    while idx < total:
        token = argv[idx]
        if token in {"-h", "--help"}:
            return ParsedArgs(help_requested=True)
        if token == "--update":
            update = True
            idx += 1
        elif token == "--allow-issues":
            allow_issues = True
            idx += 1
        elif token == "--fuchsia":
            if idx + 1 >= total:
                raise BabeldiffError("--fuchsia requires a directory argument")
            fuchsia = argv[idx + 1]
            idx += 2
        elif token.startswith("--fuchsia="):
            fuchsia = token.split("=", 1)[1]
            idx += 1
        elif token == "--":
            idx += 1
            break
        elif token.startswith("-"):
            raise BabeldiffError(f"unknown option before mode: {token}")
        else:
            break

    if idx >= total:
        raise BabeldiffError(
            "missing mode: expected one of `tree`, `commit <rev>`, or `raw` (use --help for usage)"
        )

    mode = argv[idx]
    rest = tuple(argv[idx + 1 :])
    if mode not in {"tree", "commit", "raw"}:
        raise BabeldiffError(f"unknown mode: {mode} (expected `tree`, `commit`, or `raw`)")
    if mode == "commit" and (not rest or not rest[0].strip() or rest[0].startswith("-")):
        raise BabeldiffError("commit mode requires a <rev> argument before any babeldiff flags")

    return ParsedArgs(
        update=update,
        fuchsia=fuchsia,
        allow_issues=allow_issues,
        mode=mode,
        mode_args=rest,
    )


def resolve_fuchsia_dir(explicit: str | None, *, cwd: Path | None = None) -> Path:
    """Resolve the Fuchsia checkout root from --fuchsia, git toplevel, or cwd."""
    base_cwd = Path.cwd() if cwd is None else cwd
    if explicit is not None:
        if not explicit.strip() or "\x00" in explicit:
            raise BabeldiffError(f"invalid --fuchsia path: {explicit!r}")
        candidate = Path(explicit).expanduser()
        if not candidate.is_absolute():
            candidate = base_cwd / candidate
        if not candidate.is_dir():
            raise BabeldiffError(f"Fuchsia directory does not exist: {candidate}")
        return candidate.resolve()

    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=base_cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return base_cwd.resolve()

    top = proc.stdout.strip()
    if proc.returncode == 0 and top:
        return Path(top).resolve()
    return base_cwd.resolve()


def _run_bootstrap_cmd(
    cmd: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    description: str,
) -> None:
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, stdout=sys.stderr, check=False)
    except OSError as exc:
        raise BabeldiffError(f"failed to run {description}: {exc}") from exc
    if proc.returncode != 0:
        raise BabeldiffError(f"{description} failed with exit code {proc.returncode}")


def _resolve_prebuilt_rust_bin(fuchsia_dir: Path, host_tag: str) -> Path:
    """Locate Fuchsia's prebuilt Rust bin dir, falling back to the main checkout for worktrees."""
    prebuilt_rel = Path("prebuilt") / "third_party" / "rust" / host_tag / "bin"
    prebuilt_bin = fuchsia_dir / prebuilt_rel
    if prebuilt_bin.is_dir() or not (fuchsia_dir / ".git").is_file():
        return prebuilt_bin

    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=fuchsia_dir,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return prebuilt_bin

    common_raw = (proc.stdout or "").strip()
    if proc.returncode == 0 and common_raw:
        common_dir = Path(common_raw)
        if not common_dir.is_absolute():
            common_dir = fuchsia_dir / common_dir
        fallback_bin = common_dir.resolve().parent / prebuilt_rel
        if fallback_bin.is_dir():
            return fallback_bin
    return prebuilt_bin


def ensure_babeldiff(fuchsia_dir: Path, *, update: bool = False) -> Path:
    """Ensure local/babeldiff is cloned, updated if requested, and built in release mode."""
    repo_dir = fuchsia_dir / BABELDIFF_REL_DIR
    bin_path = fuchsia_dir / BABELDIFF_REL_BIN
    needs_build = update

    if not (repo_dir / ".git").exists():
        (fuchsia_dir / "local").mkdir(parents=True, exist_ok=True)
        _run_bootstrap_cmd(
            ["git", "clone", BABELDIFF_REPO_URL, BABELDIFF_REL_DIR.as_posix()],
            cwd=fuchsia_dir,
            description="git clone babeldiff",
        )
        needs_build = True
    elif update:
        _run_bootstrap_cmd(
            ["git", "-C", BABELDIFF_REL_DIR.as_posix(), "pull", "--ff-only"],
            cwd=fuchsia_dir,
            description="git pull babeldiff",
        )

    if needs_build or not (bin_path.is_file() and os.access(bin_path, os.X_OK)):
        host_tag = resolve_prebuilt_rust_tag()
        prebuilt_bin = _resolve_prebuilt_rust_bin(fuchsia_dir, host_tag)
        env = os.environ.copy()
        env.pop("CARGO_TARGET_DIR", None)
        env.pop("CARGO_BUILD_TARGET", None)
        existing_path = env.get("PATH", "")
        env["PATH"] = f"{prebuilt_bin}{os.pathsep}{existing_path}" if existing_path else str(
            prebuilt_bin
        )
        _run_bootstrap_cmd(
            [
                "cargo",
                "build",
                "--release",
                "--manifest-path",
                BABELDIFF_REL_MANIFEST.as_posix(),
                "--target-dir",
                (BABELDIFF_REL_DIR / "target").as_posix(),
            ],
            cwd=fuchsia_dir,
            env=env,
            description="cargo build babeldiff",
        )

    return bin_path


def run_tree(fuchsia_dir: Path, bin_path: Path, extra_args: Sequence[str]) -> int:
    """Stream deterministic `git diff HEAD` bytes into `babeldiff patch -C . --base HEAD`.

    Leaves the git index untouched; newly created files must be registered with
    `git add -N -- <files>` beforehand to appear in `git diff HEAD`.
    """
    sigpipe_codes: set[int] = set()
    if hasattr(signal, "SIGPIPE"):
        sigpipe_num = int(signal.SIGPIPE)
        sigpipe_codes = {-sigpipe_num, 128 + sigpipe_num}

    try:
        git_proc = subprocess.Popen(
            [
                "git",
                "--no-pager",
                "diff",
                "--no-ext-diff",
                "--no-color",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                "HEAD",
            ],
            cwd=fuchsia_dir,
            stdout=subprocess.PIPE,
        )
    except OSError as exc:
        raise BabeldiffError(f"failed to execute git diff HEAD: {exc}") from exc

    with git_proc:
        assert git_proc.stdout is not None
        try:
            bd_proc = subprocess.Popen(
                [str(bin_path), "patch", "-C", ".", "--base", "HEAD", *extra_args],
                cwd=fuchsia_dir,
                stdin=git_proc.stdout,
            )
        except OSError as exc:
            git_proc.stdout.close()
            git_proc.terminate()
            raise BabeldiffError(f"failed to execute babeldiff ({bin_path}): {exc}") from exc

        # Close the parent read handle so git receives SIGPIPE if babeldiff exits early.
        git_proc.stdout.close()
        with bd_proc:
            bd_rc = bd_proc.wait()
        git_rc = git_proc.wait()

    if git_rc != 0 and git_rc not in sigpipe_codes:
        print(f"error: git diff HEAD failed with exit code {git_rc}", file=sys.stderr)
        return 2
    return 2 if bd_rc < 0 else bd_rc


def run_commit(fuchsia_dir: Path, bin_path: Path, rev: str, extra_args: Sequence[str]) -> int:
    """Run `babeldiff git <rev>` against a commit or range."""
    return run_raw(fuchsia_dir, bin_path, ("git", rev, *extra_args))


def run_raw(fuchsia_dir: Path, bin_path: Path, extra_args: Sequence[str]) -> int:
    """Pass raw arguments directly through to `babeldiff`."""
    try:
        proc = subprocess.run(
            [str(bin_path), *extra_args],
            cwd=fuchsia_dir,
            check=False,
        )
    except OSError as exc:
        raise BabeldiffError(f"failed to execute babeldiff ({bin_path}): {exc}") from exc
    return 2 if proc.returncode < 0 else proc.returncode


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point returning babeldiff's status (with optional 1 -> 0 mapping)."""
    raw_argv = sys.argv[1:] if argv is None else argv
    try:
        parsed = parse_args(raw_argv)
        if parsed.help_requested:
            print(USAGE_TEXT, end="")
            return 0

        fuchsia_dir = resolve_fuchsia_dir(parsed.fuchsia)
        bin_path = ensure_babeldiff(fuchsia_dir, update=parsed.update)

        if parsed.mode == "tree":
            status = run_tree(fuchsia_dir, bin_path, parsed.mode_args)
        elif parsed.mode == "commit":
            status = run_commit(fuchsia_dir, bin_path, parsed.mode_args[0], parsed.mode_args[1:])
        else:
            status = run_raw(fuchsia_dir, bin_path, parsed.mode_args)
    except (BabeldiffError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"babeldiff exit {status} (0 clean, 1 issues, 2 error)", file=sys.stderr)
    if parsed.allow_issues and status == 1:
        return 0
    return status


if __name__ == "__main__":
    raise SystemExit(main())
