#!/usr/bin/env python3
"""Collect git diff, changed files, surrounding context, and safety hazard signals.

Strictly read-only inside the target repository. Writes a self-contained context
bundle to a fresh temporary directory and prints ``summary.txt`` to stdout.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

try:
    import fcntl as _fcntl_mod
except ImportError:  # pragma: no cover
    _fcntl_mod = None  # type: ignore[assignment]

_FCNTL: Any = _fcntl_mod

_SANITIZED_GIT_ENV_KEYS = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_EXTERNAL_DIFF",
        "GIT_DIFF_OPTS",
        "GIT_LITERAL_PATHSPECS",
        "GIT_GLOB_PATHSPECS",
        "GIT_NOGLOB_PATHSPECS",
        "GIT_ICASE_PATHSPECS",
        "GIT_NO_LAZY_FETCH",
        "GIT_TERMINAL_PROMPT",
        "GIT_NO_REPLACE_OBJECTS",
    }
)
_SANITIZED_GIT_ENV_PREFIXES: tuple[str, ...] = (
    "GIT_CONFIG_KEY_",
    "GIT_CONFIG_VALUE_",
)

_DIFF_SAFETY_FLAGS: tuple[str, ...] = (
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
    "--src-prefix=a/",
    "--dst-prefix=b/",
    "--submodule=short",
    "--ignore-submodules=dirty",
    f"-O{os.devnull}",
)

_BASE_REF_CANDIDATES: tuple[str, ...] = (
    "origin/main",
    "origin/master",
    "origin/trunk",
    "main",
    "master",
    "trunk",
    "develop",
)

_NON_GIT_EXCLUDED_DIRS = frozenset({"Library", "node_modules", "target", "build"})

_CPP_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".c++",
        ".h",
        ".hh",
        ".hpp",
        ".hxx",
        ".ipp",
        ".inl",
        ".cu",
        ".cuh",
        ".m",
        ".mm",
    }
)

_PROSE_AND_DATA_SUFFIXES = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".rst",
        ".adoc",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".csv",
        ".lock",
    }
)

_UNSAFE_WORD_RE = re.compile(r"(?<![A-Za-z0-9_])unsafe(?![A-Za-z0-9_])")
_UNSAFE_WORD_BYTES_RE = re.compile(rb"(?<![A-Za-z0-9_])unsafe(?![A-Za-z0-9_])")
_CONTROL_CHAR_RE = re.compile(
    r"[\x00-\x08\x0a-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)

_FFI_RE = re.compile(
    r'extern[ \t]+"C(?:[+-]unwind)?"'
    r"|#\[(?:no_mangle|unsafe\(no_mangle\))\]"
    r"|bindgen"
    r"|cxx::bridge"
    r"|jni::"
    r"|ctypes"
    r"|cffi"
    r'|import "C"'
    r"|unsafe\.Pointer"
    r"|DllImport"
    r"|Napi::"
    r"|(?<![A-Za-z0-9_])(?:ioctl|syscall|mmap|mprotect|copy_from_user|copy_to_user)[ \t]*\("
)

_CONCURRENCY_RE = re.compile(
    r"UnsafeCell"
    r"|MaybeUninit"
    r"|transmute"
    r"|from_raw_parts"
    r"|reinterpret_cast"
    r"|const_cast"
    r"|memory_order"
    r"|Atomic"
    r"|std::thread"
    r"|pthread_"
    r"|tokio::select!"
    r"|unsafe[ \t]+impl[ \t]+(?:Send|Sync)"
    r"|co_await"
)

_UNIFIED_HUNK_RE = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_COMBINED_HUNK_RE = re.compile(
    rb"^(@{3,}) ((?:-\d+(?:,\d+)? )+)\+(\d+)(?:,(\d+))? \1"
)
_PARENT_RANGE_RE = re.compile(rb"-(\d+)(?:,(\d+))?")

_BYTE_SPACE = ord(" ")
_BYTE_PLUS = ord("+")
_BYTE_MINUS = ord("-")
_COMBINED_PREFIX_BYTES = frozenset({_BYTE_SPACE, _BYTE_PLUS, _BYTE_MINUS})

_GIT_SUBPROCESS_TIMEOUT_SECONDS = 60
_BINARY_PROBE_BYTES = 8192
_MAX_UNTRACKED_LINES = 1000
_MAX_PREVIEW_LINE_BYTES = 4096
_HAZARD_CHUNK_BYTES = 65536
_HAZARD_OVERLAP_BYTES = 64
_MAX_HAZARD_SNIPPET_BYTES = 512
_MAX_SUMMARY_HAZARD_LINES = 80
_MAX_REPO_FILES_LINES = 5000

_FILTER_ACTION_SUFFIXES = frozenset({b"clean", b"smudge", b"process", b"required"})
_SCOPED_PATH_CONFIG_KEYS: tuple[tuple[bytes, str], ...] = (
    (b"core.excludesfile", "core.excludesFile"),
    (b"core.attributesfile", "core.attributesFile"),
)
_TRUSTED_CONFIG_SCOPES = frozenset({b"system", b"global"})

_C_ESCAPE_MAP: dict[int, int] = {
    ord("a"): 0x07,
    ord("b"): 0x08,
    ord("f"): 0x0C,
    ord("n"): 0x0A,
    ord("r"): 0x0D,
    ord("t"): 0x09,
    ord("v"): 0x0B,
    ord("\\"): 0x5C,
    ord('"'): 0x22,
}


@dataclass(frozen=True)
class DiffAnalysis:
    """Line-change metrics and fail-safe Rust unsafe signal from raw git diff output."""

    added_lines: int
    removed_lines: int
    has_rust_unsafe: bool


@dataclass(frozen=True)
class GitResult:
    """Captured result from a read-only git subprocess invocation."""

    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class FileHazardScan:
    """Aggregated language classification and safety/concurrency hazard hits."""

    cpp_files: list[str]
    rust_unsafe_files: list[str]
    has_cpp: bool
    has_rust: bool
    has_rust_unsafe: bool
    has_ffi: bool
    has_concurrency: bool
    summary_hazards: list[str]


def _escape_control_char(match: re.Match[str]) -> str:
    ch = match.group(0)
    if ch == "\n":
        return "\\n"
    if ch == "\r":
        return "\\r"
    code = ord(ch)
    if code <= 0xFF:
        return f"\\x{code:02x}"
    return f"\\u{code:04x}"


def _display_path(raw: str) -> str:
    """Render a path or label as single-line UTF-8, escaping surrogates, control, and BiDi chars."""
    safe_utf8 = raw.encode("utf-8", errors="surrogateescape").decode(
        "utf-8", errors="backslashreplace"
    )
    return _CONTROL_CHAR_RE.sub(_escape_control_char, safe_utf8)


def _trim_incomplete_utf8(data: bytes, *, next_byte: int | None = None) -> bytes:
    """Step back up to 3 bytes if data ends inside an incomplete UTF-8 multibyte sequence."""
    if next_byte is not None and (next_byte & 0xC0) != 0x80:
        return data
    length = len(data)
    if length == 0 or data[-1] < 0x80:
        return data
    start = max(0, length - 4)
    for idx in range(length - 1, start - 1, -1):
        bval = data[idx]
        if bval < 0x80:
            return data
        if (bval & 0xC0) != 0x80:
            if 0xC2 <= bval <= 0xDF:
                expected = 2
            elif 0xE0 <= bval <= 0xEF:
                expected = 3
            elif 0xF0 <= bval <= 0xF4:
                expected = 4
            else:
                return data
            return data[:idx] if (length - idx) < expected else data
    return data


def _git_env(extra_config: Sequence[tuple[str, str]] = ()) -> dict[str, str]:
    """Build a sanitized environment for read-only git subprocess calls."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _SANITIZED_GIT_ENV_KEYS
        and not k.startswith(_SANITIZED_GIT_ENV_PREFIXES)
    }
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_LITERAL_PATHSPECS"] = "1"
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    if extra_config:
        env["GIT_CONFIG_COUNT"] = str(len(extra_config))
        for idx, (key, value) in enumerate(extra_config):
            env[f"GIT_CONFIG_KEY_{idx}"] = key
            env[f"GIT_CONFIG_VALUE_{idx}"] = value
    return env


def _run_git(
    cwd: Path,
    args: Sequence[str],
    *,
    extra_config: Sequence[tuple[str, str]] = (),
) -> GitResult:
    """Run git in read-only mode with external hooks, submodules, mailmap, and signatures off."""
    cmd = [
        "git",
        "--no-pager",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.quotePath=false",
        "-c",
        "log.showSignature=false",
        "-c",
        "log.mailmap=false",
        "-c",
        f"mailmap.file={os.devnull}",
        "-c",
        "diff.submodule=short",
        "-c",
        "submodule.recurse=false",
        *args,
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=_git_env(extra_config),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_GIT_SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return GitResult(returncode=127, stdout=b"", stderr=str(exc).encode("utf-8"))
    return GitResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def _is_safe_config_file_value(raw_val: str) -> bool:
    """Return True if a configured git file path is empty, missing, or an absolute regular file."""
    if raw_val == "":
        return True
    if "\x00" in raw_val:
        return False
    expanded = os.path.expanduser(raw_val)
    if not os.path.isabs(expanded):
        return False
    try:
        st = os.stat(expanded)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode)


def _discover_config_overrides(repo_root: Path) -> tuple[tuple[str, str], ...]:
    """Parse raw git config bytes to neutralize filters and hostile excludes/attributes files."""
    res = _run_git(repo_root, ["config", "--list", "--show-scope", "--includes", "-z"])
    if res.returncode != 0 or not res.stdout:
        return ()

    tokens = res.stdout.split(b"\x00")
    seen_filters: set[bytes] = set()
    overrides: list[tuple[str, str]] = []

    trusted_safe_paths: dict[bytes, str | None] = {
        key: None for key, _ in _SCOPED_PATH_CONFIG_KEYS
    }
    needs_path_override: dict[bytes, bool] = {
        key: False for key, _ in _SCOPED_PATH_CONFIG_KEYS
    }

    idx = 0
    total = len(tokens)
    while idx + 1 < total:
        scope = tokens[idx]
        entry = tokens[idx + 1]
        idx += 2
        if not scope and not entry:
            continue

        key, sep, raw_val = entry.partition(b"\n")
        if not key:
            continue

        parts = key.split(b".")
        if (
            len(parts) >= 3
            and parts[0].lower() == b"filter"
            and parts[-1].lower() in _FILTER_ACTION_SUFFIXES
        ):
            if key not in seen_filters:
                seen_filters.add(key)
                value = "false" if parts[-1].lower() == b"required" else ""
                overrides.append((os.fsdecode(key), value))
            continue

        lower_key = key.lower()
        if lower_key in needs_path_override:
            if scope not in _TRUSTED_CONFIG_SCOPES:
                needs_path_override[lower_key] = True
            else:
                decoded_val = os.fsdecode(raw_val)
                if sep == b"\n" and _is_safe_config_file_value(decoded_val):
                    trusted_safe_paths[lower_key] = decoded_val
                    needs_path_override[lower_key] = False
                else:
                    trusted_safe_paths[lower_key] = None
                    needs_path_override[lower_key] = True

    for lower_key, canonical_key in _SCOPED_PATH_CONFIG_KEYS:
        if needs_path_override[lower_key]:
            safe_val = trusted_safe_paths[lower_key]
            overrides.append((canonical_key, safe_val if safe_val is not None else os.devnull))

    return tuple(overrides)


@dataclass(frozen=True)
class GitContext:
    """Read-only git command runner scoped to a repository with config neutralization."""

    repo_root: Path
    config_overrides: tuple[tuple[str, str], ...] = ()

    def run(self, args: Sequence[str]) -> GitResult:
        return _run_git(self.repo_root, args, extra_config=self.config_overrides)

    def text(self, args: Sequence[str]) -> tuple[int, str]:
        res = self.run(args)
        return (res.returncode, res.stdout.decode("utf-8", errors="replace").strip())

    def z_paths(self, args: Sequence[str]) -> list[str]:
        res = self.run(args)
        if res.returncode != 0 or not res.stdout:
            return []
        paths: list[str] = []
        for raw_item in res.stdout.split(b"\x00"):
            if not raw_item:
                continue
            decoded = os.fsdecode(raw_item)
            if decoded:
                paths.append(decoded)
        return paths

    def diff(
        self, diff_args: Sequence[str] = (), pathspecs: Sequence[str] = ()
    ) -> bytes:
        res = self.run(["diff", *_DIFF_SAFETY_FLAGS, *diff_args, "--", *pathspecs])
        return res.stdout if res.returncode == 0 else b""

    def show(self, extra_args: Sequence[str]) -> bytes:
        res = self.run(
            ["show", "--no-show-signature", "--no-mailmap", *_DIFF_SAFETY_FLAGS, *extra_args]
        )
        return res.stdout if res.returncode == 0 else b""

    def diff_files(
        self, diff_args: Sequence[str] = (), pathspecs: Sequence[str] = ()
    ) -> list[str]:
        return self.z_paths(
            ["diff", *_DIFF_SAFETY_FLAGS, "--name-only", "-z", *diff_args, "--", *pathspecs]
        )

    def diff_and_files(
        self, diff_args: Sequence[str] = (), pathspecs: Sequence[str] = ()
    ) -> tuple[bytes, list[str]]:
        return (
            self.diff(("-U15", *diff_args), pathspecs),
            self.diff_files(diff_args, pathspecs),
        )

    def untracked_paths(self, pathspecs: Sequence[str] = ()) -> list[str]:
        args: list[str] = ["ls-files", "--others", "--exclude-standard", "-z"]
        if pathspecs:
            args.extend(["--", *pathspecs])
        return self.z_paths(args)

    def working_diff_and_files(
        self,
        has_head: bool,
        untracked_files: Sequence[str],
        pathspecs: Sequence[str] = (),
    ) -> tuple[bytes, list[str]]:
        if has_head:
            diff_bytes, files = self.diff_and_files(("HEAD",), pathspecs)
        else:
            staged_diff, staged_files = self.diff_and_files(("--cached",), pathspecs)
            unstaged_diff, unstaged_files = self.diff_and_files((), pathspecs)
            diff_bytes = staged_diff + unstaged_diff
            files = staged_files + unstaged_files
        files.extend(untracked_files)
        return (diff_bytes, files)


def _validate_accessible_dir(raw: str, flag: str, *, need_write: bool = False) -> Path | None:
    """Validate that a CLI directory option points to an accessible directory."""
    safe_raw = _display_path(raw)
    if not raw or "\x00" in raw:
        print(f"ERROR: {flag} requires a valid directory path: {safe_raw!r}", file=sys.stderr)
        return None
    candidate = Path(raw)
    try:
        if not candidate.exists():
            print(f"ERROR: {flag} directory does not exist: {safe_raw}", file=sys.stderr)
            return None
        if not candidate.is_dir():
            print(f"ERROR: {flag} path is not a directory: {safe_raw}", file=sys.stderr)
            return None
        mode = os.R_OK | os.X_OK | (os.W_OK if need_write else 0)
        if not os.access(candidate, mode):
            print(f"ERROR: {flag} directory is not accessible: {safe_raw}", file=sys.stderr)
            return None
        return candidate.resolve(strict=True)
    except OSError as exc:
        print(f"ERROR: failed to access {flag} directory {safe_raw!r}: {exc}", file=sys.stderr)
        return None


def _open_confined_regular_file(root_res: Path, rel_path: str) -> io.BufferedReader | None:
    """Open a regular non-symlink file strictly beneath root_res without TOCTOU races."""
    if not rel_path or "\x00" in rel_path or os.path.isabs(rel_path):
        return None
    parts = rel_path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None

    o_dir = int(getattr(os, "O_DIRECTORY", 0))
    o_nofollow = int(getattr(os, "O_NOFOLLOW", 0))
    o_nonblock = int(getattr(os, "O_NONBLOCK", 0))

    cur_fd = -1
    file_fd = -1
    try:
        cur_fd = os.open(str(root_res), os.O_RDONLY | o_dir)
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | o_dir | o_nofollow, dir_fd=cur_fd)
            prev_fd, cur_fd = cur_fd, next_fd
            os.close(prev_fd)
        file_fd = os.open(parts[-1], os.O_RDONLY | o_nofollow | o_nonblock, dir_fd=cur_fd)
        prev_fd, cur_fd = cur_fd, -1
        os.close(prev_fd)

        st = os.fstat(file_fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        if _FCNTL is not None and o_nonblock:
            flags = int(_FCNTL.fcntl(file_fd, _FCNTL.F_GETFL))
            if flags & o_nonblock:
                _FCNTL.fcntl(file_fd, _FCNTL.F_SETFL, flags & ~o_nonblock)

        raw_io = io.FileIO(file_fd, "rb", closefd=True)
        file_fd = -1
        try:
            return io.BufferedReader(raw_io)
        except BaseException:
            raw_io.close()
            raise
    except OSError:
        return None
    finally:
        if cur_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(cur_fd)
        if file_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(file_fd)


def _unquote_c_path(raw: bytes) -> bytes | None:
    """Decode a git C-quoted path literal (allowing an optional trailing tab suffix)."""
    if not raw.startswith(b'"'):
        return None
    out = bytearray()
    idx = 1
    length = len(raw)
    while idx < length:
        byte_val = raw[idx]
        if byte_val == 0x22:  # '"'
            trailing = raw[idx + 1 :]
            if trailing and not trailing.startswith(b"\t"):
                return None
            return bytes(out)
        if byte_val == 0x5C:  # '\\'
            if idx + 1 >= length:
                return None
            nxt = raw[idx + 1]
            if nxt in _C_ESCAPE_MAP:
                out.append(_C_ESCAPE_MAP[nxt])
                idx += 2
                continue
            if 0x30 <= nxt <= 0x37:  # octal escape \0 .. \377
                end = min(length, idx + 4)
                oct_end = idx + 1
                while oct_end < end and 0x30 <= raw[oct_end] <= 0x37:
                    oct_end += 1
                val = int(raw[idx + 1 : oct_end], 8)
                if val > 255:
                    return None
                out.append(val)
                idx = oct_end
                continue
            return None
        out.append(byte_val)
        idx += 1
    return None


def _parse_diff_header_path(raw: bytes, *, expected_prefix: bytes | None) -> bytes | None:
    """Parse a diff header path (C-quoted or unquoted), stripping any expected a/ or b/ prefix."""
    if raw == b"/dev/null" or raw.startswith(b"/dev/null\t"):
        return b"/dev/null"
    if raw.startswith(b'"'):
        parsed = _unquote_c_path(raw)
        if parsed is None:
            return None
        path = parsed
    else:
        path = raw.split(b"\t", 1)[0]
    if not path:
        return None
    if expected_prefix is not None:
        if not path.startswith(expected_prefix):
            return None
        rel = path[len(expected_prefix) :]
        return rel if rel else None
    return path


def _split_lf_lines(data: bytes) -> list[bytes]:
    """Split raw bytes strictly on LF so embedded bare CR bytes stay within their line."""
    raw_lines = data.split(b"\n")
    if raw_lines and raw_lines[-1] == b"":
        raw_lines.pop()
    return raw_lines


def _analyze_diff_bytes(git_diff_bytes: bytes) -> DiffAnalysis:
    """Analyze raw git diff bytes using hunk line counts for exact hunk boundaries.

    Splits strictly on LF and tracks unified (``@@``) and combined merge (``@@@``)
    hunk counts so embedded CR bytes and diff content lines beginning with ``--- ``
    or ``+++ `` can never be misread as file headers. Fails safe toward
    ``has_rust_unsafe=True`` if a diff section header cannot be unambiguously parsed.
    """
    added_lines = 0
    removed_lines = 0
    has_rust_unsafe = False

    saw_known_path = False
    section_is_rs = False
    section_header_error = False

    in_hunk = False
    hunk_num_parents = 1
    parent_rems: list[int] = []
    new_rem = 0
    section_may_be_rs = True

    for raw_line in _split_lf_lines(git_diff_bytes):
        line = raw_line[:-1] if raw_line.endswith(b"\r") else raw_line

        if in_hunk:
            if line.startswith(b"\\"):
                continue

            consumed = False
            if hunk_num_parents == 1:
                if (
                    (line.startswith(b" ") or line == b"")
                    and parent_rems[0] > 0
                    and new_rem > 0
                ):
                    parent_rems[0] -= 1
                    new_rem -= 1
                    consumed = True
                elif line.startswith(b"+") and new_rem > 0:
                    new_rem -= 1
                    added_lines += 1
                    if section_may_be_rs and _UNSAFE_WORD_BYTES_RE.search(line[1:]):
                        has_rust_unsafe = True
                    consumed = True
                elif line.startswith(b"-") and parent_rems[0] > 0:
                    parent_rems[0] -= 1
                    removed_lines += 1
                    if section_may_be_rs and _UNSAFE_WORD_BYTES_RE.search(line[1:]):
                        has_rust_unsafe = True
                    consumed = True
            else:
                prefix = line[:hunk_num_parents]
                if len(prefix) == hunk_num_parents and all(
                    b in _COMBINED_PREFIX_BYTES for b in prefix
                ):
                    body = line[hunk_num_parents:]
                    if _BYTE_MINUS in prefix:
                        if all(
                            parent_rems[idx] > 0
                            for idx, bval in enumerate(prefix)
                            if bval == _BYTE_MINUS
                        ):
                            for idx, bval in enumerate(prefix):
                                if bval == _BYTE_MINUS:
                                    parent_rems[idx] -= 1
                            removed_lines += 1
                            if section_may_be_rs and _UNSAFE_WORD_BYTES_RE.search(body):
                                has_rust_unsafe = True
                            consumed = True
                    elif new_rem > 0 and all(
                        parent_rems[idx] > 0
                        for idx, bval in enumerate(prefix)
                        if bval == _BYTE_SPACE
                    ):
                        new_rem -= 1
                        for idx, bval in enumerate(prefix):
                            if bval == _BYTE_SPACE:
                                parent_rems[idx] -= 1
                        if _BYTE_PLUS in prefix:
                            added_lines += 1
                            if section_may_be_rs and _UNSAFE_WORD_BYTES_RE.search(body):
                                has_rust_unsafe = True
                        consumed = True

            if consumed:
                if new_rem <= 0 and all(rem <= 0 for rem in parent_rems):
                    in_hunk = False
                continue
            in_hunk = False

        if line.startswith(b"diff --git "):
            saw_known_path = False
            section_is_rs = False
            section_header_error = False
            continue

        if line.startswith((b"diff --cc ", b"diff --combined ")):
            saw_known_path = False
            section_is_rs = False
            section_header_error = False
            parts = line.split(b" ", 2)
            raw_path = parts[2] if len(parts) == 3 else b""
            parsed = _parse_diff_header_path(raw_path, expected_prefix=None)
            if parsed is None:
                section_header_error = True
            else:
                saw_known_path = True
                if parsed.lower().endswith(b".rs"):
                    section_is_rs = True
            continue

        if line.startswith((b"--- ", b"+++ ")):
            expected_prefix = b"a/" if line.startswith(b"--- ") else b"b/"
            parsed = _parse_diff_header_path(line[4:], expected_prefix=expected_prefix)
            if parsed is None:
                section_header_error = True
            elif parsed != b"/dev/null":
                saw_known_path = True
                if parsed.lower().endswith(b".rs"):
                    section_is_rs = True
            continue

        unified_match = _UNIFIED_HUNK_RE.match(line)
        if unified_match is not None:
            old_count = int(unified_match.group(2)) if unified_match.group(2) is not None else 1
            new_count = int(unified_match.group(4)) if unified_match.group(4) is not None else 1
            section_may_be_rs = section_is_rs or section_header_error or (not saw_known_path)
            hunk_num_parents = 1
            parent_rems = [old_count]
            new_rem = new_count
            in_hunk = old_count > 0 or new_count > 0
            continue

        combined_match = _COMBINED_HUNK_RE.match(line)
        if combined_match is not None:
            num_parents = len(combined_match.group(1)) - 1
            counts = [
                int(m.group(2)) if m.group(2) is not None else 1
                for m in _PARENT_RANGE_RE.finditer(combined_match.group(2))
            ]
            if len(counts) == num_parents:
                new_count = (
                    int(combined_match.group(4)) if combined_match.group(4) is not None else 1
                )
                section_may_be_rs = section_is_rs or section_header_error or (not saw_known_path)
                hunk_num_parents = num_parents
                parent_rems = counts
                new_rem = new_count
                in_hunk = new_count > 0 or any(c > 0 for c in counts)
            continue

    return DiffAnalysis(
        added_lines=added_lines,
        removed_lines=removed_lines,
        has_rust_unsafe=has_rust_unsafe,
    )


def _is_lstat_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _is_lstat_real_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _lstat_confined_no_symlinks(
    root_res: Path, raw_spec: str
) -> tuple[Path, os.stat_result] | None:
    """Resolve raw_spec lexically under root_res while rejecting any symlink component."""
    if not raw_spec or "\x00" in raw_spec or os.path.isabs(raw_spec):
        return None
    norm = Path(os.path.normpath(str(root_res / raw_spec)))
    if norm != root_res and not norm.is_relative_to(root_res):
        return None
    cur = root_res
    try:
        st = os.lstat(cur)
        rel = norm.relative_to(root_res)
        for part in rel.parts:
            cur = cur / part
            st = os.lstat(cur)
            if stat.S_ISLNK(st.st_mode):
                return None
    except OSError:
        return None
    return (cur, st)


def _relativize_abs_if_under_root(root_res: Path, raw_abs: str) -> str | None:
    """Return a root-relative path if an absolute path falls under root_res, else None."""
    norm_abs = Path(os.path.normpath(raw_abs))
    if norm_abs == root_res:
        return "."
    if norm_abs.is_relative_to(root_res):
        return norm_abs.relative_to(root_res).as_posix()

    for candidate in (norm_abs, *norm_abs.parents):
        try:
            if candidate.resolve(strict=True) == root_res:
                rel = norm_abs.relative_to(candidate)
                return "." if rel == Path(".") else rel.as_posix()
        except OSError:
            continue
    return None


def _walk_regular_files(
    root: Path,
    *,
    max_depth: int,
    exclude_hidden: bool,
    excluded_dir_names: frozenset[str],
    exclude_git_ci: bool = False,
    display_prefix: str | None = None,
) -> list[str]:
    """Walk non-symlink regular files under root up to max_depth levels deep."""
    found: list[str] = []
    try:
        root_st = os.lstat(root)
    except OSError:
        return found

    if stat.S_ISREG(root_st.st_mode):
        if (exclude_git_ci and root.name.lower() == ".git") or (
            exclude_hidden and root.name.startswith(".")
        ):
            return found
        if display_prefix is not None:
            return [display_prefix]
        return [root.name]

    if not stat.S_ISDIR(root_st.st_mode) or max_depth <= 0:
        return found

    root_str = str(root)
    for dirpath_str, dirnames, filenames in os.walk(root_str, followlinks=False):
        dirpath = Path(dirpath_str)
        rel_dir = dirpath.relative_to(root)
        depth = 0 if rel_dir == Path(".") else len(rel_dir.parts)

        kept_dirs: list[str] = []
        if depth < max_depth - 1:
            for dname in sorted(dirnames):
                if dname in excluded_dir_names:
                    continue
                if exclude_git_ci and dname.lower() == ".git":
                    continue
                if exclude_hidden and dname.startswith("."):
                    continue
                if _is_lstat_real_dir(dirpath / dname):
                    kept_dirs.append(dname)
        dirnames[:] = kept_dirs

        for fname in sorted(filenames):
            if exclude_git_ci and fname.lower() == ".git":
                continue
            if exclude_hidden and fname.startswith("."):
                continue
            full_path = dirpath / fname
            if not _is_lstat_regular_file(full_path):
                continue
            rel_file = (
                Path(fname) if rel_dir == Path(".") else rel_dir / fname
            ).as_posix()
            if display_prefix:
                found.append(f"{display_prefix.rstrip('/')}/{rel_file}")
            else:
                found.append(rel_file)

    return found


def _collect_non_git_files(start_dir_res: Path, targets: Sequence[str]) -> list[str]:
    """List regular files in non-git mode with find-compatible depth and pruning."""
    results: set[str] = set()
    if not targets:
        results.update(
            _walk_regular_files(
                start_dir_res,
                max_depth=3,
                exclude_hidden=True,
                excluded_dir_names=_NON_GIT_EXCLUDED_DIRS,
            )
        )
        return sorted(results)

    for raw_target in targets:
        if not raw_target or "\x00" in raw_target:
            continue

        if os.path.isabs(raw_target):
            rel_in_root = _relativize_abs_if_under_root(start_dir_res, raw_target)
            if rel_in_root is None:
                target_path = Path(os.path.normpath(raw_target))
                tname = target_path.name
                if (tname.startswith(".") and tname not in (".", "..")) or (
                    tname in _NON_GIT_EXCLUDED_DIRS
                ):
                    continue
                try:
                    resolved_ext = target_path.resolve(strict=False)
                except OSError:
                    continue
                if resolved_ext == start_dir_res or resolved_ext.is_relative_to(start_dir_res):
                    continue
                results.update(
                    _walk_regular_files(
                        target_path,
                        max_depth=4,
                        exclude_hidden=True,
                        excluded_dir_names=_NON_GIT_EXCLUDED_DIRS,
                        display_prefix=raw_target,
                    )
                )
                continue
            effective_rel = rel_in_root
            display_label = "" if rel_in_root == "." else raw_target
        else:
            if ".." in Path(raw_target).parts:
                continue
            effective_rel = raw_target
            display_label = "" if raw_target == "." else raw_target

        rel_parts = Path(effective_rel).parts
        if any(
            (part.startswith(".") and part != ".") or part in _NON_GIT_EXCLUDED_DIRS
            for part in rel_parts
        ):
            continue

        checked = _lstat_confined_no_symlinks(start_dir_res, effective_rel)
        if checked is None:
            continue
        resolved, _st = checked
        results.update(
            _walk_regular_files(
                resolved,
                max_depth=4,
                exclude_hidden=True,
                excluded_dir_names=_NON_GIT_EXCLUDED_DIRS,
                display_prefix=display_label if display_label else None,
            )
        )
    return sorted(results)


def _detect_base_ref(git_ctx: GitContext) -> str:
    """Detect the repository's default base branch ref, if any."""
    rc, sym = git_ctx.text(["symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"])
    if rc == 0 and sym and not sym.startswith("-"):
        v_rc, _ = git_ctx.text(["rev-parse", "--verify", "-q", f"{sym}^{{commit}}"])
        if v_rc == 0:
            return sym
    for candidate in _BASE_REF_CANDIDATES:
        v_rc, _ = git_ctx.text(["rev-parse", "--verify", "-q", f"{candidate}^{{commit}}"])
        if v_rc == 0:
            return candidate
    return ""


def _exists_from_dir(base_dir: Path, raw_arg: str) -> bool:
    if not raw_arg or "\x00" in raw_arg:
        return False
    candidate = Path(raw_arg) if os.path.isabs(raw_arg) else (base_dir / raw_arg)
    try:
        os.lstat(candidate)
        return True
    except OSError:
        return False


def _relativize_git_pathspec(
    repo_root_res: Path, start_dir_res: Path, raw_target: str
) -> str | None:
    """Convert a target into a repo-relative pathspec, or None if invalid or outside repo."""
    if not raw_target or "\x00" in raw_target:
        return None
    if os.path.isabs(raw_target):
        return _relativize_abs_if_under_root(repo_root_res, raw_target)

    combined = Path(os.path.normpath(str(start_dir_res / raw_target)))
    if combined == repo_root_res:
        return "."
    if combined.is_relative_to(repo_root_res):
        return combined.relative_to(repo_root_res).as_posix()
    return None


def _walk_confined_git_paths(repo_root_res: Path, pathspecs: Sequence[str]) -> list[str]:
    """Walk non-symlink regular files inside repo_root_res for clean ``paths:`` fallback."""
    results: set[str] = set()
    for raw_spec in pathspecs:
        checked = _lstat_confined_no_symlinks(repo_root_res, raw_spec)
        if checked is None:
            continue
        resolved, _st = checked
        rel_prefix = (
            ""
            if resolved == repo_root_res
            else resolved.relative_to(repo_root_res).as_posix()
        )
        if rel_prefix and any(part.lower() == ".git" for part in Path(rel_prefix).parts):
            continue
        results.update(
            _walk_regular_files(
                resolved,
                max_depth=5,
                exclude_hidden=False,
                excluded_dir_names=frozenset(),
                exclude_git_ci=True,
                display_prefix=rel_prefix if rel_prefix else None,
            )
        )
    return sorted(results)


def _write_single_untracked_preview(
    out_fh: BinaryIO,
    repo_root_res: Path,
    rel_path: str,
) -> int:
    """Stream up to 1,000 bounded-length lines of a non-binary untracked regular file."""
    fh = _open_confined_regular_file(repo_root_res, rel_path)
    if fh is None:
        return 0
    lines_written = 0
    with fh:
        probe = fh.read(_BINARY_PROBE_BYTES)
        if b"\x00" in probe:
            return 0
        fh.seek(0)

        header = f"\n=== UNTRACKED NEW FILE: {_display_path(rel_path)} ===\n".encode()
        out_fh.write(header)
        lines_written += 2

        for lineno in range(1, _MAX_UNTRACKED_LINES + 1):
            head = fh.readline(_MAX_PREVIEW_LINE_BYTES + 2)
            if not head or b"\x00" in head:
                break

            truncated = False
            if head.endswith(b"\n"):
                trimmed = head.rstrip(b"\r\n")
                if len(trimmed) > _MAX_PREVIEW_LINE_BYTES:
                    truncated = True
                    kept = _trim_incomplete_utf8(
                        trimmed[:_MAX_PREVIEW_LINE_BYTES],
                        next_byte=trimmed[_MAX_PREVIEW_LINE_BYTES],
                    )
                else:
                    kept = trimmed
            else:
                saw_more = False
                saw_nul = False
                while True:
                    drain = fh.readline(_HAZARD_CHUNK_BYTES)
                    if not drain:
                        break
                    saw_more = True
                    if b"\x00" in drain:
                        saw_nul = True
                        break
                    if drain.endswith(b"\n"):
                        break
                if saw_nul:
                    break
                if saw_more or len(head.rstrip(b"\r\n")) > _MAX_PREVIEW_LINE_BYTES:
                    truncated = True
                    next_b = (
                        head[_MAX_PREVIEW_LINE_BYTES]
                        if len(head) > _MAX_PREVIEW_LINE_BYTES
                        else None
                    )
                    kept = _trim_incomplete_utf8(
                        head[:_MAX_PREVIEW_LINE_BYTES],
                        next_byte=next_b,
                    )
                else:
                    kept = head.rstrip(b"\r\n")

            text_line = _display_path(kept.decode("utf-8", errors="replace"))
            if truncated:
                text_line += " ... [truncated]"
            out_fh.write(f"{lineno:6d}\t{text_line}\n".encode())
            lines_written += 1

    return lines_written


def _write_diff_and_untracked_previews(
    diff_path: Path,
    git_diff_bytes: bytes,
    repo_root_res: Path,
    untracked_paths: Sequence[str],
) -> int:
    """Write diff.patch by streaming git diff bytes followed by bounded untracked previews."""
    total_lines = git_diff_bytes.count(b"\n")
    with diff_path.open("wb") as out_fh:
        if git_diff_bytes:
            out_fh.write(git_diff_bytes)
        for rel_path in untracked_paths:
            total_lines += _write_single_untracked_preview(out_fh, repo_root_res, rel_path)
    return total_lines


def _format_hazard_snippet(raw_snippet: bytes) -> str:
    """Cap and sanitize a hazard hit snippet so summary and log lines stay safe and bounded."""
    trimmed = raw_snippet.rstrip(b"\r\n")
    if len(trimmed) > _MAX_HAZARD_SNIPPET_BYTES:
        kept = _trim_incomplete_utf8(
            trimmed[:_MAX_HAZARD_SNIPPET_BYTES],
            next_byte=trimmed[_MAX_HAZARD_SNIPPET_BYTES],
        )
        return _display_path(kept.decode("utf-8", errors="replace")) + " ... [truncated]"
    return _display_path(trimmed.decode("utf-8", errors="replace"))


def _scan_files_for_languages_and_hazards(
    repo_root_res: Path,
    changed_files: Sequence[str],
    hazard_file_path: Path,
) -> FileHazardScan:
    """Scan changed files in bounded chunks for C/C++, Rust unsafe, FFI, and concurrency hits."""
    cpp_files: list[str] = []
    rust_unsafe_files: list[str] = []
    summary_hazards: list[str] = []

    has_cpp = False
    has_rust = False
    has_rust_unsafe = False
    has_ffi = False
    has_concurrency = False

    with hazard_file_path.open("w", encoding="utf-8") as hazard_fh:
        for rel_path in changed_files:
            suffix = Path(rel_path).suffix.lower()
            is_rust = suffix == ".rs"
            if suffix in _CPP_SUFFIXES:
                has_cpp = True
                cpp_files.append(rel_path)
            elif is_rust:
                has_rust = True
            elif suffix in _PROSE_AND_DATA_SUFFIXES:
                continue

            fh = _open_confined_regular_file(repo_root_res, rel_path)
            if fh is None:
                continue
            with fh:
                probe = fh.read(_BINARY_PROBE_BYTES)
                if not probe or b"\x00" in probe:
                    continue
                fh.seek(0)

                display_rel = _display_path(rel_path)
                found_rust_unsafe_in_file = False
                lineno = 0
                while True:
                    chunk = fh.readline(_HAZARD_CHUNK_BYTES)
                    if not chunk:
                        break
                    lineno += 1
                    snippet_raw = chunk[: _MAX_HAZARD_SNIPPET_BYTES + 1]
                    line_ffi = False
                    line_conc = False
                    line_unsafe = False
                    tail = b""

                    while True:
                        next_chunk = (
                            b"" if chunk.endswith(b"\n") else fh.readline(_HAZARD_CHUNK_BYTES)
                        )
                        has_left_cut = bool(tail)
                        has_right_cut = bool(next_chunk)
                        raw_bytes = tail + chunk
                        raw_window = (
                            raw_bytes if has_right_cut else raw_bytes.rstrip(b"\r\n")
                        ).decode("utf-8", errors="replace")
                        if is_rust and not found_rust_unsafe_in_file and not line_unsafe:
                            for match in _UNSAFE_WORD_RE.finditer(raw_window):
                                if (not has_left_cut or match.start() > 0) and (
                                    not has_right_cut or match.end() < len(raw_window)
                                ):
                                    line_unsafe = True
                                    break
                        if not line_ffi and any(
                            not has_left_cut or m.start() > 0
                            for m in _FFI_RE.finditer(raw_window)
                        ):
                            line_ffi = True
                        if not line_conc and _CONCURRENCY_RE.search(raw_window):
                            line_conc = True

                        if not next_chunk:
                            break
                        tail = chunk[-_HAZARD_OVERLAP_BYTES:]
                        chunk = next_chunk

                    if line_unsafe:
                        found_rust_unsafe_in_file = True
                        has_rust_unsafe = True
                        rust_unsafe_files.append(rel_path)
                    if line_ffi:
                        has_ffi = True
                    if line_conc:
                        has_concurrency = True
                    if line_ffi or line_conc:
                        hit_line = f"{display_rel}:{lineno}:{_format_hazard_snippet(snippet_raw)}"
                        hazard_fh.write(hit_line + "\n")
                        if len(summary_hazards) < _MAX_SUMMARY_HAZARD_LINES:
                            summary_hazards.append(hit_line)

    return FileHazardScan(
        cpp_files=cpp_files,
        rust_unsafe_files=rust_unsafe_files,
        has_cpp=has_cpp,
        has_rust=has_rust,
        has_rust_unsafe=has_rust_unsafe,
        has_ffi=has_ffi,
        has_concurrency=has_concurrency,
        summary_hazards=summary_hazards,
    )


def _write_lines(path: Path, lines: Sequence[str]) -> None:
    if not lines:
        path.write_bytes(b"")
        return
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_stdout_safe(text: str, *, redirect_broken_pipe_fd: bool = False) -> None:
    """Write summary text to stdout, handling narrow locales and closed pipes safely."""
    try:
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except UnicodeEncodeError:
            encoding = sys.stdout.encoding or "ascii"
            safe_text = text.encode(encoding, errors="backslashreplace").decode(
                encoding, errors="replace"
            )
            sys.stdout.write(safe_text)
            sys.stdout.flush()
    except BrokenPipeError:
        if not redirect_broken_pipe_fd:
            return
        try:
            fd = sys.stdout.fileno()
        except (AttributeError, OSError, io.UnsupportedOperation):
            return
        with contextlib.suppress(OSError):
            devnull_fd = os.open(os.devnull, os.O_WRONLY)
            try:
                if devnull_fd != fd:
                    os.dup2(devnull_fd, fd)
            finally:
                if devnull_fd != fd:
                    os.close(devnull_fd)


def _write_non_git_bundle(
    out_dir: Path,
    start_dir_res: Path,
    targets: Sequence[str],
) -> str:
    """Populate the context bundle for a non-git directory and return summary text."""
    files = _collect_non_git_files(start_dir_res, targets)
    display_files = [_display_path(f) for f in files]
    _write_lines(out_dir / "changed_files.txt", display_files)
    _write_lines(out_dir / "repo_files.txt", display_files[:_MAX_REPO_FILES_LINES])

    summary_lines = [
        f"SUMMARY_DIR={_display_path(str(out_dir))}",
        "IS_GIT_REPO=0",
        f"REPO_ROOT={_display_path(str(start_dir_res))}",
        "REVIEW_SCOPE=non-git-directory",
        f"CHANGED_FILES_COUNT={len(display_files)}",
    ]
    summary_text = "\n".join(summary_lines) + "\n"
    (out_dir / "summary.txt").write_text(summary_text, encoding="utf-8")
    return summary_text


def _is_valid_git_range(git_ctx: GitContext, raw: str, has_head: bool) -> bool:
    """Verify raw is a genuine git revision range (A..B or A...B), not a deleted path."""
    if not raw or os.path.isabs(raw) or raw.endswith("/"):
        return False
    if any(seg in (".", "..") for seg in raw.split("/")):
        return False
    if "..." in raw:
        if raw.count("...") != 1:
            return False
        left, right = raw.split("...", 1)
        if ".." in left or ".." in right:
            return False
    elif ".." in raw:
        if raw.count("..") != 1:
            return False
        left, right = raw.split("..", 1)
    else:
        return False

    if not left and not right:
        return False
    for side in (left, right):
        if not side:
            if not has_head:
                return False
            continue
        if side.startswith("-"):
            return False
        rc, _ = git_ctx.text(["rev-parse", "--verify", "-q", f"{side}^{{commit}}"])
        if rc != 0:
            return False
    return True


def _collect_commit_diff_and_files(
    git_ctx: GitContext, commit_ref: str
) -> tuple[bytes, list[str]]:
    """Collect full diff and changed files for a single commit (dereferencing tags and merges)."""
    p_rc, p_out = git_ctx.text(
        ["rev-list", "--parents", "-n", "1", f"{commit_ref}^{{commit}}", "--"]
    )
    tokens = p_out.split() if (p_rc == 0 and p_out) else []
    commit_sha = tokens[0] if tokens else commit_ref
    parents = tokens[1:] if len(tokens) > 1 else []

    if len(parents) >= 2:
        first_parent = parents[0]
        rev_range = f"{first_parent}..{commit_sha}"
        header_bytes = git_ctx.show(["--no-patch", "--format=fuller", commit_sha, "--"])
        diff_bytes, files = git_ctx.diff_and_files((rev_range,))
        return (header_bytes + diff_bytes, files)

    diff_bytes = git_ctx.show(["-U15", "--root", "--format=fuller", commit_sha, "--"])
    files = git_ctx.z_paths(
        [
            "diff-tree",
            "--no-ext-diff",
            f"-O{os.devnull}",
            "--root",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            commit_sha,
            "--",
        ]
    )
    return (diff_bytes, files)


def _resolve_git_scope(
    git_ctx: GitContext,
    start_dir_res: Path,
    *,
    mode: str,
    targets: Sequence[str],
    has_head: bool,
    base_ref: str,
    merge_base: str,
    branch_commits_ahead: int,
    has_working_changes: bool,
    untracked_files: Sequence[str],
) -> tuple[str, bytes, list[str], list[str]]:
    """Resolve (review_scope, git_diff_bytes, raw_changed_files, untracked_to_inline)."""
    if targets:
        first = targets[0]
        is_single_rev_candidate = (
            mode == "auto"
            and len(targets) == 1
            and not first.startswith("-")
            and not _exists_from_dir(start_dir_res, first)
        )
        if is_single_rev_candidate and _is_valid_git_range(git_ctx, first, has_head):
            diff_bytes, files = git_ctx.diff_and_files((first,))
            return (f"range:{_display_path(first)}", diff_bytes, files, [])

        if is_single_rev_candidate:
            verify_rc, _ = git_ctx.text(["rev-parse", "--verify", "-q", f"{first}^{{commit}}"])
            if verify_rc == 0:
                is_branch_ref = any(
                    git_ctx.run(["show-ref", "--verify", "-q", ref]).returncode == 0
                    for ref in (
                        f"refs/heads/{first}",
                        f"refs/remotes/{first}",
                        f"refs/remotes/origin/{first}",
                    )
                )
                if is_branch_ref and has_head:
                    ref_mb = first
                    mb_rc, mb_out = git_ctx.text(["merge-base", first, "HEAD"])
                    if mb_rc == 0 and mb_out:
                        ref_mb = mb_out
                    rev_range = f"{ref_mb}..HEAD"
                    diff_bytes, files = git_ctx.diff_and_files((rev_range,))
                    scope = (
                        f"branch-vs:{_display_path(first)} "
                        f"({_display_path(rev_range)})"
                    )
                    return (scope, diff_bytes, files, [])

                diff_bytes, files = _collect_commit_diff_and_files(git_ctx, first)
                return (f"commit:{_display_path(first)}", diff_bytes, files, [])

        scope_label = f"paths:{' '.join(_display_path(t) for t in targets)}"
        pathspecs: list[str] = []
        for raw_t in targets:
            rel_spec = _relativize_git_pathspec(git_ctx.repo_root, start_dir_res, raw_t)
            if rel_spec is not None:
                pathspecs.append(rel_spec)
        if not pathspecs:
            if mode == "staged":
                return (f"{scope_label} (staged)", b"", [], [])
            if mode == "working":
                return (f"{scope_label} (working-tree)", b"", [], [])
            if mode == "branch_and_working" and merge_base:
                return (f"{scope_label} (branch-and-working)", b"", [], [])
            return (scope_label, b"", [], [])

        if mode == "staged":
            diff_bytes, files = git_ctx.diff_and_files(("--cached",), pathspecs)
            return (f"{scope_label} (staged)", diff_bytes, files, [])

        if mode == "working":
            matching_untracked = git_ctx.untracked_paths(pathspecs)
            diff_bytes, files = git_ctx.working_diff_and_files(
                has_head, matching_untracked, pathspecs
            )
            return (f"{scope_label} (working-tree)", diff_bytes, files, matching_untracked)

        if mode == "branch_and_working" and merge_base:
            matching_untracked = git_ctx.untracked_paths(pathspecs)
            diff_bytes, files = git_ctx.diff_and_files((merge_base,), pathspecs)
            files.extend(matching_untracked)
            return (
                f"{scope_label} (branch-and-working)",
                diff_bytes,
                files,
                matching_untracked,
            )

        matching_untracked = git_ctx.untracked_paths(pathspecs)
        path_work_diff, path_work_files = git_ctx.working_diff_and_files(
            has_head, matching_untracked, pathspecs
        )
        if path_work_files:
            if merge_base and branch_commits_ahead > 0:
                bw_diff, bw_files = git_ctx.diff_and_files((merge_base,), pathspecs)
                bw_files.extend(matching_untracked)
                if bw_files:
                    return (scope_label, bw_diff, bw_files, matching_untracked)
            return (scope_label, path_work_diff, path_work_files, matching_untracked)

        if merge_base and branch_commits_ahead > 0:
            br_diff, br_files = git_ctx.diff_and_files((f"{merge_base}..HEAD",), pathspecs)
            if br_files:
                return (scope_label, br_diff, br_files, [])

        files = _walk_confined_git_paths(git_ctx.repo_root, pathspecs)
        return (scope_label, b"", files, [])

    if mode == "staged":
        diff_bytes, files = git_ctx.diff_and_files(("--cached",))
        return ("staged", diff_bytes, files, [])

    if mode == "working":
        diff_bytes, files = git_ctx.working_diff_and_files(has_head, untracked_files)
        return ("working-tree", diff_bytes, files, list(untracked_files))

    if mode == "branch_and_working" and merge_base:
        diff_bytes, files = git_ctx.diff_and_files((merge_base,))
        files.extend(untracked_files)
        return (
            f"branch-and-working ({_display_path(merge_base)}..working-tree)",
            diff_bytes,
            files,
            list(untracked_files),
        )

    if has_working_changes and branch_commits_ahead > 0 and merge_base:
        bw_diff, bw_files = git_ctx.diff_and_files((merge_base,))
        bw_files.extend(untracked_files)
        if bw_files:
            scope = (
                f"branch-and-working ({_display_path(merge_base)}..working-tree; "
                f"includes {branch_commits_ahead} branch commit(s) + uncommitted edits)"
            )
            return (scope, bw_diff, bw_files, list(untracked_files))

    if has_working_changes:
        diff_bytes, files = git_ctx.working_diff_and_files(has_head, untracked_files)
        return (
            "working-tree (staged + unstaged + untracked)",
            diff_bytes,
            files,
            list(untracked_files),
        )

    if merge_base and branch_commits_ahead > 0:
        rev_range = f"{merge_base}..HEAD"
        diff_bytes, files = git_ctx.diff_and_files((rev_range,))
        scope = (
            f"branch ({_display_path(rev_range)} vs {_display_path(base_ref)}; "
            f"{branch_commits_ahead} commit(s))"
        )
        return (scope, diff_bytes, files, [])

    if has_head:
        parent_rc, _ = git_ctx.text(["rev-parse", "--verify", "-q", "HEAD~1"])
        if parent_rc == 0:
            diff_bytes, files = git_ctx.diff_and_files(("HEAD~1..HEAD",))
            return ("latest-commit (HEAD~1..HEAD)", diff_bytes, files, [])

        diff_bytes = git_ctx.show(["-U15", "--root", "--format=fuller", "HEAD", "--"])
        files = git_ctx.z_paths(["ls-tree", "--name-only", "-r", "-z", "HEAD"])
        return ("initial-commit (HEAD)", diff_bytes, files, [])

    return ("empty-repo", b"", [], [])


def _write_git_bundle(
    out_dir: Path,
    repo_root_res: Path,
    start_dir_res: Path,
    *,
    mode: str,
    targets: Sequence[str],
) -> str:
    """Populate the full git-mode context bundle and return summary text."""
    config_overrides = _discover_config_overrides(repo_root_res)
    git_ctx = GitContext(repo_root=repo_root_res, config_overrides=config_overrides)

    br_rc, br_out = git_ctx.text(["branch", "--show-current"])
    current_branch = _display_path(br_out) if (br_rc == 0 and br_out) else "DETACHED"

    head_rc, _ = git_ctx.text(["rev-parse", "--verify", "-q", "HEAD"])
    has_head = head_rc == 0

    base_ref = _detect_base_ref(git_ctx)
    merge_base = ""
    branch_commits_ahead = 0
    if has_head and base_ref:
        mb_rc, mb_out = git_ctx.text(["merge-base", base_ref, "HEAD"])
        if mb_rc == 0 and mb_out:
            merge_base = mb_out
            cnt_rc, cnt_out = git_ctx.text(["rev-list", "--count", f"{merge_base}..HEAD"])
            if cnt_rc == 0 and cnt_out.isdigit():
                branch_commits_ahead = int(cnt_out)

    unstaged_files = git_ctx.diff_files()
    staged_files = git_ctx.diff_files(("--cached",))
    untracked_files = git_ctx.untracked_paths()
    _write_lines(out_dir / "untracked_files.txt", [_display_path(f) for f in untracked_files])

    unstaged_count = len(unstaged_files)
    staged_count = len(staged_files)
    untracked_count = len(untracked_files)
    has_working_changes = (unstaged_count + staged_count + untracked_count) > 0

    review_scope, git_diff_bytes, raw_changed_files, untracked_to_inline = _resolve_git_scope(
        git_ctx,
        start_dir_res,
        mode=mode,
        targets=targets,
        has_head=has_head,
        base_ref=base_ref,
        merge_base=merge_base,
        branch_commits_ahead=branch_commits_ahead,
        has_working_changes=has_working_changes,
        untracked_files=untracked_files,
    )

    changed_files = sorted({f for f in raw_changed_files if f})
    display_changed = [_display_path(f) for f in changed_files]
    _write_lines(out_dir / "changed_files.txt", display_changed)

    tracked_files = sorted({f for f in git_ctx.z_paths(["ls-files", "-z"]) if f})
    repo_inventory = [_display_path(f) for f in tracked_files[:_MAX_REPO_FILES_LINES]]
    if len(tracked_files) > _MAX_REPO_FILES_LINES:
        repo_inventory.append(
            f"... ({len(tracked_files) - _MAX_REPO_FILES_LINES} more tracked files omitted)"
        )
    _write_lines(out_dir / "repo_files.txt", repo_inventory)

    diff_lines = _write_diff_and_untracked_previews(
        out_dir / "diff.patch",
        git_diff_bytes,
        repo_root_res,
        untracked_to_inline,
    )
    diff_analysis = _analyze_diff_bytes(git_diff_bytes)

    scan = _scan_files_for_languages_and_hazards(
        repo_root_res,
        changed_files,
        out_dir / "hazard_hits.txt",
    )
    has_rust_unsafe = scan.has_rust_unsafe or diff_analysis.has_rust_unsafe

    summary_parts: list[str] = [
        f"SUMMARY_DIR={_display_path(str(out_dir))}",
        "IS_GIT_REPO=1",
        f"REPO_ROOT={_display_path(str(repo_root_res))}",
        f"CURRENT_BRANCH={current_branch}",
        f"BASE_REF={_display_path(base_ref) if base_ref else 'none'}",
        f"MERGE_BASE={_display_path(merge_base) if merge_base else 'none'}",
        f"BRANCH_COMMITS_AHEAD={branch_commits_ahead}",
        (
            f"HAS_WORKING_CHANGES={1 if has_working_changes else 0} "
            f"(staged={staged_count}, unstaged={unstaged_count}, untracked={untracked_count})"
        ),
        f"REVIEW_SCOPE={_display_path(review_scope)}",
        f"CHANGED_FILES_COUNT={len(changed_files)}",
        (
            f"DIFF_PATCH_LINES={diff_lines} "
            f"(+{diff_analysis.added_lines} / -{diff_analysis.removed_lines})"
        ),
        f"HAS_CPP={1 if scan.has_cpp else 0}",
        f"HAS_RUST={1 if scan.has_rust else 0}",
        f"HAS_RUST_UNSAFE={1 if has_rust_unsafe else 0}",
        f"HAS_FFI={1 if scan.has_ffi else 0}",
        f"HAS_CONCURRENCY_HAZARDS={1 if scan.has_concurrency else 0}",
        "",
        "--- CHANGED FILES ---",
    ]
    summary_parts.extend(display_changed)

    if scan.cpp_files:
        summary_parts.append("")
        summary_parts.append("--- C / C++ / OBJC++ FILES TOUCHED ---")
        summary_parts.extend(_display_path(f) for f in scan.cpp_files)

    if scan.rust_unsafe_files:
        summary_parts.append("")
        summary_parts.append("--- RUST FILES CONTAINING UNSAFE ---")
        summary_parts.extend(_display_path(f) for f in scan.rust_unsafe_files)

    if scan.summary_hazards:
        summary_parts.append("")
        summary_parts.append("--- LOW-LEVEL / SAFETY / CONCURRENCY MARKERS (FIRST 80) ---")
        summary_parts.extend(scan.summary_hazards)

    summary_text = "\n".join(summary_parts) + "\n"
    (out_dir / "summary.txt").write_text(summary_text, encoding="utf-8")
    return summary_text


def _rev_parse_resolved_dir(cwd: Path, flag: str) -> Path | None:
    res = _run_git(cwd, ["rev-parse", flag])
    if res.returncode != 0 or not res.stdout.strip():
        return None
    candidate = Path(os.fsdecode(res.stdout.rstrip(b"\r\n")))
    try:
        if not candidate.is_dir():
            return None
        return candidate.resolve(strict=True)
    except OSError:
        return None


def _detect_git_worktree(start_dir_res: Path) -> Path | None:
    """Return resolved git repository toplevel if start_dir_res is cleanly confined within it."""
    wt_res = _run_git(start_dir_res, ["rev-parse", "--is-inside-work-tree"])
    if wt_res.returncode != 0:
        err_text = wt_res.stderr.decode("utf-8", errors="replace").strip()
        if "dubious ownership" in err_text or "safe.directory" in err_text:
            print(
                f"WARNING: git reported repository ownership error: {_display_path(err_text)}",
                file=sys.stderr,
            )
        return None
    if wt_res.stdout.strip() != b"true":
        return None

    top_res = _rev_parse_resolved_dir(start_dir_res, "--show-toplevel")
    start_git_dir = _rev_parse_resolved_dir(start_dir_res, "--absolute-git-dir")
    if top_res is None or start_git_dir is None:
        return None
    if start_dir_res != top_res and not start_dir_res.is_relative_to(top_res):
        return None

    # Verify round-trip from top_res so a hostile core.worktree pointing upward cannot
    # escape the repository boundary into an unrelated parent directory.
    if top_res != start_dir_res:
        top_wt = _run_git(top_res, ["rev-parse", "--is-inside-work-tree"])
        if top_wt.returncode != 0 or top_wt.stdout.strip() != b"true":
            return None
        top_top = _rev_parse_resolved_dir(top_res, "--show-toplevel")
        top_git_dir = _rev_parse_resolved_dir(top_res, "--absolute-git-dir")
        if top_top != top_res or top_git_dir != start_git_dir:
            return None

    return top_res


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for collect_review_context.py."""
    parser = argparse.ArgumentParser(
        prog="collect_review_context.py",
        description=(
            "Collect read-only git diff, changed files, and safety hazard signals "
            "for multi-agent code review."
        ),
    )
    parser.add_argument(
        "--repo",
        "-C",
        dest="repo",
        metavar="DIR",
        default=None,
        help="Target repository or directory to inspect (defaults to current directory).",
    )
    parser.add_argument(
        "--out-parent",
        dest="out_parent",
        metavar="DIR",
        default=None,
        help="Parent directory for the generated context bundle (defaults to /tmp).",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--staged",
        action="store_const",
        dest="mode",
        const="staged",
        help="Review staged index changes only.",
    )
    mode_group.add_argument(
        "--working",
        action="store_const",
        dest="mode",
        const="working",
        help="Review working-tree changes (staged, unstaged, and untracked).",
    )
    mode_group.add_argument(
        "--branch-and-working",
        action="store_const",
        dest="mode",
        const="branch_and_working",
        help="Review branch commits ahead of base together with working-tree edits.",
    )
    parser.set_defaults(mode="auto")
    parser.add_argument(
        "targets",
        nargs="*",
        help="Optional git range, commit/branch ref, or file/directory paths.",
    )
    return parser


def _run_main(argv: Sequence[str] | None, *, redirect_broken_pipe_fd: bool) -> int:
    """Execute the collector CLI with optional CLI-only broken-pipe FD redirection."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.repo is not None:
        start_dir_res = _validate_accessible_dir(args.repo, "--repo", need_write=False)
        if start_dir_res is None:
            return 1
    else:
        try:
            start_dir_res = Path.cwd().resolve(strict=True)
        except OSError as exc:
            print(f"ERROR: failed to resolve current working directory: {exc}", file=sys.stderr)
            return 1

    if args.out_parent is not None:
        out_parent = _validate_accessible_dir(args.out_parent, "--out-parent", need_write=True)
        if out_parent is None:
            return 1
    else:
        default_tmp = Path("/tmp")
        if default_tmp.is_dir() and os.access(default_tmp, os.W_OK | os.X_OK):
            out_parent = default_tmp
        else:
            out_parent = Path(tempfile.gettempdir())

    try:
        out_dir = Path(tempfile.mkdtemp(prefix="agy-review.", dir=str(out_parent)))
    except OSError as exc:
        print(f"ERROR: failed to create temporary review bundle directory: {exc}", file=sys.stderr)
        return 1

    try:
        for filename in (
            "diff.patch",
            "changed_files.txt",
            "repo_files.txt",
            "untracked_files.txt",
            "hazard_hits.txt",
            "summary.txt",
        ):
            (out_dir / filename).write_bytes(b"")

        repo_root_res = _detect_git_worktree(start_dir_res)
        if repo_root_res is None:
            summary_text = _write_non_git_bundle(out_dir, start_dir_res, args.targets)
        else:
            summary_text = _write_git_bundle(
                out_dir,
                repo_root_res,
                start_dir_res,
                mode=args.mode,
                targets=args.targets,
            )
    except BaseException:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise

    _write_stdout_safe(summary_text, redirect_broken_pipe_fd=redirect_broken_pipe_fd)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint returning an integer exit code without mutating host OS FDs."""
    return _run_main(argv, redirect_broken_pipe_fd=False)


if __name__ == "__main__":
    raise SystemExit(_run_main(None, redirect_broken_pipe_fd=True))
