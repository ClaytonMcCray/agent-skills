#!/usr/bin/env python3
"""Manage isolated local git worktrees for parallel or staged development slices."""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

try:
    import fcntl as _fcntl_mod
except ImportError:  # pragma: no cover
    _fcntl_mod = None  # type: ignore[assignment]

_FCNTL: Any = _fcntl_mod

_DEFAULT_TMP_ROOT = Path("/tmp")
_GIT_SUBPROCESS_TIMEOUT_SECONDS = 60
_MAX_RM_OPEN_FDS = 48

_SANITIZED_GIT_ENV_KEYS = frozenset(
    {
        "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
        "GIT_CEILING_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS",
        "GIT_EXTERNAL_DIFF", "GIT_DIFF_OPTS", "GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS",
        "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS", "GIT_NO_LAZY_FETCH",
        "GIT_TERMINAL_PROMPT", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE",
    }
)
_SANITIZED_GIT_ENV_PREFIXES: tuple[str, ...] = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
_IDENTITY_ENV_KEYS: tuple[str, ...] = (
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL",
)
_GIT_BASE_FLAGS: tuple[str, ...] = (
    "--no-pager", "-c", "core.fsmonitor=false", "-c", "core.quotePath=false",
    "-c", "core.splitIndex=false", "-c", "commit.gpgsign=false", "-c", "tag.gpgSign=false",
    "-c", f"core.hooksPath={os.devnull}", "-c", "submodule.recurse=false",
    "-c", "diff.submodule=short", "-c", "color.ui=false", "-c", "log.showSignature=false",
    "-c", "protocol.file.allow=never",
)
_CONTROL_CHAR_RE = re.compile(
    r"[\x00-\x08\x0a-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)
_MULTILINE_CTRL_RE = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
_GIT_VERSION_RE = re.compile(r"(\d+)\.(\d+)")
_ACTIVE_GIT_OP_FILES: tuple[str, ...] = (
    "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply",
)
_O_NOFOLLOW = int(getattr(os, "O_NOFOLLOW", 0))
_O_DIRECTORY = int(getattr(os, "O_DIRECTORY", 0))
_O_NONBLOCK = int(getattr(os, "O_NONBLOCK", 0))
_O_NOCTTY = int(getattr(os, "O_NOCTTY", 0))
_O_SEARCH = int(getattr(os, "O_SEARCH", 0))
_O_SAFE_OPEN = _O_NOFOLLOW | _O_NONBLOCK | _O_NOCTTY


class _WorktreeError(Exception):
    """Domain error reported cleanly on stderr with exit code 1."""


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class RepoInfo:
    root: Path
    common_dir: Path


@dataclass(frozen=True)
class LinkedWorktree:
    resolved_path: Path | None
    branch_ref: str | None
    locked: bool


def _raise_oserror(exc: OSError) -> None:
    raise exc


def _escape_ctrl(match: re.Match[str]) -> str:
    ch = match.group(0)
    if ch == "\n":
        return "\\n"
    if ch == "\r":
        return "\\r"
    code = ord(ch)
    return f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}"


def _display_path(raw: str) -> str:
    """Render a path or label as single-line UTF-8 with control and BiDi chars escaped."""
    safe = raw.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="backslashreplace")
    return _CONTROL_CHAR_RE.sub(_escape_ctrl, safe)


def _sanitize_multiline_output(raw: str) -> str:
    """Escape control, C1, and BiDi characters while preserving LF newlines and tabs."""
    safe = raw.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="backslashreplace")
    return _MULTILINE_CTRL_RE.sub(_escape_ctrl, safe)


def _write_stream_safe(stream: TextIO, text: str, *, redirect_broken_pipe_fd: bool) -> None:
    """Write text safely on narrow locales/closed pipes, optionally redirecting fd on EPIPE."""
    try:
        try:
            stream.write(text)
            stream.flush()
        except UnicodeEncodeError:
            encoding = stream.encoding or "ascii"
            safe_text = text.encode(encoding, errors="backslashreplace").decode(
                encoding, errors="replace"
            )
            stream.write(safe_text)
            stream.flush()
    except BrokenPipeError:
        if not redirect_broken_pipe_fd:
            return
        try:
            out_fd = stream.fileno()
        except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
            return
        with contextlib.suppress(OSError):
            devnull_fd = os.open(os.devnull, os.O_WRONLY)
            try:
                if devnull_fd != out_fd:
                    os.dup2(devnull_fd, out_fd)
            finally:
                if devnull_fd != out_fd:
                    os.close(devnull_fd)


def _write_stdout_safe(text: str, *, redirect_broken_pipe_fd: bool = False) -> None:
    """Write output to stdout with narrow-locale encoding and broken-pipe protection."""
    _write_stream_safe(sys.stdout, text, redirect_broken_pipe_fd=redirect_broken_pipe_fd)


def _safe_resolve(path: Path, *, strict: bool = False) -> Path | None:
    try:
        return path.resolve(strict=strict)
    except (OSError, RuntimeError, ValueError):
        return None


def _canonicalize_existing_path(path: Path) -> Path | None:
    """Resolve symlinks and recover true on-disk casing on case-insensitive filesystems."""
    resolved = _safe_resolve(path, strict=True)
    if resolved is None:
        return None
    if sys.platform == "darwin" and _FCNTL is not None:
        f_getpath = getattr(_FCNTL, "F_GETPATH", 50)
        if isinstance(f_getpath, int):
            try:
                st = os.stat(resolved)
                fd = -1
                if stat.S_ISDIR(st.st_mode):
                    try:
                        fd = os.open(str(resolved), (_O_SEARCH or os.O_RDONLY) | _O_DIRECTORY)
                    except OSError:
                        fd = os.open(str(resolved), os.O_RDONLY | _O_DIRECTORY)
                elif stat.S_ISREG(st.st_mode):
                    fd = os.open(str(resolved), os.O_RDONLY | _O_NONBLOCK | _O_NOCTTY)
                if fd >= 0:
                    try:
                        buf = _FCNTL.fcntl(fd, f_getpath, bytes(1024))
                    finally:
                        os.close(fd)
                    if isinstance(buf, (bytes, bytearray)):
                        raw = bytes(buf).split(b"\x00", 1)[0]
                        if raw:
                            return Path(os.fsdecode(raw))
            except (OSError, ValueError):
                pass
    return resolved


def _resolve_possibly_missing(path: Path) -> Path | None:
    """Resolve symlinks before '..' and canonicalize casing on the deepest existing ancestor."""
    existing = _canonicalize_existing_path(path)
    if existing is not None:
        return existing
    resolved = _safe_resolve(path, strict=False)
    if resolved is None:
        return None
    cur = resolved
    tail: list[str] = []
    while True:
        canon = _canonicalize_existing_path(cur)
        if canon is not None:
            for part in reversed(tail):
                canon = canon / part
            return canon
        if cur.parent == cur:
            return resolved
        tail.append(cur.name)
        cur = cur.parent


def _stat_identity(path: Path) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
        return (st.st_dev, st.st_ino)
    except (OSError, ValueError):
        return None


def _same_path(path_a: Path, path_b: Path) -> bool:
    if path_a == path_b:
        return True
    id_a = _stat_identity(path_a)
    return id_a is not None and id_a == _stat_identity(path_b)


def _is_same_or_descendant(child: Path, ancestor: Path) -> bool:
    if child == ancestor or child.is_relative_to(ancestor):
        return True
    anc_id = _stat_identity(ancestor)
    if anc_id is None:
        return False
    if _stat_identity(child) == anc_id:
        return True
    return any(_stat_identity(parent) == anc_id for parent in child.parents)


def _paths_overlap(path_a: Path, path_b: Path) -> bool:
    return _is_same_or_descendant(path_a, path_b) or _is_same_or_descendant(path_b, path_a)


def _path_exists_or_symlink(path: Path) -> bool:
    try:
        os.lstat(path)
        return True
    except (OSError, ValueError):
        return False


def _require_token(value: str, label: str) -> None:
    if not value or "\x00" in value or value.startswith("-"):
        raise _WorktreeError(f"invalid {label}: {_display_path(value)!r}")


def _validate_dir_ancestors(target_dir: Path, label: str) -> None:
    """Verify every existing ancestor down to target_dir is a directory or symlink to one."""
    for comp in (*reversed(target_dir.parents), target_dir):
        try:
            st = os.lstat(comp)
        except FileNotFoundError:
            break
        except (OSError, ValueError) as exc:
            raise _WorktreeError(f"invalid {label} (inaccessible ancestor: {exc})") from exc
        if stat.S_ISLNK(st.st_mode):
            resolved = _safe_resolve(comp, strict=True)
            if resolved is None or not resolved.is_dir():
                raise _WorktreeError(f"invalid {label} (bad symlink: {_display_path(str(comp))})")
        elif not stat.S_ISDIR(st.st_mode):
            raise _WorktreeError(f"invalid {label} (non-dir ancestor: {_display_path(str(comp))})")


def _open_owned_dir_fd(
    path: str | Path,
    *,
    dir_fd: int | None = None,
    expected_id: tuple[int, int] | None = None,
    expected_dev: int | None = None,
    chmod_rwx: bool = False,
) -> tuple[int, os.stat_result]:
    """Open a non-symlink dir FD and verify ownership, device, optional inode, and mode."""
    fd = os.open(str(path), os.O_RDONLY | _O_NOFOLLOW | _O_DIRECTORY, dir_fd=dir_fd)
    try:
        fst = os.fstat(fd)
        if not stat.S_ISDIR(fst.st_mode):
            raise OSError(f"expected directory: {path}")
        if expected_id is not None and (fst.st_dev, fst.st_ino) != expected_id:
            raise OSError(f"directory inode changed concurrently: {path}")
        if expected_dev is not None and fst.st_dev != expected_dev:
            raise OSError(f"refusing to cross filesystem boundary: {path}")
        if hasattr(os, "geteuid") and fst.st_uid != os.geteuid():
            raise OSError(f"directory not owned by current user: {path}")
        if chmod_rwx:
            with contextlib.suppress(OSError):
                os.fchmod(fd, 0o700)
        return (fd, fst)
    except BaseException:
        os.close(fd)
        raise


@contextlib.contextmanager
def _owned_dir_fd(
    path: str | Path,
    *,
    dir_fd: int | None = None,
    expected_id: tuple[int, int] | None = None,
    chmod_rwx: bool = False,
) -> Iterator[tuple[int, os.stat_result]]:
    fd, fst = _open_owned_dir_fd(
        path, dir_fd=dir_fd, expected_id=expected_id, chmod_rwx=chmod_rwx
    )
    try:
        yield (fd, fst)
    finally:
        os.close(fd)


@dataclass
class _RmFrame:
    rel_parts: tuple[str, ...]
    fd: int | None
    entries: list[str]
    idx: int = 0


def _reopen_frame_fd(stack: list[_RmFrame], frame_idx: int, root_fd: int, root_dev: int) -> int:
    """Re-open an evicted stack frame FD by stepping downward from the deepest open ancestor."""
    frame = stack[frame_idx]
    if frame.fd is not None:
        return frame.fd
    anc_idx = frame_idx - 1
    while anc_idx >= 0 and stack[anc_idx].fd is None:
        anc_idx -= 1
    cur_fd = root_fd if anc_idx < 0 else stack[anc_idx].fd
    assert cur_fd is not None
    start_depth = 0 if anc_idx < 0 else len(stack[anc_idx].rel_parts)
    owned_cur = False
    try:
        for part in frame.rel_parts[start_depth:]:
            nxt_fd, _ = _open_owned_dir_fd(part, dir_fd=cur_fd, expected_dev=root_dev)
            prev_fd = cur_fd if owned_cur else None
            cur_fd = nxt_fd
            owned_cur = True
            if prev_fd is not None:
                os.close(prev_fd)
    except BaseException:
        if owned_cur:
            os.close(cur_fd)
        raise
    frame.fd = cur_fd
    return cur_fd


def _evict_oldest_frame_fd(stack: list[_RmFrame]) -> None:
    """Close the oldest open non-root frame FD to keep open FDs within _MAX_RM_OPEN_FDS."""
    open_frames = [f for f in stack[1:] if f.fd is not None]
    if len(open_frames) >= _MAX_RM_OPEN_FDS:
        victim = open_frames[0]
        victim_fd = victim.fd
        victim.fd = None
        if victim_fd is not None:
            os.close(victim_fd)


def _rmtree_owned_dir(root: Path, *, expected_id: tuple[int, int] | None = None) -> None:
    """Iteratively remove an owned directory tree using a bounded window of dir_fds."""
    parent_fd = os.open(str(root.parent), os.O_RDONLY | _O_DIRECTORY)
    try:
        with _owned_dir_fd(
            root.name, dir_fd=parent_fd, expected_id=expected_id, chmod_rwx=True
        ) as (root_fd, root_st):
            root_dev = root_st.st_dev
            stack: list[_RmFrame] = [_RmFrame((), root_fd, os.listdir(root_fd))]
            try:
                while stack:
                    top = stack[-1]
                    if top.idx >= len(top.entries):
                        if top.rel_parts and top.fd is not None:
                            top_fd = top.fd
                            top.fd = None
                            os.close(top_fd)
                        stack.pop()
                        if top.rel_parts:
                            p_fd = _reopen_frame_fd(stack, len(stack) - 1, root_fd, root_dev)
                            os.rmdir(top.rel_parts[-1], dir_fd=p_fd)
                        continue
                    cur_fd = _reopen_frame_fd(stack, len(stack) - 1, root_fd, root_dev)
                    name = top.entries[top.idx]
                    top.idx += 1
                    cst = os.lstat(name, dir_fd=cur_fd)
                    if stat.S_ISDIR(cst.st_mode) and not stat.S_ISLNK(cst.st_mode):
                        _evict_oldest_frame_fd(stack)
                        cur_fd = _reopen_frame_fd(stack, len(stack) - 1, root_fd, root_dev)
                        child_fd, _ = _open_owned_dir_fd(
                            name, dir_fd=cur_fd, expected_dev=root_dev, chmod_rwx=True
                        )
                        try:
                            child_entries = os.listdir(child_fd)
                        except BaseException:
                            os.close(child_fd)
                            raise
                        stack.append(_RmFrame((*top.rel_parts, name), child_fd, child_entries))
                    else:
                        os.unlink(name, dir_fd=cur_fd)
            finally:
                for frame in stack[1:]:
                    if frame.fd is not None:
                        frame_fd = frame.fd
                        frame.fd = None
                        with contextlib.suppress(OSError):
                            os.close(frame_fd)
        os.rmdir(root.name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _git_env(extra_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a sanitized git environment while preserving non-blank user identity."""
    env = {
        k: v for k, v in os.environ.items()
        if k not in _SANITIZED_GIT_ENV_KEYS and not k.startswith(_SANITIZED_GIT_ENV_PREFIXES)
    }
    for id_key in _IDENTITY_ENV_KEYS:
        if id_key in env and not env[id_key].strip():
            env.pop(id_key, None)
    env.update(
        GIT_OPTIONAL_LOCKS="0", GIT_LITERAL_PATHSPECS="1", GIT_NO_LAZY_FETCH="1",
        GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1",
    )
    if extra_env:
        env.update(extra_env)
    return env


def _kill_proc_group(proc: subprocess.Popen[bytes], sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, OSError):
        with contextlib.suppress(ProcessLookupError, OSError):
            (proc.kill if sig == signal.SIGKILL else proc.terminate)()


def _terminate_proc_group(proc: subprocess.Popen[bytes]) -> None:
    """Terminate a git process group with SIGTERM, escalating to SIGKILL if needed."""
    if proc.returncode is not None:
        return
    _kill_proc_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        _kill_proc_group(proc, signal.SIGKILL)
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            proc.wait(timeout=2)
        return
    pgid_alive = True
    try:
        deadline = time.monotonic() + 0.2
        while True:
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                pgid_alive = False
                return
            except OSError:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
    finally:
        if pgid_alive:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(proc.pid, signal.SIGKILL)


def _run_git(
    cwd: Path, args: Sequence[str], *, extra_env: Mapping[str, str] | None = None
) -> GitResult:
    """Run git in its own process group with hooks, GPG, fsmonitor, submodules, and pagers off."""
    try:
        proc = subprocess.Popen(
            ["git", *_GIT_BASE_FLAGS, *args],
            cwd=str(cwd), env=_git_env(extra_env), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return GitResult(127, b"", str(exc).encode("utf-8", errors="surrogateescape"))

    try:
        stdout, stderr = proc.communicate(timeout=_GIT_SUBPROCESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _terminate_proc_group(proc)
        msg = f"git command timed out after {_GIT_SUBPROCESS_TIMEOUT_SECONDS}s"
        return GitResult(124, b"", msg.encode("utf-8"))
    except BaseException:
        _terminate_proc_group(proc)
        raise
    finally:
        if proc.stdout is not None:
            with contextlib.suppress(OSError):
                proc.stdout.close()
        if proc.stderr is not None:
            with contextlib.suppress(OSError):
                proc.stderr.close()
        if proc.returncode is None:
            _kill_proc_group(proc, signal.SIGKILL)
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                proc.wait(timeout=2)
    return GitResult(proc.returncode, stdout, stderr)


def _git_error_text(res: GitResult) -> str:
    raw = res.stderr if res.stderr.strip() else res.stdout
    return _display_path(raw.decode("utf-8", errors="replace").strip())


def _require_git(
    cwd: Path,
    args: Sequence[str],
    context: str,
    *,
    extra_env: Mapping[str, str] | None = None,
) -> GitResult:
    res = _run_git(cwd, args, extra_env=extra_env)
    if res.returncode != 0:
        raise _WorktreeError(f"{context}: {_git_error_text(res)}")
    return res


def _git_version(cwd: Path) -> tuple[int, int]:
    res = _run_git(cwd, ["version"])
    if res.returncode != 0:
        return (0, 0)
    match = _GIT_VERSION_RE.search(res.stdout.decode("utf-8", errors="replace"))
    return (int(match.group(1)), int(match.group(2))) if match is not None else (0, 0)


def _identity_args(cwd: Path) -> tuple[str, ...]:
    """Return fallback user.name/user.email -c overrides when git lacks a configured identity."""
    if (
        _run_git(cwd, ["var", "GIT_AUTHOR_IDENT"]).returncode == 0
        and _run_git(cwd, ["var", "GIT_COMMITTER_IDENT"]).returncode == 0
    ):
        return ()
    return ("-c", "user.name=dev-swarm", "-c", "user.email=dev-swarm@localhost")


def _rev_parse_object(cwd: Path, expr: str) -> str | None:
    res = _run_git(cwd, ["rev-parse", "--verify", "--quiet", expr])
    return res.stdout.decode("ascii", errors="replace").strip() if res.returncode == 0 else None


def _rev_parse_path(cwd: Path, *flags: str, must_be_dir: bool = False) -> Path | None:
    res = _run_git(cwd, ["rev-parse", *flags])
    if res.returncode != 0 or not res.stdout.strip():
        return None
    cand = Path(os.fsdecode(res.stdout.rstrip(b"\r\n")))
    if not cand.is_absolute():
        cand = cwd / cand
    if not must_be_dir:
        return cand
    resolved = _canonicalize_existing_path(cand)
    return resolved if resolved is not None and resolved.is_dir() else None


def _ref_exists(cwd: Path, full_ref: str) -> bool:
    return _run_git(cwd, ["show-ref", "--verify", "--quiet", full_ref]).returncode == 0


def _current_branch(cwd: Path) -> str:
    res = _run_git(cwd, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    return os.fsdecode(res.stdout.rstrip(b"\r\n")) if res.returncode == 0 else ""


def _has_unmerged(cwd: Path) -> bool:
    res = _run_git(cwd, ["ls-files", "-u"])
    return res.returncode != 0 or bool(res.stdout.strip())


def _assert_no_active_git_op(cwd: Path, label: str) -> None:
    for state_name in _ACTIVE_GIT_OP_FILES:
        state_path = _rev_parse_path(cwd, "--git-path", state_name)
        if state_path is not None and _path_exists_or_symlink(state_path):
            raise _WorktreeError(f"{label} has an in-progress git operation ({state_name})")


def _detect_git_worktree(start_dir_res: Path) -> RepoInfo | None:
    """Validate that start_dir_res lies cleanly inside a git worktree (defeating core.worktree)."""
    wt = _run_git(start_dir_res, ["rev-parse", "--is-inside-work-tree"])
    if wt.returncode != 0 or wt.stdout.strip() != b"true":
        return None
    top = _rev_parse_path(start_dir_res, "--show-toplevel", must_be_dir=True)
    git_dir = _rev_parse_path(start_dir_res, "--absolute-git-dir", must_be_dir=True)
    common = _rev_parse_path(start_dir_res, "--git-common-dir", must_be_dir=True)
    if (
        top is None or git_dir is None or common is None
        or not _is_same_or_descendant(start_dir_res, top)
    ):
        return None
    if not _same_path(top, start_dir_res):
        tw = _run_git(top, ["rev-parse", "--is-inside-work-tree"])
        top_top = _rev_parse_path(top, "--show-toplevel", must_be_dir=True)
        top_git = _rev_parse_path(top, "--absolute-git-dir", must_be_dir=True)
        if (
            tw.returncode != 0 or tw.stdout.strip() != b"true"
            or top_top is None or top_git is None
            or not _same_path(top_top, top) or not _same_path(top_git, git_dir)
        ):
            return None
    return RepoInfo(root=top, common_dir=common)


def _safe_cwd() -> Path:
    try:
        return Path.cwd()
    except OSError as exc:
        raise _WorktreeError(f"failed to resolve current working directory: {exc}") from exc


def _validate_start_repo(raw_repo: str | None) -> RepoInfo:
    if raw_repo is not None:
        if not raw_repo or "\x00" in raw_repo:
            raise _WorktreeError(f"--repo requires a valid directory: {_display_path(raw_repo)!r}")
        start = _canonicalize_existing_path(Path(raw_repo))
        if start is None or not start.is_dir():
            raise _WorktreeError(f"--repo directory is invalid: {_display_path(raw_repo)}")
    else:
        start = _canonicalize_existing_path(_safe_cwd())
        if start is None:
            raise _WorktreeError("failed to resolve current working directory")
    repo = _detect_git_worktree(start)
    if repo is None:
        raise _WorktreeError(f"not inside a valid git working tree: {_display_path(str(start))}")
    return repo


def _resolve_existing_wt_dir(wt_path_arg: str) -> tuple[Path, str]:
    _require_token(wt_path_arg, "worktree path")
    disp = _display_path(wt_path_arg)
    wt_res = _canonicalize_existing_path(Path(wt_path_arg))
    if wt_res is None or not wt_res.is_dir():
        raise _WorktreeError(f"worktree directory does not exist: {disp}")
    return (wt_res, disp)


def _is_broad_base(candidate: Path) -> bool:
    """Return True if candidate is a broad system/home/temp root unsafe as a worktree base."""
    broad: list[Path] = [Path("/")]
    for raw_root in (_DEFAULT_TMP_ROOT, Path(tempfile.gettempdir())):
        canon = _canonicalize_existing_path(raw_root)
        broad.append(canon if canon is not None else raw_root)
    with contextlib.suppress(RuntimeError, OSError):
        home_raw = Path.home()
        home_res = _canonicalize_existing_path(home_raw) or _resolve_possibly_missing(home_raw)
        if home_res is not None:
            broad.append(home_res)
            broad.extend(home_res.parents)
    return any(_same_path(candidate, bad) for bad in broad)


def _reject_dotdot_after_missing(
    abs_path: Path, disp: str, label: str = "worktree base"
) -> None:
    """Reject '..' traversal after any missing path component to block symlink bypasses."""
    cur = Path(abs_path.anchor)
    saw_missing = False
    for part in abs_path.parts[1:]:
        if part == "..":
            if saw_missing:
                raise _WorktreeError(f"{label} traverses '..' after missing dir: {disp}")
            cur = cur / part
        else:
            cur = cur / part
            if not saw_missing and not _path_exists_or_symlink(cur):
                saw_missing = True


def _resolve_wt_base(
    repo: RepoInfo, raw_wt_base: str | None, *, create: bool
) -> tuple[Path, bool]:
    """Resolve and validate the worktree base directory, returning (canonical_base, explicit)."""
    explicit = raw_wt_base is not None
    if explicit:
        assert raw_wt_base is not None
        _require_token(raw_wt_base, "--wt-base path")
        wt_base = Path(raw_wt_base.rstrip("/") or raw_wt_base)
        if not wt_base.is_absolute():
            wt_base = _safe_cwd() / wt_base
    else:
        safe_repo = _SAFE_NAME_RE.sub("-", repo.root.name).strip("-") or "repo"
        tmp_root = (
            _DEFAULT_TMP_ROOT
            if _DEFAULT_TMP_ROOT.is_dir() and os.access(_DEFAULT_TMP_ROOT, os.W_OK | os.X_OK)
            else Path(tempfile.gettempdir())
        )
        wt_base = tmp_root / f"agy-wt-{safe_repo}"

    disp = _display_path(str(wt_base))
    if wt_base.name in ("", ".", ".."):
        raise _WorktreeError(f"invalid worktree base path: {disp}")
    _reject_dotdot_after_missing(wt_base, disp)
    _validate_dir_ancestors(wt_base.parent, "worktree base")

    st: os.stat_result | None = None
    try:
        st = os.lstat(wt_base)
    except FileNotFoundError:
        parent_res = _resolve_possibly_missing(wt_base.parent)
        if parent_res is not None:
            with contextlib.suppress(FileNotFoundError, OSError):
                st = os.lstat(parent_res / wt_base.name)
    except (OSError, ValueError) as exc:
        raise _WorktreeError(f"failed to inspect worktree base {disp}: {exc}") from exc
    if st is not None and (stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode)):
        raise _WorktreeError(f"worktree base must be a real dir, not symlink/file: {disp}")

    prospective = _resolve_possibly_missing(wt_base)
    if prospective is None or _is_broad_base(prospective) or _paths_overlap(prospective, repo.root):
        raise _WorktreeError(f"worktree base is unsafe or overlaps repository root: {disp}")

    if st is None:
        if not create:
            return (prospective, explicit)
        try:
            if explicit:
                prospective.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(FileExistsError):
                os.mkdir(prospective, 0o700)
            st = os.lstat(prospective)
        except OSError as exc:
            raise _WorktreeError(f"failed to create worktree base {disp}: {exc}") from exc
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise _WorktreeError(f"worktree base must be a real dir, not symlink/file: {disp}")

    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        raise _WorktreeError(f"worktree base is not owned by current user: {disp}")
    mode = stat.S_IMODE(st.st_mode)
    if explicit and (mode & 0o002):
        raise _WorktreeError(f"worktree base must not be world-writable: {disp}")
    if not explicit and (mode & 0o077):
        try:
            with _owned_dir_fd(
                prospective, expected_id=(st.st_dev, st.st_ino)
            ) as (base_fd, _base_st):
                os.fchmod(base_fd, 0o700)
        except OSError as exc:
            raise _WorktreeError(f"failed to secure default worktree base {disp}: {exc}") from exc

    wt_base_res = _canonicalize_existing_path(prospective)
    if wt_base_res is None or _is_broad_base(wt_base_res) or _paths_overlap(wt_base_res, repo.root):
        raise _WorktreeError(f"worktree base is unsafe or overlaps repository root: {disp}")
    return (wt_base_res, explicit)


def _parse_porcelain_stanzas(
    raw_stanzas: Sequence[Sequence[bytes]],
) -> tuple[Path | None, str | None, list[LinkedWorktree]]:
    primary_root: Path | None = None
    primary_branch: str | None = None
    linked: list[LinkedWorktree] = []
    stanza_idx = 0
    for fields in raw_stanzas:
        raw_p: Path | None = None
        branch_ref: str | None = None
        locked = False
        for field in fields:
            if field.startswith(b"worktree "):
                raw_p = Path(os.fsdecode(field[9:]))
            elif field.startswith(b"branch "):
                branch_ref = os.fsdecode(field[7:])
            elif field == b"locked" or field.startswith(b"locked "):
                locked = True
        if raw_p is None:
            continue
        resolved = _resolve_possibly_missing(raw_p)
        if stanza_idx == 0:
            primary_root = resolved
            primary_branch = branch_ref
        else:
            linked.append(LinkedWorktree(resolved, branch_ref, locked))
        stanza_idx += 1
    return (primary_root, primary_branch, linked)


def _parse_linked_worktrees(
    repo_root: Path,
) -> tuple[Path | None, str | None, list[LinkedWorktree]]:
    """Parse git worktree list --porcelain (with -z on Git >= 2.36 and line fallback)."""
    if _git_version(repo_root) >= (2, 36):
        res = _run_git(repo_root, ["worktree", "list", "--porcelain", "-z"])
        if res.returncode == 0:
            return _parse_porcelain_stanzas(
                [b.split(b"\x00") for b in res.stdout.split(b"\x00\x00") if b]
            )
        if res.returncode != 129:
            raise _WorktreeError(f"failed to list git worktrees: {_git_error_text(res)}")
    res = _require_git(
        repo_root, ["worktree", "list", "--porcelain"], "failed to list git worktrees"
    )
    stanzas = [
        [ln.rstrip(b"\r") for ln in blk.splitlines() if ln.rstrip(b"\r")]
        for blk in res.stdout.replace(b"\r\n", b"\n").split(b"\n\n")
        if blk.strip()
    ]
    return _parse_porcelain_stanzas(stanzas)


def _find_linked_worktree(
    target_res: Path, linked: Sequence[LinkedWorktree]
) -> LinkedWorktree | None:
    for entry in linked:
        if entry.resolved_path is not None and _same_path(entry.resolved_path, target_res):
            return entry
    return None


def _is_in_wt_base(candidate_res: Path, wt_base_res: Path) -> bool:
    return (not _same_path(candidate_res, wt_base_res)) and _same_path(
        candidate_res.parent, wt_base_res
    )


def _linked_worktree_allowed(
    candidate_res: Path, entry: LinkedWorktree, wt_base_res: Path, explicit_wt_base: bool
) -> bool:
    if entry.branch_ref is not None and not entry.branch_ref.startswith("refs/heads/dev-swarm/"):
        return False
    if _is_in_wt_base(candidate_res, wt_base_res):
        return True
    return (not explicit_wt_base) and (entry.branch_ref or "").startswith("refs/heads/dev-swarm/")


def _verify_orphan_dot_git(wt_res: Path, repo_common_dir: Path) -> bool:
    """Fail-closed scan rejecting nested .git entries; returns True if valid top-level .git seen."""
    worktrees_root = repo_common_dir / "worktrees"
    top_dot_gits: list[Path] = []
    try:
        for dirpath_str, dirnames, filenames in os.walk(
            wt_res, onerror=_raise_oserror, followlinks=False
        ):
            dirpath = Path(dirpath_str)
            is_top = _same_path(dirpath, wt_res)
            for name in (*dirnames, *filenames):
                if name.lower() != ".git":
                    continue
                if not is_top:
                    raise _WorktreeError("refusing to remove directory with nested .git entry")
                top_dot_gits.append(dirpath / name)
    except _WorktreeError:
        raise
    except (OSError, RecursionError) as exc:
        raise _WorktreeError(f"failed to inspect worktree directory: {exc}") from exc

    for dot_git in top_dot_gits:
        try:
            fd = os.open(str(dot_git), os.O_RDONLY | _O_SAFE_OPEN)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise _WorktreeError("refusing to remove dir with non-worktree .git entry")
                raw_bytes = os.read(fd, 4097)
            finally:
                os.close(fd)
        except _WorktreeError:
            raise
        except OSError as exc:
            raise _WorktreeError(f"failed to inspect {dot_git.name}: {exc}") from exc
        lines = raw_bytes.splitlines()
        first = lines[0].decode("utf-8", errors="surrogateescape") if lines else ""
        if len(raw_bytes) > 4096 or b"\x00" in raw_bytes or not first.startswith("gitdir:"):
            raise _WorktreeError("refusing to remove directory with invalid .git file")
        raw_target = first[7:].strip()
        if not raw_target:
            raise _WorktreeError("refusing to remove directory with invalid .git file")
        tpath = Path(raw_target)
        tres = _resolve_possibly_missing(tpath if tpath.is_absolute() else wt_res / tpath)
        common_res = _rev_parse_path(wt_res, "--git-common-dir", must_be_dir=True)
        if (
            tres is None
            or _same_path(tres, worktrees_root)
            or not _same_path(tres.parent, worktrees_root)
            or (common_res is not None and not _same_path(common_res, repo_common_dir))
        ):
            raise _WorktreeError("refusing to remove worktree belonging to another repository")
    return bool(top_dot_gits)


def _diff_tree_filter(
    repo_root: Path, diff_filter: str, base_obj: str, target_obj: str
) -> list[str]:
    res = _require_git(
        repo_root,
        ["diff-tree", "-r", "-z", "--no-renames", "--name-only",
         f"--diff-filter={diff_filter}", base_obj, target_obj],
        "failed to inspect changed paths",
    )
    return [os.fsdecode(p) for p in res.stdout.split(b"\x00") if p]


def _fold_rel(rel: str) -> str:
    """Normalize and casefold a relative path for case-insensitive filesystem identity checks."""
    return unicodedata.normalize("NFD", rel).casefold()


def _collect_deleted_meta(
    repo_root: Path, deleted: set[str]
) -> tuple[dict[tuple[int, int], set[str]], set[str], dict[tuple[int, int], set[str]]]:
    """Collect inode->folded-path maps for deleted tracked entries and their real ancestor dirs."""
    deleted_file_ids: dict[tuple[int, int], set[str]] = {}
    deleted_dirs: set[str] = set()
    deleted_dir_ids: dict[tuple[int, int], set[str]] = {}
    for d_rel in deleted:
        parts = Path(d_rel).parts
        blocked = False
        for depth in range(1, len(parts)):
            anc_rel = "/".join(parts[:depth])
            try:
                ast = os.lstat(repo_root.joinpath(*parts[:depth]))
            except OSError:
                blocked = True
                break
            if not stat.S_ISDIR(ast.st_mode):
                blocked = True
                break
            deleted_dirs.add(anc_rel)
            deleted_dir_ids.setdefault((ast.st_dev, ast.st_ino), set()).add(_fold_rel(anc_rel))
        if blocked:
            continue
        with contextlib.suppress(OSError):
            dst = os.lstat(repo_root / d_rel)
            if not stat.S_ISDIR(dst.st_mode):
                deleted_file_ids.setdefault((dst.st_dev, dst.st_ino), set()).add(_fold_rel(d_rel))
    return (deleted_file_ids, deleted_dirs, deleted_dir_ids)


def _is_deleted_match(
    rel: str,
    st: os.stat_result,
    exact: set[str],
    id_map: Mapping[tuple[int, int], set[str]],
    *,
    icase: bool,
) -> bool:
    return rel in exact or (icase and _fold_rel(rel) in id_map.get((st.st_dev, st.st_ino), ()))


def _check_added_path_collisions(
    repo_root: Path, added_rel_paths: Sequence[str], deleted: set[str]
) -> None:
    """Fail if any added path collides with untracked/ignored disk entries, allowing refactors."""
    if not added_rel_paths:
        return
    icase = _same_path(repo_root / ".git", repo_root / ".GIT")
    deleted_file_ids, deleted_dirs, deleted_dir_ids = _collect_deleted_meta(repo_root, deleted)
    for rel in added_rel_paths:
        if not rel:
            continue
        parts = Path(rel).parts
        prefix_exempt = False
        for depth in range(1, len(parts)):
            prefix_rel = "/".join(parts[:depth])
            try:
                pst = os.lstat(repo_root.joinpath(*parts[:depth]))
            except FileNotFoundError:
                break
            except OSError as exc:
                raise _WorktreeError(f"failed to inspect {_display_path(rel)}: {exc}") from exc
            if not stat.S_ISDIR(pst.st_mode):
                if _is_deleted_match(prefix_rel, pst, deleted, deleted_file_ids, icase=icase):
                    prefix_exempt = True
                    break
                raise _WorktreeError(
                    f"refusing to overwrite non-directory path blocking {_display_path(rel)}"
                )
        if prefix_exempt:
            continue
        target = repo_root / rel
        try:
            tst = os.lstat(target)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _WorktreeError(f"failed to inspect {_display_path(rel)}: {exc}") from exc
        if stat.S_ISDIR(tst.st_mode):
            if not _is_deleted_match(rel, tst, deleted_dirs, deleted_dir_ids, icase=icase):
                raise _WorktreeError(
                    f"refusing to overwrite untracked/ignored directory: {_display_path(rel)}"
                )
            try:
                for dirpath_str, dirnames, filenames in os.walk(
                    target, onerror=_raise_oserror, followlinks=False
                ):
                    dirpath = Path(dirpath_str)
                    real_subdirs: list[str] = []
                    entries = (
                        *((d, True) for d in dirnames),
                        *((f, False) for f in filenames),
                    )
                    for ename, is_dir in entries:
                        child = dirpath / ename
                        cst = os.lstat(child)
                        crel = child.relative_to(repo_root).as_posix()
                        if is_dir and stat.S_ISDIR(cst.st_mode):
                            if not _is_deleted_match(
                                crel, cst, deleted_dirs, deleted_dir_ids, icase=icase
                            ):
                                raise _WorktreeError(
                                    f"refusing to overwrite untracked/ignored directory in "
                                    f"{_display_path(rel)}: {_display_path(crel)}"
                                )
                            real_subdirs.append(ename)
                        elif not _is_deleted_match(
                            crel, cst, deleted, deleted_file_ids, icase=icase
                        ):
                            raise _WorktreeError(
                                f"refusing to overwrite untracked/ignored entry in "
                                f"{_display_path(rel)}: {_display_path(crel)}"
                            )
                    dirnames[:] = real_subdirs
            except _WorktreeError:
                raise
            except (OSError, RecursionError) as exc:
                raise _WorktreeError(
                    f"failed to inspect directory {_display_path(rel)}: {exc}"
                ) from exc
        elif not _is_deleted_match(rel, tst, deleted, deleted_file_ids, icase=icase):
            raise _WorktreeError(
                f"refusing to overwrite existing untracked or ignored file: {_display_path(rel)}"
            )


def _check_new_gitlinks(repo_root: Path, base_commit: str, target_commit: str) -> None:
    """Reject newly added 160000 gitlink/submodule entries before staging a slice."""
    res = _require_git(
        repo_root,
        ["diff-tree", "-r", "--raw", "--no-renames", "-z", base_commit, target_commit],
        "failed to inspect worktree tree entries",
    )
    tokens = res.stdout.split(b"\x00")
    idx = 0
    while idx + 1 < len(tokens):
        header, path_b = tokens[idx], tokens[idx + 1]
        idx += 2
        if not header.startswith(b":"):
            continue
        parts = header[1:].split(b" ")
        if len(parts) >= 2 and parts[1] == b"160000" and parts[0] != b"160000":
            raise _WorktreeError(
                f"refusing to apply newly added gitlink/submodule: "
                f"{_display_path(os.fsdecode(path_b))}"
            )


def _snapshot_worktree_commit(wt_res: Path, pre_wt_head: str) -> str:
    """Snapshot dirty wt_res via a copy of its live index without mutating wt_res."""
    wt_status = _require_git(
        wt_res, ["status", "--porcelain", "--untracked-files=all"], "failed to inspect worktree"
    )
    if not wt_status.stdout.strip():
        return pre_wt_head

    try:
        tmp_dir = Path(tempfile.mkdtemp(prefix="agy-idx-"))
    except OSError as exc:
        raise _WorktreeError(f"failed to create snapshot temp directory: {exc}") from exc

    try:
        tmp_idx = tmp_dir / "index"
        idx_env = {"GIT_INDEX_FILE": str(tmp_idx)}
        wt_idx = _rev_parse_path(wt_res, "--git-path", "index")
        seeded = False
        if wt_idx is not None:
            try:
                src_fd = os.open(str(wt_idx), os.O_RDONLY | _O_SAFE_OPEN)
            except FileNotFoundError:
                src_fd = -1
            except OSError as exc:
                raise _WorktreeError(f"failed to open worktree index: {exc}") from exc
            if src_fd >= 0:
                fd_owned = True
                try:
                    src_st = os.fstat(src_fd)
                    if not stat.S_ISREG(src_st.st_mode):
                        raise _WorktreeError("worktree index is not a regular file")
                    src_fh = os.fdopen(src_fd, "rb")
                    fd_owned = False
                    with src_fh, tmp_idx.open("wb") as dst_fh:
                        shutil.copyfileobj(src_fh, dst_fh)
                    os.utime(tmp_idx, ns=(src_st.st_atime_ns, src_st.st_mtime_ns))
                except OSError as exc:
                    raise _WorktreeError(f"failed to copy worktree index: {exc}") from exc
                finally:
                    if fd_owned:
                        with contextlib.suppress(OSError):
                            os.close(src_fd)
                seeded = True
        if not seeded:
            _require_git(
                wt_res, ["read-tree", pre_wt_head], "failed to seed snapshot index",
                extra_env=idx_env,
            )
        _require_git(wt_res, ["add", "-A"], "failed to stage snapshot changes", extra_env=idx_env)
        snap_tree = _require_git(
            wt_res, ["write-tree"], "failed to write snapshot tree", extra_env=idx_env
        ).stdout.decode("ascii", errors="replace").strip()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    pre_wt_tree = _rev_parse_object(wt_res, f"{pre_wt_head}^{{tree}}")
    if pre_wt_tree is not None and snap_tree == pre_wt_tree:
        return pre_wt_head
    ct = _require_git(
        wt_res,
        [*_identity_args(wt_res), "commit-tree", snap_tree, "-p", pre_wt_head, "-m",
         "dev-swarm: slice transport"],
        "failed to create slice snapshot commit",
    )
    return ct.stdout.decode("ascii", errors="replace").strip()


def _read_squash_msg_nofollow(path: Path) -> tuple[bool, bytes | None]:
    """Read pre-existing regular SQUASH_MSG without following symlinks or blocking on FIFOs."""
    try:
        fd = os.open(str(path), os.O_RDONLY | _O_SAFE_OPEN)
    except FileNotFoundError:
        return (False, None)
    except OSError:
        return (True, None)
    fd_owned = True
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return (True, None)
        fh = os.fdopen(fd, "rb")
        fd_owned = False
        with fh:
            return (True, fh.read())
    except OSError:
        return (True, None)
    finally:
        if fd_owned:
            with contextlib.suppress(OSError):
                os.close(fd)


def _write_fd_owned(fd: int, data: bytes, *, verify_reg_and_truncate: bool = False) -> None:
    """Write bytes to an already-open FD, closing it on every normal or error exit."""
    fd_owned = True
    try:
        if verify_reg_and_truncate:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return
            os.ftruncate(fd, 0)
        fh = os.fdopen(fd, "wb")
        fd_owned = False
        with fh:
            fh.write(data)
    finally:
        if fd_owned:
            with contextlib.suppress(OSError):
                os.close(fd)


def _restore_squash_msg_nofollow(
    path: Path, *, had_squash_msg: bool, saved_bytes: bytes | None
) -> None:
    """Scrub newly created SQUASH_MSG or restore pre-existing bytes without following symlinks."""
    with contextlib.suppress(OSError):
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            if had_squash_msg and saved_bytes is not None:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_NOCTTY
                _write_fd_owned(os.open(str(path), flags, 0o644), saved_bytes)
            return
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            return
        if not had_squash_msg:
            path.unlink()
        elif saved_bytes is not None:
            _write_fd_owned(
                os.open(str(path), os.O_WRONLY | _O_SAFE_OPEN),
                saved_bytes,
                verify_reg_and_truncate=True,
            )


def _cmd_create(
    repo: RepoInfo, raw_wt_base: str | None, slice_id: str, base_ref: str, *, redirect_bp: bool
) -> int:
    _require_token(slice_id, "slice-id")
    _require_token(base_ref, "base-ref")
    safe_slice = _SAFE_NAME_RE.sub("-", slice_id).strip("-")[:80]
    if not safe_slice:
        raise _WorktreeError(f"slice-id needs alphanumeric chars: {_display_path(slice_id)!r}")
    base_sha = _rev_parse_object(repo.root, f"{base_ref}^{{commit}}")
    if base_sha is None:
        raise _WorktreeError(f"cannot resolve base-ref {_display_path(base_ref)!r} to a commit")
    canon_ns = _canonicalize_existing_path(repo.common_dir / "refs" / "heads" / "dev-swarm")
    if canon_ns is not None and canon_ns.parts[-3:] != ("refs", "heads", "dev-swarm"):
        raise _WorktreeError(
            f"case-mismatched refs/heads/dev-swarm directory exists: {_display_path(str(canon_ns))}"
        )

    wt_base_res, _explicit = _resolve_wt_base(repo, raw_wt_base, create=True)
    base_stamp = f"{datetime.now().strftime('%H%M%S')}-{os.getpid()}"
    counter = 0
    while True:
        stamp = base_stamp if counter == 0 else f"{base_stamp}-{counter}"
        branch = f"dev-swarm/{safe_slice}-{stamp}"
        wt_path = wt_base_res / f"{safe_slice}-{stamp}"
        if not _path_exists_or_symlink(wt_path) and not _ref_exists(
            repo.root, f"refs/heads/{branch}"
        ):
            break
        counter += 1

    add_res = _run_git(
        repo.root, ["worktree", "add", "-q", "-b", branch, "--", str(wt_path), base_sha]
    )
    if add_res.returncode != 0:
        if _path_exists_or_symlink(wt_path):
            with contextlib.suppress(OSError):
                _rmtree_owned_dir(wt_path)
            _run_git(repo.root, ["worktree", "prune"])
        if _ref_exists(repo.root, f"refs/heads/{branch}"):
            _run_git(repo.root, ["branch", "-D", "--", branch])
        raise _WorktreeError(f"git worktree add failed: {_git_error_text(add_res)}")

    _write_stdout_safe(
        f"WORKTREE_PATH={_display_path(str(wt_path))}\n"
        f"WORKTREE_BRANCH={_display_path(branch)}\nBASE_SHA={base_sha}\n",
        redirect_broken_pipe_fd=redirect_bp,
    )
    return 0


def _cmd_status(raw_repo: str | None, wt_path_arg: str, *, redirect_bp: bool) -> int:
    wt_res, disp = _resolve_existing_wt_dir(wt_path_arg)
    wt_info = _detect_git_worktree(wt_res)
    if wt_info is None or not _same_path(wt_info.root, wt_res):
        raise _WorktreeError(f"path is not a git worktree root: {disp}")
    if raw_repo is not None:
        repo = _validate_start_repo(raw_repo)
        if not _same_path(wt_info.common_dir, repo.common_dir):
            raise _WorktreeError(f"worktree does not belong to --repo: {disp}")

    chunks = [_require_git(wt_res, ["status", "--short"], "git status failed").stdout.decode(
        "utf-8", errors="replace"
    )]
    if _rev_parse_object(wt_res, "HEAD^{commit}") is not None:
        diff = _run_git(
            wt_res, ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--stat", "HEAD", "--"]
        )
        if diff.returncode == 0 and diff.stdout:
            chunks.append(diff.stdout.decode("utf-8", errors="replace"))
    _write_stdout_safe(
        _sanitize_multiline_output("".join(chunks)), redirect_broken_pipe_fd=redirect_bp
    )
    return 0


def _squash_apply_tree(repo: RepoInfo, head_sha: str, wt_head_sha: str, disp: str) -> None:
    """Squash-stage wt_head_sha into repo.root index & working tree without advancing HEAD."""
    wt_tree_res = _run_git(repo.root, ["write-tree"])
    head_tree = _rev_parse_object(repo.root, "HEAD^{tree}")
    if wt_tree_res.returncode != 0 or not wt_tree_res.stdout.strip() or head_tree is None:
        raise _WorktreeError("failed to snapshot repository index and HEAD tree")
    i_tree = wt_tree_res.stdout.decode("ascii", errors="replace").strip()

    mb_res = _run_git(repo.root, ["merge-base", head_sha, wt_head_sha])
    diff_base = (
        mb_res.stdout.decode("ascii", errors="replace").strip()
        if mb_res.returncode == 0 and mb_res.stdout.strip() else head_sha
    )
    _check_new_gitlinks(repo.root, diff_base, wt_head_sha)

    if _git_version(repo.root) >= (2, 38):
        lhs = head_sha if i_tree == head_tree else _require_git(
            repo.root,
            [*_identity_args(repo.root), "commit-tree", i_tree, "-p", head_sha, "-m",
             "dev-swarm-index"],
            "failed to construct index commit",
        ).stdout.decode("ascii", errors="replace").strip()
        mt = _require_git(
            repo.root, ["merge-tree", "--write-tree", lhs, wt_head_sha],
            f"merge conflict applying worktree {disp}",
        )
        mt_lines = mt.stdout.splitlines()
        if not mt_lines:
            raise _WorktreeError("git merge-tree returned empty output")
        merged = mt_lines[0].decode("ascii", errors="replace").strip()
        added = _diff_tree_filter(repo.root, "A", i_tree, merged)
        deleted = set(_diff_tree_filter(repo.root, "D", i_tree, merged))
        _check_added_path_collisions(repo.root, added, deleted)
        _require_git(
            repo.root, ["read-tree", "-m", "-u", i_tree, merged],
            "failed to stage merged tree into repository",
        )
        return

    if i_tree != head_tree:
        raise _WorktreeError("stacking multiple worktree slices requires git >= 2.38")
    ls_raw = _run_git(repo.root, ["ls-tree", "-r", "--name-only", "-z", head_tree]).stdout
    tracked_head = {os.fsdecode(p) for p in ls_raw.split(b"\x00") if p}
    added = [p for p in _diff_tree_filter(repo.root, "A", diff_base, wt_head_sha)
             if p not in tracked_head]
    deleted = {p for p in _diff_tree_filter(repo.root, "D", diff_base, wt_head_sha)
               if p in tracked_head}
    _check_added_path_collisions(repo.root, added, deleted)

    sq_path = _rev_parse_path(repo.root, "--git-path", "SQUASH_MSG")
    had_sq, saved_sq = _read_squash_msg_nofollow(sq_path) if sq_path is not None else (False, None)
    mg_args = [
        "merge", "--squash", "--ff", "--no-autostash", "--no-verify-signatures", "-q", wt_head_sha
    ]
    mg = _run_git(repo.root, mg_args)
    if mg.returncode != 0 and _has_unmerged(repo.root):
        _run_git(repo.root, ["reset", "--merge"])
    if sq_path is not None:
        _restore_squash_msg_nofollow(sq_path, had_squash_msg=had_sq, saved_bytes=saved_sq)
    if mg.returncode != 0:
        raise _WorktreeError(f"failed to squash-apply worktree {disp}: {_git_error_text(mg)}")


def _cmd_apply(repo: RepoInfo, wt_path_arg: str, *, redirect_bp: bool) -> int:
    wt_res, disp = _resolve_existing_wt_dir(wt_path_arg)
    wt_info = _detect_git_worktree(wt_res)
    if (
        wt_info is None or not _same_path(wt_info.root, wt_res)
        or not _same_path(wt_info.common_dir, repo.common_dir) or _paths_overlap(wt_res, repo.root)
    ):
        raise _WorktreeError(f"path is not a linked worktree of this repository: {disp}")
    if _find_linked_worktree(wt_res, _parse_linked_worktrees(repo.root)[2]) is None:
        raise _WorktreeError(f"path is not a registered linked worktree: {disp}")

    head_sha = _rev_parse_object(repo.root, "HEAD^{commit}")
    if head_sha is None:
        raise _WorktreeError("target repository has an unborn HEAD; create an initial commit first")
    if _has_unmerged(repo.root):
        raise _WorktreeError("target repository has unmerged index entries")
    _assert_no_active_git_op(repo.root, "target repository")
    if _has_unmerged(wt_res):
        raise _WorktreeError(f"worktree has unmerged index entries: {disp}")
    _assert_no_active_git_op(wt_res, f"worktree {disp}")
    pre_wt_head = _rev_parse_object(wt_res, "HEAD^{commit}")
    if pre_wt_head is None:
        raise _WorktreeError(f"worktree has an unborn HEAD: {disp}")

    wt_label = _current_branch(wt_res) or f"detached@{pre_wt_head[:12]}"
    wt_head_sha = _snapshot_worktree_commit(wt_res, pre_wt_head)
    if _run_git(repo.root, ["merge-base", "--is-ancestor", wt_head_sha, head_sha]).returncode != 0:
        _squash_apply_tree(repo, head_sha, wt_head_sha, disp)

    repo_st = _run_git(repo.root, ["status", "--short"])
    st_text = (
        _sanitize_multiline_output(repo_st.stdout.decode("utf-8", errors="replace"))
        if repo_st.returncode == 0 else ""
    )
    _write_stdout_safe(
        f"APPLIED_FROM={disp} ({_display_path(wt_label)})\n{st_text}",
        redirect_broken_pipe_fd=redirect_bp,
    )
    return 0


def _same_ref(common_dir: Path, ref_a: str | None, ref_b: str, *, icase_git: bool) -> bool:
    if ref_a is None:
        return False
    return (
        ref_a == ref_b
        or (icase_git and _fold_rel(ref_a) == _fold_rel(ref_b))
        or _same_path(common_dir / ref_a, common_dir / ref_b)
    )


def _cmd_cleanup(
    repo: RepoInfo,
    raw_wt_base: str | None,
    wt_path_arg: str,
    branch_name: str | None,
    *,
    redirect_bp: bool,
) -> int:
    _require_token(wt_path_arg, "worktree path")
    icase_git = _same_path(repo.common_dir / "HEAD", repo.common_dir / "head")
    exact_refs: set[str] = set()
    target_ref = ""
    if branch_name is not None:
        _require_token(branch_name, "branch name")
        b_disp = _display_path(branch_name)
        if not branch_name.startswith("dev-swarm/") or len(branch_name) <= len("dev-swarm/"):
            raise _WorktreeError(f"refusing to delete non-dev-swarm branch: {b_disp}")
        if _run_git(repo.root, ["check-ref-format", "--branch", branch_name]).returncode != 0:
            raise _WorktreeError(f"invalid branch name: {b_disp}")
        target_ref = f"refs/heads/{branch_name}"
        ref_list = _require_git(
            repo.root, ["for-each-ref", "--format=%(refname)", "refs/heads/dev-swarm"],
            "failed to enumerate dev-swarm branches",
        )
        exact_refs = {os.fsdecode(ln) for ln in ref_list.stdout.splitlines() if ln}
        if target_ref not in exact_refs and _ref_exists(repo.root, target_ref):
            raise _WorktreeError(f"refusing to delete case-mismatched branch: {b_disp}")
        cur_branch = _current_branch(repo.root)
        if cur_branch and _same_ref(
            repo.common_dir, f"refs/heads/{cur_branch}", target_ref, icase_git=icase_git
        ):
            raise _WorktreeError(f"refusing to delete currently checked-out branch: {b_disp}")

    wt_base_res, explicit_wt_base = _resolve_wt_base(repo, raw_wt_base, create=False)
    primary_root, primary_branch, linked = _parse_linked_worktrees(repo.root)

    wt_path = Path(wt_path_arg.rstrip("/") or wt_path_arg)
    disp = _display_path(wt_path_arg)
    if wt_path.name in ("", ".", ".."):
        raise _WorktreeError(f"invalid worktree path: {disp!r}")
    wt_abs = wt_path if wt_path.is_absolute() else _safe_cwd() / wt_path
    _reject_dotdot_after_missing(wt_abs, disp, "worktree path")

    wt_res: Path | None = None
    st: os.stat_result | None = None
    is_registered = False
    try:
        st = os.lstat(wt_abs)
    except FileNotFoundError:
        _validate_dir_ancestors(wt_abs.parent, "worktree path")
        resolved_missing = _resolve_possibly_missing(wt_abs)
        if resolved_missing is None or _path_exists_or_symlink(resolved_missing):
            raise _WorktreeError(f"refusing to clean unmanaged missing path: {disp}") from None
        target_res = resolved_missing
    except (OSError, ValueError) as exc:
        raise _WorktreeError(f"failed to inspect worktree path {disp!r}: {exc}") from exc
    else:
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise _WorktreeError(f"refusing to clean non-directory or symlink path: {disp}")
        wt_res = _canonicalize_existing_path(wt_abs)
        if wt_res is None:
            raise _WorktreeError(f"failed to resolve worktree path: {disp}")
        target_res = wt_res

    if _paths_overlap(target_res, repo.root) or (
        primary_root is not None and _paths_overlap(target_res, primary_root)
    ):
        raise _WorktreeError(f"refusing to clean repository path: {disp}")

    entry = _find_linked_worktree(target_res, linked)
    if entry is not None:
        if entry.locked:
            raise _WorktreeError(f"refusing to clean locked worktree: {disp}")
        if not _linked_worktree_allowed(target_res, entry, wt_base_res, explicit_wt_base):
            raise _WorktreeError(f"refusing to clean non-dev-swarm worktree: {disp}")
        is_registered = True
    elif not _is_in_wt_base(target_res, wt_base_res):
        raise _WorktreeError(f"refusing to clean path outside worktree base: {disp}")

    if st is not None:
        assert wt_res is not None
        if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
            raise _WorktreeError(f"refusing to remove directory not owned by user: {disp}")
        has_top_git = _verify_orphan_dot_git(wt_res, repo.common_dir)
        if entry is not None and not _is_in_wt_base(target_res, wt_base_res):
            wt_info = _detect_git_worktree(wt_res)
            if (
                not has_top_git
                or wt_info is None
                or not _same_path(wt_info.root, wt_res)
                or not _same_path(wt_info.common_dir, repo.common_dir)
            ):
                raise _WorktreeError(f"refusing to clean unverified worktree outside base: {disp}")

    if branch_name is not None:
        b_disp = _display_path(branch_name)
        if _same_ref(repo.common_dir, primary_branch, target_ref, icase_git=icase_git):
            raise _WorktreeError(f"refusing to delete branch checked out in primary: {b_disp}")
        if any(
            _same_ref(repo.common_dir, o.branch_ref, target_ref, icase_git=icase_git)
            and (o.resolved_path is None or not _same_path(o.resolved_path, target_res))
            for o in linked
        ):
            raise _WorktreeError(f"refusing to delete branch in another worktree: {b_disp}")

    if wt_res is not None:
        assert st is not None
        pre_id = (st.st_dev, st.st_ino)
        rm_cmd = ["worktree", "remove", "--force", "--", str(wt_res)]
        need_fb = (not is_registered) or (_run_git(repo.root, rm_cmd).returncode != 0)
        if need_fb and _path_exists_or_symlink(wt_res):
            try:
                _rmtree_owned_dir(wt_res, expected_id=pre_id)
            except OSError as exc:
                raise _WorktreeError(f"failed to remove worktree directory {disp}: {exc}") from exc

    _run_git(repo.root, ["worktree", "prune"])
    if branch_name is not None and target_ref in exact_refs:
        _require_git(
            repo.root, ["branch", "-D", "--", branch_name],
            f"failed to delete branch {_display_path(branch_name)}",
        )
    _write_stdout_safe(f"CLEANED={disp}\n", redirect_broken_pipe_fd=redirect_bp)
    return 0


def _add_common_args(parser: argparse.ArgumentParser, *, suppress_defaults: bool) -> None:
    dval: object = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--repo", "-C", dest="repo", metavar="DIR", default=dval,
        help="Target git repository or subdirectory (defaults to current directory).",
    )
    parser.add_argument(
        "--wt-base", dest="wt_base", metavar="DIR", default=dval,
        help="Base directory for worktrees (defaults to /tmp/agy-wt-<repo>).",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for manage_worktree.py."""
    parser = argparse.ArgumentParser(
        prog="manage_worktree.py",
        description="Manage isolated local git worktrees for staged or parallel slices.",
    )
    _add_common_args(parser, suppress_defaults=False)
    sub = parser.add_subparsers(dest="command", required=True)
    parsers = {
        name: sub.add_parser(name, help=help_text)
        for name, help_text in (
            ("create", "Create an isolated git worktree."),
            ("status", "Show git status and diff stat for a worktree."),
            ("apply", "Squash-stage a worktree into the index without advancing HEAD."),
            ("cleanup", "Remove a managed dev-swarm worktree and optional dev-swarm/* branch."),
        )
    }
    for p in parsers.values():
        _add_common_args(p, suppress_defaults=True)
    parsers["create"].add_argument("slice_id", help="Slice identifier for branch/dir naming.")
    parsers["create"].add_argument("base_ref", nargs="?", default="HEAD", help="Base commit-ish.")
    for name in ("status", "apply", "cleanup"):
        parsers[name].add_argument("worktree_path", help="Path to the worktree directory.")
    parsers["cleanup"].add_argument("branch_name", nargs="?", default=None, help="dev-swarm/* ref.")
    return parser


def _run_main(argv: Sequence[str] | None, *, redirect_broken_pipe_fd: bool) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    raw_repo: str | None = getattr(args, "repo", None)
    raw_wt_base: str | None = getattr(args, "wt_base", None)
    try:
        if args.command == "status":
            return _cmd_status(raw_repo, args.worktree_path, redirect_bp=redirect_broken_pipe_fd)
        repo = _validate_start_repo(raw_repo)
        if args.command == "create":
            return _cmd_create(
                repo, raw_wt_base, args.slice_id, args.base_ref, redirect_bp=redirect_broken_pipe_fd
            )
        if args.command == "apply":
            return _cmd_apply(repo, args.worktree_path, redirect_bp=redirect_broken_pipe_fd)
        if args.command == "cleanup":
            return _cmd_cleanup(
                repo, raw_wt_base, args.worktree_path, args.branch_name,
                redirect_bp=redirect_broken_pipe_fd,
            )
    except _WorktreeError as exc:
        _write_stream_safe(
            sys.stderr, f"ERROR: {exc}\n", redirect_broken_pipe_fd=redirect_broken_pipe_fd
        )
        return 1
    parser.error(f"unknown command: {args.command}")
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint returning an integer exit code without mutating host OS FDs."""
    return _run_main(argv, redirect_broken_pipe_fd=False)


if __name__ == "__main__":
    raise SystemExit(_run_main(None, redirect_broken_pipe_fd=True))
