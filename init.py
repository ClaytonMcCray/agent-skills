#!/usr/bin/env python3
"""Multi-machine installer, status checker, skill adopter, and repo doctor for agent-skills."""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import py_compile
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote


@dataclass(frozen=True)
class HarnessSpec:
    """Configuration describing a harness adapter and its local machine paths."""

    name: str
    repo_rel_dir: str
    config_rel_dir: str
    skills_json_name: str
    skills_dir_name: str


HARNESS_REGISTRY: dict[str, HarnessSpec] = {
    "agy": HarnessSpec(
        name="agy",
        repo_rel_dir="harnesses/agy",
        config_rel_dir=".gemini/config",
        skills_json_name="skills.json",
        skills_dir_name="skills",
    ),
}

KEBAB_CASE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FM_KEY_RE = re.compile(r"^([A-Za-z0-9_.-]+)[ \t]*:(?:[ \t]+(.*)|[ \t]*)$")
BLOCK_SCALAR_RE = re.compile(
    r"^([|>])(?:([+-])([1-9]?)|([1-9])([+-]?))?[ \t]*(?:#.*)?$"
)
FENCE_OPEN_RE = re.compile(
    r"^[ \t]*((?:>[ \t]*)*)(?:(?:[-*+]|\d+[.)])[ \t]+)?(`{3,}|~{3,})(.*)$"
)
URI_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
HARDCODED_USER_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?<!:/)"
    r"(?:file://localhost|/System/Volumes/Data|/private|/var)?"
    r"/(?:Users|home)/(?![<{$])([A-Za-z0-9_][A-Za-z0-9._-]*)"
)

PRUNED_ENTRY_NAMES = frozenset(
    {
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".venv",
        "venv",
        "node_modules",
        ".DS_Store",
    }
)
PATH_SCAN_SUFFIXES = frozenset({".md", ".py", ".json"})
COMMONMARK_ASCII_PUNCT = frozenset(r"""!"#$%&'()*+,-./:;<=>?@[\]^_`{|}~""")
YAML_TOKEN_BOUNDARIES = frozenset({" ", "\t", "[", "{", ",", ":"})
MAX_FLOW_DEPTH = 32

_UMASK_LOCK = threading.Lock()


class FrontmatterError(ValueError):
    """Raised when a SKILL.md YAML frontmatter block is missing or malformed."""


def safe_resolve(path: Path, *, strict: bool = False) -> Path | None:
    """Resolve a path while catching broken symlinks, loops, NUL bytes, and OS errors."""
    try:
        return path.resolve(strict=strict)
    except (OSError, RuntimeError, ValueError):
        return None


def _safe_is_symlink(path: Path) -> bool:
    try:
        return path.is_symlink()
    except (OSError, ValueError):
        return False


def _safe_exists(path: Path) -> bool:
    try:
        return path.exists()
    except (OSError, ValueError):
        return False


def _safe_is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except (OSError, ValueError):
        return False


def _safe_is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except (OSError, ValueError):
        return False


def _path_exists_or_symlink(path: Path) -> bool:
    return _safe_is_symlink(path) or _safe_exists(path)


def _safe_readlink(path: Path) -> str:
    try:
        return os.readlink(path)
    except OSError as exc:
        return f"<unreadable: {exc}>"


def _paths_overlap(path_a: Path, path_b: Path) -> bool:
    return path_a == path_b or path_a.is_relative_to(path_b) or path_b.is_relative_to(path_a)


def _is_pruned_name(name: str) -> bool:
    return name.lower() == ".git" or name in PRUNED_ENTRY_NAMES


def _adopt_ignore(_src_dir: str, names: list[str]) -> set[str]:
    return {name for name in names if _is_pruned_name(name) or name.startswith(".adopt-")}


def _iter_visible_entries_checked(directory: Path) -> tuple[list[Path], str | None]:
    """Return sorted visible direct children of directory plus any OSError message."""
    try:
        if not directory.is_dir():
            return ([], None)
        children = sorted(directory.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        return ([], f"{directory}: failed to list directory: {exc}")
    visible = [
        entry
        for entry in children
        if not entry.name.startswith(".") and entry.name != "__pycache__"
    ]
    return (visible, None)


def _iter_visible_entries(directory: Path) -> list[Path]:
    entries, _ = _iter_visible_entries_checked(directory)
    return entries


def _agy_config_paths(home: Path) -> tuple[Path, Path, Path]:
    """Return (config_dir, skills_json_path, agy_skills_dir) for the Antigravity harness."""
    spec = HARNESS_REGISTRY["agy"]
    config_dir = home / spec.config_rel_dir
    return (
        config_dir,
        config_dir / spec.skills_json_name,
        config_dir / spec.skills_dir_name,
    )


def _validate_dir_ancestors(target_dir: Path, *, stop_at: Path | None = None) -> str | None:
    """Verify every existing/symlinked ancestor from stop_at down to target_dir is a directory."""
    chain: list[Path] = []
    if stop_at is not None:
        cur = target_dir
        while True:
            chain.append(cur)
            if cur == stop_at or cur.parent == cur:
                break
            cur = cur.parent
        chain.reverse()
    else:
        chain = [*reversed(target_dir.parents), target_dir]

    for comp in chain:
        try:
            st = os.lstat(comp)
        except FileNotFoundError:
            break
        except (OSError, ValueError) as exc:
            return f"inaccessible ancestor {comp}: {exc}"

        if stat.S_ISLNK(st.st_mode):
            resolved = safe_resolve(comp, strict=True)
            if resolved is None:
                return f"broken or circular symlink ancestor: {comp}"
            if not _safe_is_dir(resolved):
                return f"symlink ancestor points to non-directory: {comp} -> {resolved}"
        elif not stat.S_ISDIR(st.st_mode):
            return f"non-directory ancestor: {comp}"
    return None


def _validate_in_repo_container(repo_root_res: Path, rel_parts: Sequence[str]) -> str | None:
    """Verify every existing path component below repo_root_res is a real non-symlink dir."""
    cur = repo_root_res
    for part in rel_parts:
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            break
        except OSError as exc:
            return f"inaccessible repository container {cur}: {exc}"
        if stat.S_ISLNK(st.st_mode):
            return f"repository container must not be a symlink: {cur}"
        if not stat.S_ISDIR(st.st_mode):
            return f"repository container is not a directory: {cur}"
    return None


def _resolve_confined_relative_path(
    start_dir: Path,
    rel_target: str,
    repo_root_res: Path,
    *,
    overlay: tuple[Path, Path] | None = None,
    forbidden_dir_res: Path | None = None,
    max_hops: int = 40,
) -> tuple[Path | None, str | None, bool]:
    """Resolve a relative path step-by-step while expanding symlinks and enforcing confinement.

    Returns (resolved_virtual_path, error_message, is_missing_only).
    """
    if not rel_target:
        return (None, "empty relative path", False)
    if "\x00" in rel_target:
        return (None, "path contains NUL byte", False)
    if os.path.isabs(rel_target):
        return (None, f"target must be relative, got absolute {rel_target!r}", False)
    if start_dir != repo_root_res and not start_dir.is_relative_to(repo_root_res):
        return (None, "start directory is outside repository", False)

    def map_to_disk(vpath: Path) -> tuple[Path, bool]:
        if overlay is not None:
            vroot, sroot = overlay
            if vpath == vroot or vpath.is_relative_to(vroot):
                return (sroot / vpath.relative_to(vroot), True)
        return (vpath, False)

    def check_forbidden(disk_path: Path, via_overlay: bool) -> bool:
        return (
            not via_overlay
            and forbidden_dir_res is not None
            and (disk_path == forbidden_dir_res or disk_path.is_relative_to(forbidden_dir_res))
        )

    cur = start_dir
    queue: deque[str] = deque(rel_target.split("/"))
    hops = 0

    while queue:
        part = queue.popleft()

        cur_disk, cur_via_overlay = map_to_disk(cur)
        if check_forbidden(cur_disk, cur_via_overlay):
            return (None, "path enters staging directory", False)
        try:
            cur_st = os.lstat(cur_disk)
        except OSError:
            return (None, f"missing or inaccessible directory component for {rel_target!r}", True)
        if not stat.S_ISDIR(cur_st.st_mode):
            return (None, f"cannot traverse non-directory component for {rel_target!r}", False)

        if part in ("", "."):
            continue

        if part == "..":
            if cur == repo_root_res:
                return (None, f"path climbs outside repository ({rel_target!r})", False)
            cur = cur.parent
            continue

        nxt = cur / part
        nxt_disk, nxt_via_overlay = map_to_disk(nxt)
        if check_forbidden(nxt_disk, nxt_via_overlay):
            return (None, f"path resolves into staging directory ({rel_target!r})", False)
        try:
            nxt_st = os.lstat(nxt_disk)
        except FileNotFoundError:
            return (None, f"target does not exist ({rel_target!r})", True)
        except OSError as exc:
            return (None, f"target is inaccessible ({rel_target!r}): {exc}", False)

        if stat.S_ISLNK(nxt_st.st_mode):
            hops += 1
            if hops > max_hops:
                return (None, f"circular or overly deep symlink chain ({rel_target!r})", False)
            try:
                link_target = os.readlink(nxt_disk)
            except OSError as exc:
                return (None, f"failed to read symlink ({rel_target!r}): {exc}", False)
            if not link_target or "\x00" in link_target or os.path.isabs(link_target):
                return (
                    None,
                    f"chained symlink has non-relative target {link_target!r}",
                    False,
                )
            for link_part in reversed(link_target.split("/")):
                queue.appendleft(link_part)
            continue

        cur = nxt

    cur_disk, cur_via_overlay = map_to_disk(cur)
    if check_forbidden(cur_disk, cur_via_overlay):
        return (None, f"path resolves into staging directory ({rel_target!r})", False)
    try:
        os.lstat(cur_disk)
    except FileNotFoundError:
        return (None, f"target does not exist ({rel_target!r})", True)
    except OSError as exc:
        return (None, f"target is inaccessible ({rel_target!r}): {exc}", False)

    return (cur, None, False)


def discover_skill_dirs(parent: Path) -> list[Path]:
    """Return sorted non-hidden direct child directories of parent."""
    return [entry for entry in _iter_visible_entries(parent) if _safe_is_dir(entry)]


def discover_agy_repo_skills(repo_root: Path) -> dict[str, Path]:
    """Map skill names exposed to Antigravity (shared + harnesses/agy) to repo dirs."""
    skills: dict[str, Path] = {}
    for skill_dir in discover_skill_dirs(repo_root / "shared"):
        skills[skill_dir.name] = skill_dir
    agy_spec = HARNESS_REGISTRY["agy"]
    for skill_dir in discover_skill_dirs(repo_root / agy_spec.repo_rel_dir):
        skills[skill_dir.name] = skill_dir
    return skills


def _duplicate_agy_skill_names(repo_root: Path) -> set[str]:
    shared_names = {p.name for p in discover_skill_dirs(repo_root / "shared")}
    agy_names = {
        p.name for p in discover_skill_dirs(repo_root / HARNESS_REGISTRY["agy"].repo_rel_dir)
    }
    return shared_names & agy_names


def format_skills_json_path(target: Path, home: Path) -> str:
    """Format a target path as ~/... when under home, else as an absolute POSIX path."""
    home_res = safe_resolve(home)
    target_res = safe_resolve(target)
    if home_res is None:
        home_res = Path(os.path.abspath(str(home)))
    if target_res is None:
        target_res = Path(os.path.abspath(str(target)))
    if target_res == home_res:
        return "~"
    if target_res.is_relative_to(home_res):
        return f"~/{target_res.relative_to(home_res).as_posix()}"
    return target_res.as_posix()


def resolve_configured_entry_path(raw_path: str, home: Path) -> Path | None:
    """Resolve a skills.json entry path against home; returns None for workspace-relative paths."""
    if "\x00" in raw_path:
        return None
    if raw_path == "~":
        return safe_resolve(home)
    if raw_path.startswith("~/"):
        rel = raw_path[2:].lstrip("/")
        return safe_resolve(home / rel) if rel else safe_resolve(home)
    if raw_path.startswith("/"):
        return safe_resolve(Path(raw_path))
    return None


def unique_backup_path(base_dir: Path, stem: str, timestamp: str) -> Path:
    """Return a non-colliding backup path of the form <stem>.bak.<ts>[-N]."""
    candidate = base_dir / f"{stem}.bak.{timestamp}"
    if not _path_exists_or_symlink(candidate):
        return candidate
    suffix = 1
    while True:
        candidate = base_dir / f"{stem}.bak.{timestamp}-{suffix}"
        if not _path_exists_or_symlink(candidate):
            return candidate
        suffix += 1


def _create_unique_backup_dir(base_dir: Path, stem: str, timestamp: str) -> Path:
    """Atomically reserve and create a unique backup directory using exist_ok=False."""
    base_dir.mkdir(parents=True, exist_ok=True)
    suffix = 0
    while True:
        name = f"{stem}.bak.{timestamp}" if suffix == 0 else f"{stem}.bak.{timestamp}-{suffix}"
        candidate = base_dir / name
        try:
            candidate.mkdir(parents=False, exist_ok=False)
            return candidate
        except FileExistsError:
            suffix += 1


def _create_unique_backup_file_slot(base_dir: Path, stem: str, timestamp: str) -> Path:
    """Atomically reserve a unique backup file path using O_CREAT | O_EXCL."""
    base_dir.mkdir(parents=True, exist_ok=True)
    suffix = 0
    while True:
        name = f"{stem}.bak.{timestamp}" if suffix == 0 else f"{stem}.bak.{timestamp}-{suffix}"
        candidate = base_dir / name
        try:
            fd = os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            suffix += 1
            continue
        os.close(fd)
        return candidate


def _rmtree_owned_dir(root: Path) -> None:
    """Make owned non-symlink subdirs writable so rmtree cleans read-only staging dirs."""
    if not _path_exists_or_symlink(root):
        return
    if not _safe_is_symlink(root):
        with contextlib.suppress(OSError):
            os.chmod(root, stat.S_IMODE(os.lstat(root).st_mode) | stat.S_IRWXU)
        for dirpath_str, dirnames, _filenames in os.walk(root, followlinks=False):
            dirpath = Path(dirpath_str)
            kept: list[str] = []
            for dirname in dirnames:
                child = dirpath / dirname
                if not _safe_is_symlink(child):
                    with contextlib.suppress(OSError):
                        os.chmod(child, stat.S_IMODE(os.lstat(child).st_mode) | stat.S_IRWXU)
                    kept.append(dirname)
            dirnames[:] = kept
    shutil.rmtree(root, ignore_errors=True)


def _leading_spaces(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _preceded_by_odd_backslashes(text: str, pos: int) -> bool:
    count = 0
    idx = pos - 1
    while idx >= 0 and text[idx] == "\\":
        count += 1
        idx -= 1
    return (count % 2) == 1


def _strip_inline_comment(value: str) -> str:
    """Remove an unquoted inline YAML comment (# preceded by whitespace)."""
    in_single = False
    in_double = False
    escaped = False
    idx = 0
    length = len(value)
    while idx < length:
        ch = value[idx]
        if escaped:
            escaped = False
            idx += 1
            continue
        if in_double:
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
            idx += 1
            continue
        if in_single:
            if ch == "'":
                if idx + 1 < length and value[idx + 1] == "'":
                    idx += 2
                    continue
                in_single = False
            idx += 1
            continue
        if ch == '"' and (idx == 0 or value[idx - 1] in YAML_TOKEN_BOUNDARIES):
            in_double = True
        elif ch == "'" and (idx == 0 or value[idx - 1] in YAML_TOKEN_BOUNDARIES):
            in_single = True
        elif ch == "#" and (idx == 0 or value[idx - 1] in (" ", "\t")):
            return value[:idx].rstrip(" \t")
        idx += 1
    return value.rstrip(" \t")


def _consume_quoted_scalar(
    first_raw: str, lines: list[str], next_idx: int, start_lineno: int, *, min_indent: int = 0
) -> tuple[str, int]:
    """Incrementally consume a single- or multi-line quoted YAML scalar in O(N)."""
    quote = first_raw[0]
    chars: list[str] = []
    escape_map = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "/": "/"}

    def scan_segment(segment: str, start_pos: int) -> tuple[bool, bool]:
        idx = start_pos
        length = len(segment)
        if quote == '"':
            while idx < length:
                ch = segment[idx]
                if ch == "\\":
                    if idx + 1 >= length:
                        return (False, True)
                    esc = segment[idx + 1]
                    chars.append(escape_map.get(esc, esc))
                    idx += 2
                    continue
                if ch == '"':
                    trailing = segment[idx + 1 :].strip()
                    if trailing and not trailing.startswith("#"):
                        raise FrontmatterError(
                            f"line {start_lineno}: unexpected trailing content after closing quote"
                        )
                    return (True, False)
                chars.append(ch)
                idx += 1
            return (False, False)

        while idx < length:
            ch = segment[idx]
            if ch == "'":
                if idx + 1 < length and segment[idx + 1] == "'":
                    chars.append("'")
                    idx += 2
                    continue
                trailing = segment[idx + 1 :].strip()
                if trailing and not trailing.startswith("#"):
                    raise FrontmatterError(
                        f"line {start_lineno}: unexpected trailing content after closing quote"
                    )
                return (True, False)
            chars.append(ch)
            idx += 1
        return (False, False)

    closed, escaped_eol = scan_segment(first_raw, 1)
    if closed:
        return ("".join(chars), next_idx)

    idx = next_idx
    total = len(lines)
    pending_blanks = 0

    while idx < total:
        line = lines[idx]
        stripped = line.strip()
        if not stripped:
            pending_blanks += 1
            idx += 1
            continue
        if _leading_spaces(line) <= min_indent:
            break

        if escaped_eol:
            escaped_eol = False
            pending_blanks = 0
        elif pending_blanks > 0:
            chars.append("\n" * pending_blanks)
            pending_blanks = 0
        else:
            chars.append(" ")

        idx += 1
        closed, escaped_eol = scan_segment(stripped, 0)
        if closed:
            return ("".join(chars), idx)

    kind = "double" if quote == '"' else "single"
    raise FrontmatterError(f"line {start_lineno}: unterminated {kind}-quoted string")


def _split_flow_items(inner: str, lineno: int, *, _depth: int = 0) -> list[object]:
    """Split top-level comma-separated items inside a YAML flow sequence [...]."""
    if _depth >= MAX_FLOW_DEPTH:
        raise FrontmatterError(
            f"line {lineno}: flow nesting exceeds maximum depth ({MAX_FLOW_DEPTH})"
        )
    if not inner.strip():
        return []
    items: list[object] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    bracket_depth = 0
    brace_depth = 0
    idx = 0
    length = len(inner)

    while idx < length:
        ch = inner[idx]
        if escaped:
            buf.append(ch)
            escaped = False
            idx += 1
            continue
        if in_double:
            buf.append(ch)
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
            idx += 1
            continue
        if in_single:
            if ch == "'":
                if idx + 1 < length and inner[idx + 1] == "'":
                    buf.append("''")
                    idx += 2
                    continue
                in_single = False
            buf.append(ch)
            idx += 1
            continue

        at_boundary = not buf or buf[-1] in YAML_TOKEN_BOUNDARIES
        if ch == '"' and at_boundary:
            in_double = True
            buf.append(ch)
        elif ch == "'" and at_boundary:
            in_single = True
            buf.append(ch)
        elif ch == "[":
            bracket_depth += 1
            buf.append(ch)
        elif ch == "]":
            bracket_depth = max(0, bracket_depth - 1)
            buf.append(ch)
        elif ch == "{":
            brace_depth += 1
            buf.append(ch)
        elif ch == "}":
            brace_depth = max(0, brace_depth - 1)
            buf.append(ch)
        elif ch == "," and bracket_depth == 0 and brace_depth == 0:
            token = "".join(buf).strip()
            if token:
                items.append(_parse_inline_scalar(token, lineno, _depth=_depth + 1))
            buf = []
        else:
            buf.append(ch)
        idx += 1

    trailing = "".join(buf).strip()
    if trailing:
        items.append(_parse_inline_scalar(trailing, lineno, _depth=_depth + 1))
    return items


def _consume_flow_collection(
    first_raw: str,
    lines: list[str],
    next_idx: int,
    start_lineno: int,
    *,
    open_char: str,
    close_char: str,
) -> tuple[str, int]:
    """Incrementally consume a balanced single- or multi-line YAML flow [...] or {...} in O(N)."""
    inner_chars: list[str] = []
    depth = 0
    in_single = False
    in_double = False
    escaped = False
    escaped_eol = False
    idx = next_idx
    total = len(lines)
    current_seg = first_raw
    first_line = True

    while True:
        pos = 0
        seg_len = len(current_seg)
        if escaped_eol:
            if not current_seg:
                if idx >= total:
                    raise FrontmatterError(
                        f"line {start_lineno}: unterminated flow {open_char!r}"
                    )
                current_seg = lines[idx].strip()
                idx += 1
                continue
            escaped_eol = False
        elif not first_line and inner_chars and inner_chars[-1] not in (" ", "\t", "\n"):
            inner_chars.append(" ")
        first_line = False

        while pos < seg_len:
            ch = current_seg[pos]
            if escaped:
                inner_chars.append(ch)
                escaped = False
                pos += 1
                continue
            if in_double:
                inner_chars.append(ch)
                if ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_double = False
                pos += 1
                continue
            if in_single:
                if ch == "'":
                    if pos + 1 < seg_len and current_seg[pos + 1] == "'":
                        inner_chars.append("''")
                        pos += 2
                        continue
                    in_single = False
                inner_chars.append(ch)
                pos += 1
                continue

            prev_ch = current_seg[pos - 1] if pos > 0 else " "
            if ch == "#" and (pos == 0 or prev_ch in (" ", "\t")):
                break
            if ch == '"' and prev_ch in YAML_TOKEN_BOUNDARIES:
                in_double = True
                inner_chars.append(ch)
                pos += 1
                continue
            if ch == "'" and prev_ch in YAML_TOKEN_BOUNDARIES:
                in_single = True
                inner_chars.append(ch)
                pos += 1
                continue
            if ch == open_char:
                if depth > 0:
                    inner_chars.append(ch)
                depth += 1
                if depth > MAX_FLOW_DEPTH:
                    raise FrontmatterError(
                        f"line {start_lineno}: flow nesting exceeds maximum depth "
                        f"({MAX_FLOW_DEPTH})"
                    )
                pos += 1
                continue
            if ch == close_char:
                depth -= 1
                if depth == 0:
                    trailing = current_seg[pos + 1 :].strip()
                    if trailing and not trailing.startswith("#"):
                        raise FrontmatterError(
                            f"line {start_lineno}: unexpected content after closing {close_char!r}"
                        )
                    return ("".join(inner_chars), idx)
                inner_chars.append(ch)
                pos += 1
                continue

            inner_chars.append(ch)
            pos += 1

        if in_double and escaped:
            if inner_chars and inner_chars[-1] == "\\":
                inner_chars.pop()
            escaped = False
            escaped_eol = True

        if idx >= total:
            raise FrontmatterError(f"line {start_lineno}: unterminated flow {open_char!r}")
        current_seg = lines[idx].strip()
        idx += 1


def _consume_flow_sequence(
    first_raw: str, lines: list[str], next_idx: int, start_lineno: int
) -> tuple[list[object], int]:
    inner, end_idx = _consume_flow_collection(
        first_raw, lines, next_idx, start_lineno, open_char="[", close_char="]"
    )
    return (_split_flow_items(inner, start_lineno), end_idx)


def _consume_flow_mapping(
    first_raw: str, lines: list[str], next_idx: int, start_lineno: int
) -> tuple[dict[str, object], int]:
    _inner, end_idx = _consume_flow_collection(
        first_raw, lines, next_idx, start_lineno, open_char="{", close_char="}"
    )
    return ({}, end_idx)


def _parse_inline_scalar(raw: str, lineno: int, *, _depth: int = 0) -> object:
    """Parse a single-line YAML scalar, inline flow sequence, or inline flow mapping."""
    stripped = raw.strip()
    if not stripped:
        return ""
    if stripped[0] in ('"', "'"):
        val, _ = _consume_quoted_scalar(stripped, [], 0, lineno)
        return val
    cleaned = _strip_inline_comment(stripped)
    if not cleaned:
        return ""
    if cleaned.startswith("[") and cleaned.endswith("]"):
        return _split_flow_items(cleaned[1:-1], lineno, _depth=_depth)
    if cleaned.startswith("{") and cleaned.endswith("}"):
        return {}
    if cleaned in ("null", "Null", "NULL", "~"):
        return None
    if cleaned in ("true", "True", "TRUE"):
        return True
    if cleaned in ("false", "False", "FALSE"):
        return False
    return cleaned


def _consume_block_scalar(
    lines: list[str],
    start_idx: int,
    style: str,
    chomp: str,
    explicit_indent: int | None,
    *,
    min_indent: int = 0,
) -> tuple[str, int]:
    """Consume a YAML literal (|) or folded (>) block scalar starting at start_idx."""
    collected: list[str] = []
    idx = start_idx
    total = len(lines)
    block_indent = (min_indent + explicit_indent) if explicit_indent is not None else None

    while idx < total:
        line = lines[idx]
        if not line.strip():
            collected.append(line)
            idx += 1
            continue
        indent = _leading_spaces(line)
        if indent <= min_indent:
            break
        if block_indent is None:
            block_indent = indent
        if indent < block_indent:
            break
        collected.append(line)
        idx += 1

    if block_indent is None:
        return ("", idx)

    trailing_blank_count = 0
    while collected and not collected[-1].strip():
        collected.pop()
        trailing_blank_count += 1

    if not collected:
        return ("\n" * trailing_blank_count, idx) if chomp == "+" else ("", idx)

    stripped_lines = [line[block_indent:] if line.strip() else "" for line in collected]

    if style == "|":
        body = "\n".join(stripped_lines)
    else:
        parts: list[str] = []
        last_nonempty: str | None = None
        for pos, current in enumerate(stripped_lines):
            curr_indented = bool(current) and current[0] in (" ", "\t")
            if pos == 0:
                parts.append("\n" if current == "" else current)
            else:
                prev = stripped_lines[pos - 1]
                prev_indented = bool(prev) and prev[0] in (" ", "\t")
                if current == "":
                    parts.append("\n")
                elif prev == "":
                    if last_nonempty is None:
                        parts.append(current)
                    elif curr_indented or last_nonempty[0] in (" ", "\t"):
                        parts.append("\n" + current)
                    else:
                        parts.append(current)
                elif prev_indented or curr_indented:
                    parts.append("\n" + current)
                else:
                    parts.append(" " + current)
            if current != "":
                last_nonempty = current
        body = "".join(parts)

    if chomp == "-":
        return (body, idx)
    if chomp == "+":
        return (body + "\n" * (1 + trailing_blank_count), idx)
    return (body + "\n", idx)


def _consume_plain_continuations(
    first_cleaned: str, lines: list[str], start_idx: int, *, min_indent: int = 0
) -> tuple[str, bool, int]:
    """Consume indented plain scalar continuation lines, skipping indented comments."""
    parts = [first_cleaned]
    continued = False
    idx = start_idx
    total = len(lines)
    saw_blank = False

    while idx < total:
        nxt = lines[idx]
        nxt_stripped = nxt.strip()
        if not nxt_stripped:
            saw_blank = True
            idx += 1
            continue
        if _leading_spaces(nxt) <= min_indent:
            break
        if nxt_stripped.startswith("#"):
            idx += 1
            continue
        cleaned_nxt = _strip_inline_comment(nxt_stripped)
        if cleaned_nxt:
            parts.append(("\n" if saw_blank else " ") + cleaned_nxt)
            continued = True
            saw_blank = False
        idx += 1

    return ("".join(parts), continued, idx)


def _consume_yaml_value(
    raw_value: str,
    lines: list[str],
    next_idx: int,
    lineno: int,
    *,
    min_indent: int = 0,
    allow_block_scalar: bool = True,
) -> tuple[object, int]:
    """Parse a YAML value (block scalar, quoted, flow sequence/mapping, or plain scalar)."""
    if allow_block_scalar:
        block_match = BLOCK_SCALAR_RE.match(raw_value)
        if block_match:
            style = block_match.group(1)
            chomp = block_match.group(2) or block_match.group(5) or ""
            indent_str = block_match.group(3) or block_match.group(4) or ""
            explicit_indent = int(indent_str) if indent_str else None
            return _consume_block_scalar(
                lines, next_idx, style, chomp, explicit_indent, min_indent=min_indent
            )

    if raw_value[0] in ('"', "'"):
        return _consume_quoted_scalar(
            raw_value, lines, next_idx, lineno, min_indent=min_indent
        )

    if raw_value.startswith("["):
        return _consume_flow_sequence(raw_value, lines, next_idx, lineno)

    if raw_value.startswith("{"):
        return _consume_flow_mapping(raw_value, lines, next_idx, lineno)

    cleaned_first = _strip_inline_comment(raw_value)
    combined, continued, end_idx = _consume_plain_continuations(
        cleaned_first, lines, next_idx, min_indent=min_indent
    )
    return (combined if continued else _parse_inline_scalar(cleaned_first, lineno), end_idx)


def parse_skill_frontmatter(text: str) -> dict[str, object]:
    """Parse YAML frontmatter from a SKILL.md file using Python stdlib only."""
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = re.split(r"\r\n|\n|\r", text)
    if not lines or lines[0].rstrip(" \t") != "---":
        raise FrontmatterError("missing opening '---' frontmatter delimiter on line 1")

    close_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].rstrip(" \t") == "---":
            close_idx = idx
            break
    if close_idx is None:
        raise FrontmatterError("unclosed frontmatter block (missing closing '---')")

    fm_lines = lines[1:close_idx]
    data: dict[str, object] = {}
    idx = 0
    total = len(fm_lines)

    while idx < total:
        line = fm_lines[idx]
        lineno = idx + 2
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            idx += 1
            continue
        if _leading_spaces(line) > 0:
            raise FrontmatterError(f"line {lineno}: unexpected indentation at top level")
        match = FM_KEY_RE.match(line)
        if not match:
            raise FrontmatterError(f"line {lineno}: expected 'key: value' mapping entry")

        key = match.group(1)
        raw_rest = (match.group(2) or "").strip()
        idx += 1

        if raw_rest and not raw_rest.startswith("#"):
            value, idx = _consume_yaml_value(raw_rest, fm_lines, idx, lineno, min_indent=0)
            data[key] = value
            continue

        look = idx
        while look < total:
            cand = fm_lines[look].strip()
            if not cand or cand.startswith("#"):
                look += 1
                continue
            break

        if look >= total:
            data[key] = ""
            idx = look
            continue

        next_line = fm_lines[look]
        next_indent = _leading_spaces(next_line)
        next_stripped = next_line.strip()
        next_lineno = look + 2

        if next_stripped == "-" or next_stripped.startswith("- "):
            seq_indent = next_indent
            items: list[object] = []
            idx = look
            while idx < total:
                cur = fm_lines[idx]
                cur_stripped = cur.strip()
                if not cur_stripped or cur_stripped.startswith("#"):
                    idx += 1
                    continue
                cur_indent = _leading_spaces(cur)
                if cur_indent < seq_indent:
                    break
                if cur_indent == seq_indent and (
                    cur_stripped == "-" or cur_stripped.startswith("- ")
                ):
                    item_raw = cur_stripped[1:].strip()
                    item_lineno = idx + 2
                    idx += 1
                    if item_raw and not item_raw.startswith("#"):
                        item_val, idx = _consume_yaml_value(
                            item_raw, fm_lines, idx, item_lineno, min_indent=seq_indent
                        )
                        items.append(item_val)
                    else:
                        items.append("")
                    continue
                if cur_indent > seq_indent:
                    idx += 1
                    continue
                break
            data[key] = items
            continue

        if next_indent == 0:
            data[key] = ""
            idx = look
            continue

        if FM_KEY_RE.match(next_stripped):
            nested_map: dict[str, object] = {}
            map_indent = next_indent
            idx = look
            while idx < total:
                cur = fm_lines[idx]
                cur_stripped = cur.strip()
                if not cur_stripped or cur_stripped.startswith("#"):
                    idx += 1
                    continue
                cur_indent = _leading_spaces(cur)
                if cur_indent == 0:
                    break
                if cur_indent > map_indent:
                    idx += 1
                    continue
                nmatch = FM_KEY_RE.match(cur_stripped)
                if nmatch:
                    nkey = nmatch.group(1)
                    nraw = (nmatch.group(2) or "").strip()
                    nlineno = idx + 2
                    idx += 1
                    if nraw and not nraw.startswith("#"):
                        nval, idx = _consume_yaml_value(
                            nraw, fm_lines, idx, nlineno, min_indent=cur_indent
                        )
                        nested_map[nkey] = nval
                    else:
                        nested_map[nkey] = ""
                else:
                    idx += 1
            data[key] = nested_map
            continue

        value, idx = _consume_yaml_value(
            next_stripped,
            fm_lines,
            look + 1,
            next_lineno,
            min_indent=0,
            allow_block_scalar=False,
        )
        data[key] = value

    return data


def validate_skill_dir(skill_dir: Path) -> list[str]:
    """Validate that skill_dir contains a regular, compliant SKILL.md with matching name."""
    errors: list[str] = []
    skill_md = skill_dir / "SKILL.md"
    try:
        if skill_md.is_symlink():
            return [f"{skill_md}: SKILL.md must be a regular file, not a symlink"]
        if not skill_md.is_file():
            return [f"{skill_dir}: missing SKILL.md"]
    except OSError as exc:
        return [f"{skill_md}: inaccessible SKILL.md: {exc}"]

    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"{skill_md}: failed to read UTF-8 text: {exc}"]

    try:
        frontmatter = parse_skill_frontmatter(text)
    except FrontmatterError as exc:
        return [f"{skill_md}: invalid frontmatter: {exc}"]

    name = frontmatter.get("name")
    if not isinstance(name, str) or not name:
        errors.append(f"{skill_md}: frontmatter 'name' must be a non-empty string")
    else:
        if not KEBAB_CASE_RE.match(name):
            errors.append(
                f"{skill_md}: frontmatter 'name' ({name!r}) must be lowercase kebab-case"
            )
        if name != skill_dir.name:
            errors.append(
                f"{skill_md}: frontmatter 'name' ({name!r}) does not match "
                f"directory name ({skill_dir.name!r})"
            )

    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        errors.append(f"{skill_md}: frontmatter 'description' must be a non-empty string")

    return errors


def _validate_symlink_within_repo(
    link_path: Path, repo_root: Path, repo_root_res: Path
) -> str | None:
    """Validate that an in-repo symlink is relative and strictly confined within repo_root."""
    rel = link_path.relative_to(repo_root).as_posix()
    try:
        raw_target = os.readlink(link_path)
    except OSError as exc:
        return f"{rel}: failed to read symlink target: {exc}"
    start_dir = repo_root_res / link_path.relative_to(repo_root).parent
    resolved, err, _ = _resolve_confined_relative_path(start_dir, raw_target, repo_root_res)
    if err is not None or resolved is None:
        return f"{rel}: {err or 'invalid symlink'}"
    return None


def _validate_skill_tree_symlinks(
    skill_dir: Path, repo_root: Path, repo_root_res: Path
) -> list[str]:
    """Validate every symlink and directory readability inside a repository skill directory."""
    if _safe_is_symlink(skill_dir):
        return [f"{skill_dir}: skill directory must not be a symlink"]

    errors: list[str] = []

    def on_walk_error(exc: OSError) -> None:
        target = exc.filename if exc.filename else str(skill_dir)
        errors.append(f"{target}: unreadable directory ({exc.strerror or exc})")

    for dirpath_str, dirnames, filenames in os.walk(
        skill_dir, followlinks=False, onerror=on_walk_error
    ):
        dirpath = Path(dirpath_str)
        kept: list[str] = []
        for dirname in dirnames:
            full_dir = dirpath / dirname
            try:
                st = os.lstat(full_dir)
            except OSError as exc:
                errors.append(f"{full_dir}: inaccessible entry ({exc.strerror or exc})")
                continue
            if stat.S_ISLNK(st.st_mode):
                err = _validate_symlink_within_repo(full_dir, repo_root, repo_root_res)
                if err is not None:
                    errors.append(err)
            else:
                kept.append(dirname)
        dirnames[:] = kept
        for filename in filenames:
            full_file = dirpath / filename
            try:
                st = os.lstat(full_file)
            except OSError as exc:
                errors.append(f"{full_file}: inaccessible entry ({exc.strerror or exc})")
                continue
            if stat.S_ISLNK(st.st_mode):
                err = _validate_symlink_within_repo(full_file, repo_root, repo_root_res)
                if err is not None:
                    errors.append(err)
    return errors


def _validate_staged_skill_symlinks(
    staged_skill: Path,
    dest_dir: Path,
    repo_root: Path,
    repo_root_res: Path,
    stage_parent: Path,
) -> list[str]:
    """Validate symlinks in a staged adoption copy using a virtual overlay at dest_dir."""
    errors: list[str] = []
    staged_res = safe_resolve(staged_skill, strict=True)
    stage_parent_res = safe_resolve(stage_parent, strict=True)
    if staged_res is None or stage_parent_res is None:
        return [f"{staged_skill}: failed to resolve staging directory"]

    dest_dir_virtual = repo_root_res / dest_dir.relative_to(repo_root)
    overlay = (dest_dir_virtual, staged_res)

    def on_walk_error(exc: OSError) -> None:
        target = exc.filename if exc.filename else str(staged_skill)
        errors.append(f"{target}: unreadable staged directory ({exc.strerror or exc})")

    for dirpath_str, dirnames, filenames in os.walk(
        staged_skill, followlinks=False, onerror=on_walk_error
    ):
        dirpath = Path(dirpath_str)
        candidates: list[Path] = []
        kept: list[str] = []
        for dirname in dirnames:
            full_dir = dirpath / dirname
            try:
                st = os.lstat(full_dir)
            except OSError as exc:
                errors.append(f"{full_dir}: inaccessible staged entry ({exc.strerror or exc})")
                continue
            if stat.S_ISLNK(st.st_mode):
                candidates.append(full_dir)
            else:
                kept.append(dirname)
        dirnames[:] = kept
        for filename in filenames:
            full_file = dirpath / filename
            try:
                st = os.lstat(full_file)
            except OSError as exc:
                errors.append(f"{full_file}: inaccessible staged entry ({exc.strerror or exc})")
                continue
            if stat.S_ISLNK(st.st_mode):
                candidates.append(full_file)

        for staged_link in candidates:
            rel_in_skill = staged_link.relative_to(staged_skill)
            final_virtual = dest_dir_virtual / rel_in_skill
            rel_label = final_virtual.relative_to(repo_root_res).as_posix()
            try:
                raw_target = os.readlink(staged_link)
            except OSError as exc:
                errors.append(f"{rel_label}: failed to read symlink target: {exc}")
                continue
            resolved, err, _ = _resolve_confined_relative_path(
                final_virtual.parent,
                raw_target,
                repo_root_res,
                overlay=overlay,
                forbidden_dir_res=stage_parent_res,
            )
            if err is not None or resolved is None:
                errors.append(f"{rel_label}: {err or 'invalid symlink'}")
    return errors


def _validate_string_list_field(container: dict[str, object], field: str, label: str) -> str | None:
    if field not in container or container[field] is None:
        return None
    val = container[field]
    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
        return f"{label} must be an array of strings"
    return None


def _load_skills_json(path: Path) -> tuple[dict[str, object] | None, str | None]:
    """Load and validate a skills.json file. Returns (data, error_message)."""
    try:
        raw_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return (None, f"failed to read UTF-8 JSON: {exc}")

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return (None, f"invalid JSON ({exc.msg} at line {exc.lineno}, col {exc.colno})")

    if not isinstance(data, dict):
        return (None, "top-level JSON value must be an object")

    top_exclude_err = _validate_string_list_field(data, "exclude", "'exclude'")
    if top_exclude_err is not None:
        return (None, top_exclude_err)

    if "entries" in data:
        entries = data["entries"]
        if not isinstance(entries, list):
            return (None, "'entries' must be a JSON array")
        for idx, item in enumerate(entries):
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                return (
                    None,
                    f"'entries[{idx}]' must be an object with a string 'path' field",
                )
            if "\x00" in item["path"]:
                return (None, f"'entries[{idx}].path' must not contain NUL bytes")
            for field in ("include_only", "exclude"):
                field_err = _validate_string_list_field(
                    item, field, f"'entries[{idx}].{field}'"
                )
                if field_err is not None:
                    return (None, field_err)

    if "inherits" in data and not isinstance(data["inherits"], list):
        return (None, "'inherits' must be a JSON array")

    return (data, None)


def _resolve_skills_json_target(skills_json_path: Path) -> tuple[Path | None, str | None]:
    """Resolve the writable file target for skills.json without breaking valid symlinks."""
    try:
        if skills_json_path.is_symlink():
            resolved = safe_resolve(skills_json_path, strict=True)
            if resolved is None:
                return (None, "broken or circular symlink")
            if not resolved.is_file():
                return (None, f"symlink points to non-file ({resolved})")
            return (resolved, None)

        if skills_json_path.exists():
            if not skills_json_path.is_file():
                return (None, "path exists and is not a regular file")
            return (skills_json_path, None)
    except OSError as exc:
        return (None, f"inaccessible skills.json path: {exc}")

    return (skills_json_path, None)


def _desired_agy_targets(repo_root: Path) -> list[Path]:
    return [repo_root / "shared", repo_root / HARNESS_REGISTRY["agy"].repo_rel_dir]


def _configured_resolved_paths(data: dict[str, object], home: Path) -> set[Path]:
    resolved_paths: set[Path] = set()
    entries = data.get("entries")
    if not isinstance(entries, list):
        return resolved_paths
    for item in entries:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            resolved = resolve_configured_entry_path(item["path"], home)
            if resolved is not None:
                resolved_paths.add(resolved)
    return resolved_paths


def _missing_skills_json_entries(
    data: dict[str, object], repo_root: Path, home: Path
) -> tuple[list[tuple[Path, str]], list[str]]:
    """Return (missing_targets_with_formatted_path, target_errors) for Antigravity."""
    configured = _configured_resolved_paths(data, home)
    missing: list[tuple[Path, str]] = []
    target_errors: list[str] = []
    repo_root_res = safe_resolve(repo_root, strict=True)
    if repo_root_res is None:
        return ([], [f"failed to resolve repository root: {repo_root}"])

    for desired in _desired_agy_targets(repo_root):
        rel_parts = desired.relative_to(repo_root).parts
        container_err = _validate_in_repo_container(repo_root_res, rel_parts)
        if container_err is not None:
            target_errors.append(f"invalid repo target {desired} ({container_err})")
            continue
        desired_res = safe_resolve(desired)
        if desired_res is None:
            target_errors.append(f"failed to resolve desired repo target: {desired}")
            continue
        if desired_res not in configured:
            missing.append((desired_res, format_skills_json_path(desired, home)))

    return (missing, target_errors)


def _matches_skill_pattern(pattern: str, skill_name: str) -> bool:
    try:
        return re.fullmatch(pattern, skill_name) is not None
    except re.error:
        return pattern == skill_name


def _entry_exposes_skill(
    item: dict[str, object], skill_name: str, top_exclude: list[str] | None = None
) -> bool:
    if top_exclude and any(_matches_skill_pattern(pat, skill_name) for pat in top_exclude):
        return False
    include_only = item.get("include_only")
    if isinstance(include_only, list) and not any(
        _matches_skill_pattern(pat, skill_name) for pat in include_only if isinstance(pat, str)
    ):
        return False
    exclude = item.get("exclude")
    return not (
        isinstance(exclude, list)
        and any(_matches_skill_pattern(pat, skill_name) for pat in exclude if isinstance(pat, str))
    )


def _is_skill_exposed_by_skills_json(
    data: dict[str, object], repo_skill_dir: Path, home: Path
) -> bool:
    """Return True iff skills.json exposes repo_skill_dir without filtering it out."""
    parent_res = safe_resolve(repo_skill_dir.parent)
    if parent_res is None:
        return False
    entries = data.get("entries")
    if not isinstance(entries, list):
        return False
    raw_top_exclude = data.get("exclude")
    top_exclude = (
        [x for x in raw_top_exclude if isinstance(x, str)]
        if isinstance(raw_top_exclude, list)
        else None
    )
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        if (
            resolve_configured_entry_path(item["path"], home) == parent_res
            and _entry_exposes_skill(item, repo_skill_dir.name, top_exclude)
        ):
            return True
    return False


def _hidden_repo_skills(
    data: dict[str, object], repo_skill_map: dict[str, Path], home: Path
) -> list[str]:
    """Return repo skills whose parent is in skills.json but excluded by filters."""
    configured = _configured_resolved_paths(data, home)
    hidden: list[str] = []
    for name, skill_dir in sorted(repo_skill_map.items()):
        parent_res = safe_resolve(skill_dir.parent)
        if (
            parent_res is not None
            and parent_res in configured
            and not _is_skill_exposed_by_skills_json(data, skill_dir, home)
        ):
            hidden.append(name)
    return hidden


def _read_process_umask() -> int:
    with _UMASK_LOCK:
        current = os.umask(0o077)
        try:
            return current
        finally:
            os.umask(current)


def _fsync_directory(directory: Path) -> None:
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _atomic_write_json(target: Path, data: dict[str, object]) -> None:
    """Atomically write JSON to target, preserving non-symlink file mode and fsyncing."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        existing_mode = 0o644 & ~_read_process_umask()
    else:
        if stat.S_ISLNK(st.st_mode):
            raise OSError(f"refusing to overwrite symlink directly: {target}")
        existing_mode = stat.S_IMODE(st.st_mode)

    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), existing_mode)
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
        _fsync_directory(target.parent)
    except BaseException:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        raise


def _move_entry_to_backup(entry: Path, dest: Path, backup_dir: Path) -> None:
    """Move entry to dest via os.rename with an explicit EXDEV-only staged fallback."""
    try:
        os.rename(entry, dest)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise

    if _safe_is_symlink(entry):
        dest.symlink_to(os.readlink(entry))
        entry.unlink()
        return

    if _safe_is_dir(entry):
        stage_dir = Path(tempfile.mkdtemp(prefix=f".move-{entry.name}-", dir=backup_dir))
        try:
            staged_copy = stage_dir / entry.name
            shutil.copytree(entry, staged_copy, symlinks=True)
            os.rename(staged_copy, dest)
        finally:
            _rmtree_owned_dir(stage_dir)
        shutil.rmtree(entry)
        return

    stage_file: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=backup_dir, prefix=f".move-{entry.name}-", delete=False
        ) as handle:
            stage_file = Path(handle.name)
        shutil.copy2(entry, stage_file)
        os.replace(stage_file, dest)
    except BaseException:
        if stage_file is not None:
            with contextlib.suppress(OSError):
                stage_file.unlink()
        raise
    entry.unlink()


def _find_shadowing_agy_skills(
    home: Path, repo_skill_map: dict[str, Path]
) -> tuple[list[Path], str | None]:
    _, _, agy_skills_dir = _agy_config_paths(home)
    try:
        dir_st = os.lstat(agy_skills_dir)
    except FileNotFoundError:
        return ([], None)
    except OSError as exc:
        return ([], f"{agy_skills_dir}: inaccessible ({exc})")
    if stat.S_ISLNK(dir_st.st_mode) or not stat.S_ISDIR(dir_st.st_mode):
        return ([], None)

    entries, err = _iter_visible_entries_checked(agy_skills_dir)
    if err is not None:
        return ([], err)

    colliding: list[Path] = []
    for entry in entries:
        try:
            st = os.lstat(entry)
        except OSError as exc:
            return ([], f"{entry}: inaccessible ({exc})")
        if (stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode)) and entry.name in repo_skill_map:
            colliding.append(entry)
    return (colliding, None)


def cmd_status(repo_root: Path, home: Path) -> int:
    """Print a read-only report of repo skills, ~/.agents/skills, and Antigravity config."""
    if not _safe_is_dir(repo_root):
        print(f"ERROR: repository path is not a directory: {repo_root}", file=sys.stderr)
        return 1
    home_err = _validate_dir_ancestors(home)
    if home_err is not None:
        print(f"ERROR: invalid home path ({home_err})", file=sys.stderr)
        return 1

    shared_dir = repo_root / "shared"
    shared_skills = [p.name for p in discover_skill_dirs(shared_dir)]
    print(f"Repository: {repo_root}")
    print(f"  shared skills ({len(shared_skills)}): {', '.join(shared_skills) or '(none)'}")
    for harness_name, spec in sorted(HARNESS_REGISTRY.items()):
        harness_skills = [p.name for p in discover_skill_dirs(repo_root / spec.repo_rel_dir)]
        print(
            f"  harness '{harness_name}' skills ({len(harness_skills)}): "
            f"{', '.join(harness_skills) or '(none)'}"
        )

    agents_dir = home / ".agents"
    agents_skills = agents_dir / "skills"
    expected_shared = safe_resolve(shared_dir)
    agents_ancestor_err = _validate_dir_ancestors(agents_dir, stop_at=home)
    if agents_ancestor_err is not None:
        agents_status = f"conflict ({agents_ancestor_err})"
    else:
        try:
            agents_st = os.lstat(agents_skills)
        except FileNotFoundError:
            agents_status = "not installed"
        except OSError as exc:
            agents_status = f"conflict (inaccessible: {exc})"
        else:
            if stat.S_ISLNK(agents_st.st_mode):
                resolved_link = safe_resolve(agents_skills, strict=True)
                raw_link = _safe_readlink(agents_skills)
                if resolved_link is None:
                    agents_status = f"conflict (broken or circular symlink -> {raw_link})"
                elif expected_shared is not None and resolved_link == expected_shared:
                    agents_status = f"ok (symlink -> {resolved_link})"
                else:
                    agents_status = (
                        f"conflict (symlink -> {resolved_link}, expected {expected_shared})"
                    )
            elif stat.S_ISDIR(agents_st.st_mode):
                agents_status = (
                    "conflict (real directory; run 'init.py apply --replace' to back up)"
                )
            else:
                agents_status = (
                    "conflict (non-directory file; run 'init.py apply --replace' to back up)"
                )
    print(f"\n~/.agents/skills ({agents_skills}): {agents_status}")

    config_dir, skills_json_path, agy_skills_dir = _agy_config_paths(home)
    repo_agy_skills = discover_agy_repo_skills(repo_root)
    config_ancestor_err = _validate_dir_ancestors(config_dir, stop_at=home)
    if config_ancestor_err is not None:
        print(f"~/.gemini/config/skills.json ({skills_json_path}): invalid ({config_ancestor_err})")
        print(f"~/.gemini/config/skills ({agy_skills_dir}): invalid ({config_ancestor_err})")
        return 0

    try:
        os.lstat(skills_json_path)
    except FileNotFoundError:
        print(f"~/.gemini/config/skills.json ({skills_json_path}): not present")
    except OSError as exc:
        print(f"~/.gemini/config/skills.json ({skills_json_path}): invalid (inaccessible: {exc})")
    else:
        resolved_target, target_err = _resolve_skills_json_target(skills_json_path)
        if target_err is not None or resolved_target is None:
            print(f"~/.gemini/config/skills.json ({skills_json_path}): invalid ({target_err})")
        else:
            data, load_err = _load_skills_json(resolved_target)
            link_note = (
                f" [symlink -> {resolved_target}]" if _safe_is_symlink(skills_json_path) else ""
            )
            if load_err is not None or data is None:
                print(
                    f"~/.gemini/config/skills.json ({skills_json_path}){link_note}: "
                    f"invalid ({load_err})"
                )
            else:
                missing, target_errors = _missing_skills_json_entries(data, repo_root, home)
                hidden = _hidden_repo_skills(data, repo_agy_skills, home)
                if target_errors:
                    print(
                        f"~/.gemini/config/skills.json ({skills_json_path}){link_note}: "
                        f"invalid repo target ({'; '.join(target_errors)})"
                    )
                elif missing:
                    missing_labels = ", ".join(formatted for _, formatted in missing)
                    print(
                        f"~/.gemini/config/skills.json ({skills_json_path}){link_note}: "
                        f"missing entries ({missing_labels})"
                    )
                elif hidden:
                    print(
                        f"~/.gemini/config/skills.json ({skills_json_path}){link_note}: "
                        f"configured (filtered out by include_only/exclude: {', '.join(hidden)})"
                    )
                else:
                    print(
                        f"~/.gemini/config/skills.json ({skills_json_path}){link_note}: configured"
                    )

    try:
        agy_st = os.lstat(agy_skills_dir)
    except FileNotFoundError:
        print(f"~/.gemini/config/skills ({agy_skills_dir}): no shadowing collisions")
    except OSError as exc:
        print(f"~/.gemini/config/skills ({agy_skills_dir}): invalid (inaccessible: {exc})")
    else:
        if stat.S_ISLNK(agy_st.st_mode):
            raw_link = _safe_readlink(agy_skills_dir)
            resolved_skills = safe_resolve(agy_skills_dir, strict=True)
            if resolved_skills is None:
                print(
                    f"~/.gemini/config/skills ({agy_skills_dir}): "
                    f"conflict (broken or circular symlink -> {raw_link})"
                )
            else:
                print(
                    f"~/.gemini/config/skills ({agy_skills_dir}): "
                    f"symlink -> {resolved_skills} (not traversed for shadowing)"
                )
        elif not stat.S_ISDIR(agy_st.st_mode):
            print(f"~/.gemini/config/skills ({agy_skills_dir}): conflict (non-directory file)")
        else:
            shadowing, scan_err = _find_shadowing_agy_skills(home, repo_agy_skills)
            if scan_err is not None:
                print(f"~/.gemini/config/skills ({agy_skills_dir}): invalid ({scan_err})")
            elif shadowing:
                names = ", ".join(entry.name for entry in shadowing)
                print(
                    f"~/.gemini/config/skills ({agy_skills_dir}): "
                    f"shadowing repo skills ({names})"
                )
            else:
                print(f"~/.gemini/config/skills ({agy_skills_dir}): no shadowing collisions")

    return 0


def _apply_agents_symlink(
    repo_root_res: Path,
    shared_res: Path,
    home: Path,
    timestamp: str,
    *,
    dry_run: bool,
    replace: bool,
) -> bool:
    """Link ~/.agents/skills -> <repo>/shared, backing up conflicts under --replace."""
    agents_dir = home / ".agents"
    agents_skills = agents_dir / "skills"
    ancestor_err = _validate_dir_ancestors(agents_dir, stop_at=home)
    if ancestor_err is not None:
        print(f"ERROR: cannot link {agents_skills} ({ancestor_err}).", file=sys.stderr)
        return False

    agents_dir_res = safe_resolve(agents_dir, strict=False)
    if agents_dir_res is not None and (
        agents_dir_res == repo_root_res or agents_dir_res.is_relative_to(repo_root_res)
    ):
        print(
            f"ERROR: refusing to modify {agents_skills} because {agents_dir} resolves inside "
            f"repository {repo_root_res}.",
            file=sys.stderr,
        )
        return False

    try:
        st = os.lstat(agents_skills)
    except FileNotFoundError:
        is_link = False
        exists = False
        is_real_dir = False
    except OSError as exc:
        print(f"ERROR: cannot inspect {agents_skills}: {exc}", file=sys.stderr)
        return False
    else:
        is_link = stat.S_ISLNK(st.st_mode)
        exists = _safe_exists(agents_skills)
        is_real_dir = (not is_link) and stat.S_ISDIR(st.st_mode)

    if not is_link and not exists:
        if dry_run:
            print(f"[dry-run] Would symlink {agents_skills} -> {shared_res}")
            return True
        try:
            agents_dir.mkdir(parents=True, exist_ok=True)
            agents_skills.symlink_to(shared_res)
        except OSError as exc:
            print(
                f"ERROR: failed to symlink {agents_skills} -> {shared_res}: {exc}",
                file=sys.stderr,
            )
            return False
        print(f"Linked {agents_skills} -> {shared_res}")
        return True

    if is_link and exists and safe_resolve(agents_skills, strict=True) == shared_res:
        print(f"OK: {agents_skills} already links to {shared_res}")
        return True

    if not replace:
        kind = "symlink" if is_link else ("directory" if is_real_dir else "file")
        print(
            f"CONFLICT: {agents_skills} already exists as a {kind}; "
            "leaving untouched (re-run with --replace to back up and link).",
            file=sys.stderr,
        )
        return False

    if not is_link and exists:
        agents_res = safe_resolve(agents_skills, strict=True)
        if (
            agents_res is None
            or _paths_overlap(agents_res, repo_root_res)
            or _paths_overlap(agents_res, shared_res)
        ):
            print(
                f"ERROR: refusing to back up {agents_skills} because it overlaps "
                f"repository {repo_root_res}.",
                file=sys.stderr,
            )
            return False

    if is_real_dir:
        existing_entries, list_err = _iter_visible_entries_checked(agents_skills)
        if list_err is not None:
            print(
                f"WARNING: could not scan {agents_skills} for unmigrated skills ({list_err})",
                file=sys.stderr,
            )
        else:
            existing_names: set[str] = set()
            for entry in existing_entries:
                try:
                    entry_st = os.lstat(entry)
                except OSError as exc:
                    print(
                        f"WARNING: could not inspect {entry} during unmigrated scan: {exc}",
                        file=sys.stderr,
                    )
                    continue
                if stat.S_ISDIR(entry_st.st_mode) or stat.S_ISLNK(entry_st.st_mode):
                    existing_names.add(entry.name)
            shared_names = {p.name for p in discover_skill_dirs(shared_res)}
            unmigrated = sorted(existing_names - shared_names)
            if unmigrated:
                preview_backup = unique_backup_path(agents_dir, "skills", timestamp)
                print(
                    f"WARNING: unmigrated skills in {agents_skills} not in repo shared/: "
                    f"{', '.join(unmigrated)}. They will be preserved under {preview_backup}; "
                    "import them with 'init.py adopt <backup>/<name> --scope shared'.",
                    file=sys.stderr,
                )

    if dry_run:
        preview_backup = unique_backup_path(agents_dir, "skills", timestamp)
        print(
            f"[dry-run] Would move {agents_skills} -> {preview_backup} "
            f"and symlink {agents_skills} -> {shared_res}"
        )
        return True

    try:
        backup_path = (
            _create_unique_backup_dir(agents_dir, "skills", timestamp)
            if is_real_dir
            else _create_unique_backup_file_slot(agents_dir, "skills", timestamp)
        )
    except OSError as exc:
        print(f"ERROR: failed to reserve backup slot in {agents_dir}: {exc}", file=sys.stderr)
        return False

    try:
        os.rename(agents_skills, backup_path)
    except OSError as exc:
        if is_real_dir:
            with contextlib.suppress(OSError):
                backup_path.rmdir()
        else:
            with contextlib.suppress(OSError):
                backup_path.unlink()
        print(f"ERROR: failed to back up {agents_skills} -> {backup_path}: {exc}", file=sys.stderr)
        return False

    try:
        agents_skills.symlink_to(shared_res)
    except BaseException as exc:
        if not _path_exists_or_symlink(agents_skills):
            try:
                os.rename(backup_path, agents_skills)
            except OSError as rb_exc:
                print(
                    f"ERROR: rollback failed ({backup_path} -> {agents_skills}): {rb_exc}",
                    file=sys.stderr,
                )
        else:
            print(
                f"ERROR: rollback skipped because {agents_skills} reappeared; "
                f"original backup preserved at {backup_path}",
                file=sys.stderr,
            )
        if isinstance(exc, OSError):
            print(
                f"ERROR: failed to symlink {agents_skills} -> {shared_res}: {exc}",
                file=sys.stderr,
            )
            return False
        raise

    print(f"Backed up {agents_skills} -> {backup_path}")
    print(f"Linked {agents_skills} -> {shared_res}")
    return True


def _apply_agy_skills_json(
    repo_root: Path, repo_root_res: Path, home: Path, *, dry_run: bool
) -> tuple[bool, dict[str, object] | None]:
    """Non-destructively ensure repo entries exist in ~/.gemini/config/skills.json."""
    config_dir, skills_json_path, _ = _agy_config_paths(home)
    ancestor_err = _validate_dir_ancestors(config_dir, stop_at=home)
    if ancestor_err is not None:
        print(f"ERROR: cannot configure {skills_json_path} ({ancestor_err}).", file=sys.stderr)
        return (False, None)

    config_dir_res = safe_resolve(config_dir, strict=False)
    if config_dir_res is not None and (
        config_dir_res == repo_root_res or config_dir_res.is_relative_to(repo_root_res)
    ):
        print(
            f"ERROR: refusing to configure {skills_json_path} because {config_dir} resolves "
            f"inside repository {repo_root_res}.",
            file=sys.stderr,
        )
        return (False, None)

    write_target, target_err = _resolve_skills_json_target(skills_json_path)
    if target_err is not None or write_target is None:
        print(
            f"ERROR: {skills_json_path} cannot be updated ({target_err}); refusing to overwrite.",
            file=sys.stderr,
        )
        return (False, None)

    target_existed = _safe_exists(write_target)
    data: dict[str, object] = {"entries": []}
    if target_existed:
        loaded, load_err = _load_skills_json(write_target)
        if load_err is not None or loaded is None:
            print(
                f"ERROR: {skills_json_path} is malformed ({load_err}); refusing to overwrite.",
                file=sys.stderr,
            )
            return (False, None)
        data = loaded

    missing, target_errors = _missing_skills_json_entries(data, repo_root, home)
    if target_errors:
        for err in target_errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return (False, None)

    changed = False
    if "entries" not in data:
        data["entries"] = []
        changed = True
    entries = data["entries"]
    assert isinstance(entries, list)

    for _desired_res, formatted in missing:
        entries.append({"path": formatted})
        changed = True

    needs_write = (not target_existed) or changed
    if not needs_write:
        print(f"OK: {skills_json_path} already contains required entries")
        return (True, data)

    if dry_run:
        print(f"[dry-run] Would write {write_target}")
        return (True, data)

    try:
        _atomic_write_json(write_target, data)
    except OSError as exc:
        print(f"ERROR: failed to write {write_target}: {exc}", file=sys.stderr)
        return (False, None)

    if _safe_is_symlink(skills_json_path):
        print(f"Updated {skills_json_path} via symlink target {write_target}")
    else:
        print(f"Updated {write_target}")
    return (True, data)


def _apply_agy_shadow_backups(
    repo_root: Path,
    repo_root_res: Path,
    home: Path,
    timestamp: str,
    *,
    skills_json_ready: bool,
    skills_json_data: dict[str, object] | None,
    dry_run: bool,
    replace: bool,
) -> bool:
    """Detect or safely back up loose skills in ~/.gemini/config/skills/ shadowing repo skills."""
    config_dir, skills_json_path, agy_skills_dir = _agy_config_paths(home)
    if _validate_dir_ancestors(config_dir, stop_at=home) is not None:
        return True

    try:
        agy_dir_st = os.lstat(agy_skills_dir)
    except FileNotFoundError:
        return True
    except OSError as exc:
        print(f"ERROR: cannot inspect {agy_skills_dir}: {exc}", file=sys.stderr)
        return False

    if stat.S_ISLNK(agy_dir_st.st_mode):
        raw_link = _safe_readlink(agy_skills_dir)
        if not replace:
            print(
                f"WARNING: {agy_skills_dir} is a symlink (-> {raw_link}); "
                "skipping loose-skill collision scan.",
                file=sys.stderr,
            )
            return True
        print(
            f"ERROR: {agy_skills_dir} is a symlink (-> {raw_link}); "
            "refusing to move entries through symlinked directory.",
            file=sys.stderr,
        )
        return False

    if not stat.S_ISDIR(agy_dir_st.st_mode):
        if not replace:
            print(
                f"WARNING: {agy_skills_dir} exists as a non-directory file; "
                "skipping loose-skill collision scan.",
                file=sys.stderr,
            )
            return True
        print(
            f"ERROR: {agy_skills_dir} exists as a non-directory file; cannot inspect shadows.",
            file=sys.stderr,
        )
        return False

    repo_skill_map = discover_agy_repo_skills(repo_root)
    colliding, scan_err = _find_shadowing_agy_skills(home, repo_skill_map)
    if scan_err is not None:
        print(f"ERROR: {scan_err}", file=sys.stderr)
        return False
    if not colliding:
        return True

    if not replace:
        names = ", ".join(entry.name for entry in colliding)
        print(
            f"WARNING: loose skills in {agy_skills_dir} shadow repo skills: "
            f"{names}. Re-run with --replace to back them up.",
            file=sys.stderr,
        )
        return True

    agy_skills_res = safe_resolve(agy_skills_dir, strict=True)
    if agy_skills_res is None or _paths_overlap(agy_skills_res, repo_root_res):
        print(
            f"ERROR: refusing to move skills from {agy_skills_dir} because it overlaps "
            f"repository {repo_root_res}.",
            file=sys.stderr,
        )
        return False

    if not skills_json_ready or skills_json_data is None:
        names = ", ".join(entry.name for entry in colliding)
        print(
            f"WARNING: refusing to move shadowing skills ({names}) because "
            f"{skills_json_path} was not updated.",
            file=sys.stderr,
        )
        return False

    duplicate_names = _duplicate_agy_skill_names(repo_root)
    ok = True
    backup_dir: Path | None = None
    moved_count = 0

    try:
        for entry in colliding:
            if entry.name in duplicate_names:
                print(
                    f"ERROR: refusing to move shadowing skill {entry} because '{entry.name}' "
                    "is duplicated in both shared/ and harnesses/agy/.",
                    file=sys.stderr,
                )
                ok = False
                continue

            repo_skill_dir = repo_skill_map[entry.name]

            if not _safe_is_symlink(entry):
                entry_res = safe_resolve(entry, strict=True)
                repo_skill_res = safe_resolve(repo_skill_dir, strict=True)
                if (
                    entry_res is None
                    or entry_res == repo_skill_res
                    or _paths_overlap(entry_res, repo_root_res)
                ):
                    print(
                        f"ERROR: refusing to move {entry} because it overlaps repository "
                        f"{repo_root_res}.",
                        file=sys.stderr,
                    )
                    ok = False
                    continue

            if not _is_skill_exposed_by_skills_json(skills_json_data, repo_skill_dir, home):
                print(
                    f"ERROR: refusing to move shadowing skill {entry} because {skills_json_path} "
                    f"filters out '{entry.name}' via include_only/exclude.",
                    file=sys.stderr,
                )
                ok = False
                continue

            validation_errors = validate_skill_dir(repo_skill_dir) + _validate_skill_tree_symlinks(
                repo_skill_dir, repo_root, repo_root_res
            )
            if validation_errors:
                print(
                    f"ERROR: refusing to move shadowing skill {entry} because repo skill "
                    f"{repo_skill_dir} failed validation: {'; '.join(validation_errors)}",
                    file=sys.stderr,
                )
                ok = False
                continue

            if backup_dir is None:
                if dry_run:
                    backup_dir = unique_backup_path(config_dir, "skills", timestamp)
                else:
                    try:
                        backup_dir = _create_unique_backup_dir(config_dir, "skills", timestamp)
                    except OSError as exc:
                        print(
                            f"ERROR: failed to create backup directory in {config_dir}: {exc}",
                            file=sys.stderr,
                        )
                        return False

            dest = backup_dir / entry.name
            if dry_run:
                print(f"[dry-run] Would move shadowing skill {entry} -> {dest}")
                continue

            try:
                _move_entry_to_backup(entry, dest, backup_dir)
            except OSError as exc:
                print(
                    f"ERROR: failed to back up shadowing skill {entry} -> {dest}: {exc}",
                    file=sys.stderr,
                )
                ok = False
                continue

            moved_count += 1
            print(f"Backed up shadowing skill {entry} -> {dest}")
    finally:
        if backup_dir is not None and not dry_run and moved_count == 0:
            with contextlib.suppress(OSError):
                backup_dir.rmdir()

    return ok


def cmd_apply(repo_root: Path, home: Path, *, dry_run: bool, replace: bool) -> int:
    """Link ~/.agents/skills -> shared/ and non-destructively configure Antigravity skills.json."""
    home_err = _validate_dir_ancestors(home)
    if home_err is not None:
        print(f"ERROR: invalid home path ({home_err})", file=sys.stderr)
        return 1

    repo_root_res = safe_resolve(repo_root, strict=True)
    if repo_root_res is None or not _safe_is_dir(repo_root_res):
        print(f"ERROR: repository path is not a directory: {repo_root}", file=sys.stderr)
        return 1

    shared_container_err = _validate_in_repo_container(repo_root_res, ("shared",))
    if shared_container_err is not None:
        print(f"ERROR: {shared_container_err}", file=sys.stderr)
        return 1

    shared_dir = repo_root / "shared"
    if not _safe_is_dir(shared_dir):
        print(f"ERROR: repository shared/ directory not found: {shared_dir}", file=sys.stderr)
        return 1

    shared_res = safe_resolve(shared_dir, strict=True)
    if shared_res is None:
        print(f"ERROR: failed to resolve shared/ directory: {shared_dir}", file=sys.stderr)
        return 1

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    agents_ok = _apply_agents_symlink(
        repo_root_res, shared_res, home, timestamp, dry_run=dry_run, replace=replace
    )
    skills_json_ready, skills_json_data = _apply_agy_skills_json(
        repo_root, repo_root_res, home, dry_run=dry_run
    )
    shadows_ok = _apply_agy_shadow_backups(
        repo_root,
        repo_root_res,
        home,
        timestamp,
        skills_json_ready=skills_json_ready,
        skills_json_data=skills_json_data,
        dry_run=dry_run,
        replace=replace,
    )
    return 0 if (agents_ok and skills_json_ready and shadows_ok) else 1


def _find_cross_scope_duplicate(repo_root: Path, scope: str, name: str) -> Path | None:
    """Return an existing skill directory with the same name in a different scope, if any."""
    if scope != "shared":
        candidate = repo_root / "shared" / name
        if _path_exists_or_symlink(candidate):
            return candidate
    else:
        harnesses_root = repo_root / "harnesses"
        if _safe_is_dir(harnesses_root):
            for harness_dir in _iter_visible_entries(harnesses_root):
                if _safe_is_dir(harness_dir) and not _safe_is_symlink(harness_dir):
                    candidate = harness_dir / name
                    if _path_exists_or_symlink(candidate):
                        return candidate
    return None


def cmd_adopt(repo_root: Path, source_dir: Path | str, *, scope: str, force: bool) -> int:
    """Copy a loose skill directory into shared/<name> or harnesses/<scope>/<name>."""
    if isinstance(source_dir, str) and ("\x00" in source_dir or not source_dir.strip()):
        print("ERROR: source skill path must not be empty or contain NUL bytes", file=sys.stderr)
        return 1

    repo_root_res = safe_resolve(repo_root, strict=True)
    if repo_root_res is None or not _safe_is_dir(repo_root_res):
        print(f"ERROR: repository path is not a directory: {repo_root}", file=sys.stderr)
        return 1

    try:
        expanded_src = os.path.expanduser(str(source_dir))
    except RuntimeError as exc:
        print(f"ERROR: failed to expand source path {source_dir!r}: {exc}", file=sys.stderr)
        return 1

    source_abs = Path(os.path.abspath(expanded_src))
    if not _safe_is_dir(source_abs):
        print(f"ERROR: source skill path is not a directory: {source_abs}", file=sys.stderr)
        return 1

    validation_errors = validate_skill_dir(source_abs)
    if validation_errors:
        for err in validation_errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    name = source_abs.name
    if scope == "shared":
        dest_rel_parts: tuple[str, ...] = ("shared",)
        dest_parent = repo_root / "shared"
    elif scope in HARNESS_REGISTRY:
        dest_rel_parts = Path(HARNESS_REGISTRY[scope].repo_rel_dir).parts
        dest_parent = repo_root / HARNESS_REGISTRY[scope].repo_rel_dir
    else:
        print(f"ERROR: unsupported adopt scope: {scope}", file=sys.stderr)
        return 1

    container_err = _validate_in_repo_container(repo_root_res, dest_rel_parts)
    if container_err is not None:
        print(f"ERROR: {container_err}", file=sys.stderr)
        return 1

    cross_dup = _find_cross_scope_duplicate(repo_root, scope, name)
    if cross_dup is not None:
        print(
            f"ERROR: skill '{name}' already exists in another scope at {cross_dup}",
            file=sys.stderr,
        )
        return 1

    dest_dir = dest_parent / name
    src_res = safe_resolve(source_abs, strict=True)
    src_parent_res = safe_resolve(source_abs.parent, strict=True)
    dest_parent_res = safe_resolve(dest_parent, strict=False)
    if src_res is None or src_parent_res is None or dest_parent_res is None:
        print(f"ERROR: failed to resolve paths for adoption: {source_abs}", file=sys.stderr)
        return 1

    lexical_src = src_parent_res / source_abs.name
    lexical_dest = dest_parent_res / name
    overlaps_dest = (
        dest_parent_res == src_res
        or dest_parent_res.is_relative_to(src_res)
        or lexical_src == lexical_dest
        or (
            not _safe_is_symlink(dest_dir)
            and (src_res == lexical_dest or src_res.is_relative_to(lexical_dest))
        )
    )
    if overlaps_dest:
        print(
            f"ERROR: cannot adopt {source_abs} into {dest_dir} (same or overlapping path)",
            file=sys.stderr,
        )
        return 1

    if _path_exists_or_symlink(dest_dir) and not force:
        print(
            f"ERROR: destination already exists: {dest_dir} (pass --force to overwrite)",
            file=sys.stderr,
        )
        return 1

    stage_parent: Path | None = None
    staged_skill: Path | None = None
    old_slot: Path | None = None
    moved_old = False
    preserve_old = False

    try:
        dest_parent.mkdir(parents=True, exist_ok=True)
        stage_parent = Path(tempfile.mkdtemp(prefix=f".adopt-{name}-", dir=dest_parent))
        staged_skill = stage_parent / name
        shutil.copytree(source_abs, staged_skill, symlinks=True, ignore=_adopt_ignore)
        symlink_errors = _validate_staged_skill_symlinks(
            staged_skill, dest_dir, repo_root, repo_root_res, stage_parent
        )
        if symlink_errors:
            for err in symlink_errors:
                print(f"ERROR: {err}", file=sys.stderr)
            return 1

        if force:
            if _path_exists_or_symlink(dest_dir):
                old_slot = stage_parent / "_old"
                os.rename(dest_dir, old_slot)
                moved_old = True
        elif _path_exists_or_symlink(dest_dir):
            print(
                f"ERROR: destination already exists: {dest_dir} (pass --force to overwrite)",
                file=sys.stderr,
            )
            return 1
        os.rename(staged_skill, dest_dir)
    except BaseException as exc:
        if moved_old and old_slot is not None:
            if not _path_exists_or_symlink(dest_dir):
                try:
                    os.rename(old_slot, dest_dir)
                except OSError as rb_exc:
                    preserve_old = True
                    print(
                        f"ERROR: rollback failed ({rb_exc}); original skill preserved at "
                        f"{old_slot}",
                        file=sys.stderr,
                    )
            else:
                preserve_old = True
                print(
                    f"ERROR: rollback skipped because {dest_dir} reappeared; "
                    f"original skill preserved at {old_slot}",
                    file=sys.stderr,
                )
        if isinstance(exc, OSError):
            print(f"ERROR: failed to adopt {source_abs} into {dest_dir}: {exc}", file=sys.stderr)
            return 1
        raise
    finally:
        if stage_parent is not None:
            if preserve_old:
                if staged_skill is not None:
                    _rmtree_owned_dir(staged_skill)
            else:
                _rmtree_owned_dir(stage_parent)

    print(f"Adopted skill '{name}' -> {dest_dir}")
    return 0


def _walk_directory_entries(
    root: Path,
    *,
    seen: set[Path],
    entries: list[Path],
    scan_errors: list[str] | None = None,
    prune: bool = True,
) -> None:
    """Walk root without following symlinks, collecting files, symlinks, and read errors."""

    def on_walk_error(exc: OSError) -> None:
        if scan_errors is not None:
            target = exc.filename if exc.filename else str(root)
            scan_errors.append(f"{target}: unreadable directory ({exc.strerror or exc})")

    for dirpath_str, dirnames, filenames in os.walk(
        root, followlinks=False, onerror=on_walk_error
    ):
        dirpath = Path(dirpath_str)
        kept_dirnames: list[str] = []
        for dirname in dirnames:
            if prune and _is_pruned_name(dirname):
                continue
            full_dir = dirpath / dirname
            try:
                st = os.lstat(full_dir)
            except OSError as exc:
                if scan_errors is not None:
                    scan_errors.append(f"{full_dir}: inaccessible entry ({exc.strerror or exc})")
                continue
            if stat.S_ISLNK(st.st_mode):
                if full_dir not in seen:
                    seen.add(full_dir)
                    entries.append(full_dir)
            else:
                kept_dirnames.append(dirname)
        dirnames[:] = kept_dirnames

        for filename in filenames:
            if prune and _is_pruned_name(filename):
                continue
            full_file = dirpath / filename
            try:
                os.lstat(full_file)
            except OSError as exc:
                if scan_errors is not None:
                    scan_errors.append(f"{full_file}: inaccessible entry ({exc.strerror or exc})")
                continue
            if full_file not in seen:
                seen.add(full_file)
                entries.append(full_file)


def _run_git_ls_files(
    repo_abs: str, extra_args: Sequence[str]
) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(
            [
                "git",
                "--no-pager",
                "-c",
                "core.fsmonitor=false",
                "-C",
                repo_abs,
                "ls-files",
                "-z",
                *extra_args,
            ],
            capture_output=True,
            check=False,
        )
    except OSError:
        return None


def iter_repo_entries(
    repo_root: Path,
    *,
    scan_errors: list[str] | None = None,
    extra_scan_dirs: set[Path] | None = None,
) -> list[Path]:
    """List repository files and symlinks, pruning only untracked cache directories."""
    repo_abs = os.path.abspath(str(repo_root))
    if _safe_exists(repo_root / ".git"):
        cached_proc = _run_git_ls_files(repo_abs, ("--cached",))
        others_proc = _run_git_ls_files(repo_abs, ("--others", "--exclude-standard"))
        if (
            cached_proc is not None
            and others_proc is not None
            and cached_proc.returncode == 0
            and others_proc.returncode == 0
        ):
            for proc in (cached_proc, others_proc):
                if scan_errors is not None and proc.stderr:
                    stderr_text = proc.stderr.decode("utf-8", errors="replace")
                    for line in stderr_text.splitlines():
                        cleaned = line.strip()
                        if cleaned:
                            scan_errors.append(f"git ls-files: {cleaned}")

            seen: set[Path] = set()
            entries: list[Path] = []
            for proc, should_prune in ((cached_proc, False), (others_proc, True)):
                for raw_item in proc.stdout.split(b"\x00"):
                    if not raw_item:
                        continue
                    rel_path = Path(os.fsdecode(raw_item))
                    if should_prune and any(_is_pruned_name(part) for part in rel_path.parts):
                        continue
                    full_path = repo_root / rel_path
                    try:
                        st = os.lstat(full_path)
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        if scan_errors is not None:
                            scan_errors.append(
                                f"{full_path}: inaccessible entry ({exc.strerror or exc})"
                            )
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        if extra_scan_dirs is not None:
                            extra_scan_dirs.add(full_path)
                        _walk_directory_entries(
                            full_path,
                            seen=seen,
                            entries=entries,
                            scan_errors=scan_errors,
                            prune=should_prune,
                        )
                    elif full_path not in seen:
                        seen.add(full_path)
                        entries.append(full_path)
            entries.sort(key=lambda p: p.relative_to(repo_root).as_posix())
            return entries

    seen = set()
    entries = []
    _walk_directory_entries(
        repo_root, seen=seen, entries=entries, scan_errors=scan_errors, prune=True
    )
    entries.sort(key=lambda p: p.relative_to(repo_root).as_posix())
    return entries


def _find_nested_git_entries(
    repo_root: Path,
    *,
    entries: Sequence[Path] = (),
    extra_dirs: Sequence[Path] = (),
) -> list[Path]:
    """Find any nested .git (case-insensitive) directories or files below repo_root."""
    nested: set[Path] = set()
    cache_pruned = PRUNED_ENTRY_NAMES - {".git"}

    def scan_subtree(start_root: Path, *, prune_caches: bool) -> None:
        for dirpath_str, dirnames, filenames in os.walk(start_root, followlinks=False):
            dirpath = Path(dirpath_str)
            is_repo_root = dirpath == repo_root
            kept: list[str] = []
            for dirname in dirnames:
                if dirname.lower() == ".git":
                    if not is_repo_root:
                        nested.add(dirpath / dirname)
                    continue
                if prune_caches and dirname in cache_pruned:
                    continue
                if not _safe_is_symlink(dirpath / dirname):
                    kept.append(dirname)
            dirnames[:] = kept
            for filename in filenames:
                if filename.lower() == ".git" and not is_repo_root:
                    nested.add(dirpath / filename)

    scan_subtree(repo_root, prune_caches=True)

    # Also check parent directories of enumerated (tracked/unignored) entries and extra dirs
    # so tracked node_modules/ trees cannot conceal nested .git entries.
    dirs_to_probe: set[Path] = set(extra_dirs)
    for entry in entries:
        cur = entry.parent
        while cur != repo_root and cur.parent != cur:
            dirs_to_probe.add(cur)
            cur = cur.parent
    for probe_dir in dirs_to_probe:
        if probe_dir == repo_root or _safe_is_symlink(probe_dir):
            continue
        try:
            for child in probe_dir.iterdir():
                if child.name.lower() == ".git":
                    nested.add(child)
        except OSError:
            continue

    return sorted(nested, key=lambda p: p.relative_to(repo_root).as_posix())


def _strip_inline_code_on_line(line: str) -> str:
    """Replace inline backtick code spans with spaces in O(len(line)) time."""
    if "`" not in line:
        return line

    runs: list[tuple[int, int, int, bool]] = []
    by_len: dict[int, deque[int]] = defaultdict(deque)
    idx = 0
    length = len(line)
    while idx < length:
        if line[idx] == "`":
            start = idx
            while idx < length and line[idx] == "`":
                idx += 1
            run_len = idx - start
            escaped = _preceded_by_odd_backslashes(line, start)
            by_len[run_len].append(len(runs))
            runs.append((start, idx, run_len, not escaped))
        else:
            idx += 1

    if not runs:
        return line

    chars = list(line)
    run_idx = 0
    total_runs = len(runs)
    while run_idx < total_runs:
        start, _end, run_len, can_open = runs[run_idx]
        if not can_open:
            run_idx += 1
            continue
        positions = by_len[run_len]
        while positions and positions[0] <= run_idx:
            positions.popleft()
        if positions:
            close_run_idx = positions.popleft()
            close_end = runs[close_run_idx][1]
            for pos in range(start, close_end):
                chars[pos] = " "
            run_idx = close_run_idx + 1
        else:
            run_idx += 1

    return "".join(chars)


def _strip_blockquote_prefix(line: str, depth: int) -> str | None:
    """Strip exactly depth leading '>' blockquote markers, or return None if fewer exist."""
    pos = 0
    length = len(line)
    for _ in range(depth):
        while pos < length and line[pos] in (" ", "\t"):
            pos += 1
        if pos >= length or line[pos] != ">":
            return None
        pos += 1
        if pos < length and line[pos] in (" ", "\t"):
            pos += 1
    return line[pos:]


def strip_markdown_code(text: str) -> list[str]:
    """Return lines of markdown with fenced code blocks and inline code spans blanked out."""
    fence_char: str | None = None
    fence_len = 0
    fence_bq_depth = 0
    cleaned_lines: list[str] = []

    for line in text.splitlines():
        if fence_char is not None:
            inner = _strip_blockquote_prefix(line, fence_bq_depth) if fence_bq_depth > 0 else line
            if inner is None:
                fence_char = None
                fence_len = 0
                fence_bq_depth = 0
            else:
                close_pattern = rf"^[ \t]*{re.escape(fence_char)}{{{fence_len},}}[ \t]*$"
                if re.match(close_pattern, inner):
                    fence_char = None
                    fence_len = 0
                    fence_bq_depth = 0
                cleaned_lines.append("")
                continue

        open_match = FENCE_OPEN_RE.match(line)
        if open_match:
            bq_prefix = open_match.group(1)
            marker = open_match.group(2)
            info = open_match.group(3)
            if not (marker[0] == "`" and "`" in info):
                fence_char = marker[0]
                fence_len = len(marker)
                fence_bq_depth = bq_prefix.count(">")
                cleaned_lines.append("")
                continue

        cleaned_lines.append(_strip_inline_code_on_line(line))

    return cleaned_lines


def _unescape_commonmark(text: str) -> str:
    """Unescape CommonMark backslash-escaped ASCII punctuation."""
    if "\\" not in text:
        return text
    out: list[str] = []
    idx = 0
    length = len(text)
    while idx < length:
        if (
            text[idx] == "\\"
            and idx + 1 < length
            and text[idx + 1] in COMMONMARK_ASCII_PUNCT
        ):
            out.append(text[idx + 1])
            idx += 2
        else:
            out.append(text[idx])
            idx += 1
    return "".join(out)


def _precompute_bare_dest_segments(line: str) -> tuple[dict[int, int], dict[int, int]]:
    """Precompute O(1) bare-destination endings across all non-whitespace segments in O(N)."""
    seg_close: dict[int, int] = {}
    seg_balanced_end: dict[int, int] = {}
    length = len(line)
    pos = 0

    while pos < length:
        while pos < length and line[pos] in (" ", "\t"):
            pos += 1
        if pos >= length:
            break
        seg_start = pos
        depth = 0
        stack: list[tuple[int, int]] = []
        while pos < length and line[pos] not in (" ", "\t"):
            ch = line[pos]
            stack.append((pos, depth))
            if ch == "\\":
                pos += 2 if (pos + 1 < length and line[pos + 1] not in (" ", "\t")) else 1
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                while stack and stack[-1][1] >= depth:
                    open_pos, _ = stack.pop()
                    seg_close[open_pos] = pos
                depth -= 1
            pos += 1
        seg_end = pos
        for open_pos, open_depth in stack:
            if open_depth == depth:
                seg_balanced_end[open_pos] = seg_end
        if seg_end == seg_start:
            pos += 1

    return (seg_close, seg_balanced_end)


def _parse_title_and_close_paren(
    line: str, pos: int, no_more_closer: set[str]
) -> int | None:
    """Parse optional whitespace + CommonMark link title + closing ')' starting at pos."""
    length = len(line)
    ws_start = pos
    while pos < length and line[pos] in (" ", "\t"):
        pos += 1
    if pos >= length:
        return None
    if line[pos] == ")":
        return pos
    if pos == ws_start:
        return None

    opener = line[pos]
    if opener not in ('"', "'", "("):
        return None
    closer = ")" if opener == "(" else opener
    if closer in no_more_closer:
        return None

    scan = pos + 1
    while scan < length:
        ch = line[scan]
        if ch == "\\":
            scan += 2
            continue
        if opener == "(" and ch == "(":
            return None
        if ch == closer:
            scan += 1
            while scan < length and line[scan] in (" ", "\t"):
                scan += 1
            return scan if (scan < length and line[scan] == ")") else None
        scan += 1

    no_more_closer.add(closer)
    return None


def _iter_markdown_links_on_line(line: str) -> list[str]:
    """Extract unescaped inline markdown link destinations on a single line in O(len(line))."""
    if "](" not in line:
        return []

    seg_close, seg_balanced_end = _precompute_bare_dest_segments(line)
    results: list[str] = []
    open_brackets: list[int] = []
    length = len(line)
    idx = 0
    no_more_gt = False
    no_more_closer: set[str] = set()

    while idx < length:
        ch = line[idx]
        if ch == "\\":
            idx += 2
            continue
        if ch == "[":
            open_brackets.append(idx)
            idx += 1
            continue
        if ch == "]":
            if not open_brackets:
                idx += 1
                continue
            open_brackets.pop()
            if idx + 1 < length and line[idx + 1] == "(":
                scan = idx + 2
                while scan < length and line[scan] in (" ", "\t"):
                    scan += 1

                if scan < length and line[scan] == ")":
                    results.append("")
                    idx = scan + 1
                    continue

                if scan < length and line[scan] == "<":
                    if not no_more_gt:
                        gt_pos = scan + 1
                        found_gt = False
                        aborted_lt = False
                        while gt_pos < length:
                            gch = line[gt_pos]
                            if gch == "\\":
                                gt_pos += 2
                                continue
                            if gch == "<":
                                aborted_lt = True
                                break
                            if gch == ">":
                                found_gt = True
                                break
                            gt_pos += 1
                        if found_gt:
                            close_paren = _parse_title_and_close_paren(
                                line, gt_pos + 1, no_more_closer
                            )
                            if close_paren is not None:
                                raw_dest = line[scan + 1 : gt_pos].strip()
                                results.append(_unescape_commonmark(raw_dest))
                                idx = close_paren + 1
                                continue
                        elif not aborted_lt:
                            no_more_gt = True
                    idx += 1
                    continue

                if scan in seg_close:
                    end_paren = seg_close[scan]
                    results.append(_unescape_commonmark(line[scan:end_paren]))
                    idx = end_paren + 1
                    continue

                if scan in seg_balanced_end:
                    dest_end = seg_balanced_end[scan]
                    close_paren = _parse_title_and_close_paren(line, dest_end, no_more_closer)
                    if close_paren is not None:
                        results.append(_unescape_commonmark(line[scan:dest_end]))
                        idx = close_paren + 1
                        continue
        idx += 1

    return results


def _check_skill_container(container: Path, repo_root: Path) -> tuple[set[str], list[str]]:
    """Validate all skill directories inside a container (e.g. shared/ or harnesses/agy/)."""
    errors: list[str] = []
    skill_names: set[str] = set()
    rel_container = container.relative_to(repo_root).as_posix()

    if _safe_is_symlink(container):
        return (skill_names, [f"{rel_container}: skill container must not be a symlink"])
    if _safe_exists(container) and not _safe_is_dir(container):
        return (skill_names, [f"{rel_container}: skill container must be a directory"])
    if not _safe_exists(container):
        return (skill_names, errors)

    children, list_err = _iter_visible_entries_checked(container)
    if list_err is not None:
        return (skill_names, [list_err])

    for child in children:
        rel = child.relative_to(repo_root).as_posix()
        if _safe_is_symlink(child):
            errors.append(f"{rel}: skill directory must not be a symlink")
        elif _safe_is_dir(child):
            skill_names.add(child.name)
            errors.extend(validate_skill_dir(child))
        else:
            errors.append(f"{rel}: stray non-directory entry inside skill container")
    return (skill_names, errors)


def cmd_doctor(repo_root: Path) -> int:
    """Run repository integrity checks without mutating the repository."""
    if not _safe_is_dir(repo_root):
        print(f"ERROR: repository path is not a directory: {repo_root}", file=sys.stderr)
        return 1

    repo_root_res = safe_resolve(repo_root, strict=True)
    if repo_root_res is None:
        print(f"ERROR: failed to resolve repository path: {repo_root}", file=sys.stderr)
        return 1

    errors: list[str] = []
    scan_errors: list[str] = []
    extra_scan_dirs: set[Path] = set()
    entries = iter_repo_entries(
        repo_root, scan_errors=scan_errors, extra_scan_dirs=extra_scan_dirs
    )
    errors.extend(scan_errors)

    for nested_git in _find_nested_git_entries(
        repo_root, entries=entries, extra_dirs=tuple(extra_scan_dirs)
    ):
        rel = nested_git.relative_to(repo_root).as_posix()
        errors.append(f"{rel}: nested .git entry is not allowed inside repository")

    # Skill frontmatter & structure across shared/* and harnesses/*/*
    shared_names, shared_errs = _check_skill_container(repo_root / "shared", repo_root)
    total_skills = len(shared_names)
    errors.extend(shared_errs)

    harnesses_root = repo_root / "harnesses"
    if _safe_is_symlink(harnesses_root):
        errors.append("harnesses: directory must not be a symlink")
    elif _safe_exists(harnesses_root) and not _safe_is_dir(harnesses_root):
        errors.append("harnesses: path exists and is not a directory")
    elif _safe_is_dir(harnesses_root):
        harness_children, h_err = _iter_visible_entries_checked(harnesses_root)
        if h_err is not None:
            errors.append(h_err)
        for harness_dir in harness_children:
            rel = harness_dir.relative_to(repo_root).as_posix()
            if _safe_is_symlink(harness_dir) or not _safe_is_dir(harness_dir):
                errors.append(f"{rel}: stray non-directory or symlink entry under harnesses/")
                continue
            harness_names, harness_errs = _check_skill_container(harness_dir, repo_root)
            total_skills += len(harness_names)
            errors.extend(harness_errs)
            for dup in sorted(shared_names & harness_names):
                errors.append(f"{rel}/{dup}: duplicate skill name also exists in shared/{dup}")

    # Symlink confinement & integrity check
    symlink_count = 0
    for entry in entries:
        if not _safe_is_symlink(entry):
            continue
        symlink_count += 1
        err = _validate_symlink_within_repo(entry, repo_root, repo_root_res)
        if err is not None:
            errors.append(err)

    utf8_cache: dict[Path, str | None] = {}

    def read_utf8_once(path: Path, rel_label: str) -> str | None:
        if path in utf8_cache:
            return utf8_cache[path]
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"{rel_label}: failed to read UTF-8 file: {exc}")
            utf8_cache[path] = None
            return None
        utf8_cache[path] = content
        return content

    # Relative markdown link validation
    md_count = 0
    for entry in entries:
        if _safe_is_symlink(entry) or not _safe_is_file(entry) or entry.suffix.lower() != ".md":
            continue
        md_count += 1
        rel = entry.relative_to(repo_root).as_posix()
        text = read_utf8_once(entry, rel)
        if text is None:
            continue

        start_dir = repo_root_res / entry.relative_to(repo_root).parent
        cleaned_lines = strip_markdown_code(text)
        for lineno, line in enumerate(cleaned_lines, start=1):
            for target in _iter_markdown_links_on_line(line):
                if not target:
                    errors.append(f"{rel}:{lineno}: empty markdown link target")
                    continue
                if target.startswith("#"):
                    continue
                if target.lower().startswith("file:"):
                    errors.append(
                        f"{rel}:{lineno}: non-portable file: markdown link {target!r}"
                    )
                    continue
                if URI_SCHEME_RE.match(target):
                    continue
                if target.startswith("/"):
                    errors.append(
                        f"{rel}:{lineno}: root-anchored markdown link {target!r} is non-portable"
                    )
                    continue
                rel_part = target.split("#", 1)[0].split("?", 1)[0]
                if not rel_part:
                    continue
                unquoted = unquote(rel_part)
                if rel_part.startswith("/") or unquoted.startswith("/"):
                    errors.append(
                        f"{rel}:{lineno}: root-anchored markdown link {target!r} is non-portable"
                    )
                    continue
                if "\x00" in rel_part or "\x00" in unquoted:
                    errors.append(f"{rel}:{lineno}: markdown link contains NUL byte: {target!r}")
                    continue

                resolved, link_err, missing_only = _resolve_confined_relative_path(
                    start_dir, rel_part, repo_root_res
                )
                if missing_only and "%" in rel_part:
                    resolved, link_err, _ = _resolve_confined_relative_path(
                        start_dir, unquoted, repo_root_res
                    )

                if link_err is not None or resolved is None:
                    errors.append(f"{rel}:{lineno}: invalid relative markdown link: {link_err}")

    # Hardcoded machine user path scan across .md, .py, .json files
    scanned_path_files = 0
    for entry in entries:
        if _safe_is_symlink(entry) or not _safe_is_file(entry):
            continue
        if entry.suffix.lower() not in PATH_SCAN_SUFFIXES:
            continue
        scanned_path_files += 1
        rel = entry.relative_to(repo_root).as_posix()
        text = read_utf8_once(entry, rel)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in HARDCODED_USER_PATH_RE.finditer(line):
                errors.append(
                    f"{rel}:{lineno}: hardcoded machine user path detected: {match.group(0)!r}"
                )

    # Syntax-check every .py file without writing __pycache__ into the repo
    py_files = [
        entry
        for entry in entries
        if not _safe_is_symlink(entry) and _safe_is_file(entry) and entry.suffix.lower() == ".py"
    ]
    with tempfile.TemporaryDirectory(prefix="agent-skills-doctor-") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        for idx, py_file in enumerate(py_files):
            rel = py_file.relative_to(repo_root).as_posix()
            try:
                py_compile.compile(
                    str(py_file),
                    cfile=str(tmp_dir / f"compiled_{idx}.pyc"),
                    doraise=True,
                )
            except py_compile.PyCompileError as exc:
                errors.append(f"{rel}: py_compile failed: {exc.msg}")
            except OSError as exc:
                errors.append(f"{rel}: failed to read python file for py_compile: {exc}")

    if errors:
        print(f"DOCTOR FAILED ({len(errors)} issue(s)):", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(
        f"DOCTOR OK: {total_skills} skill(s), {symlink_count} symlink(s), "
        f"{md_count} markdown file(s), {scanned_path_files} path-scanned file(s), "
        f"{len(py_files)} python file(s) validated."
    )
    return 0


def _add_repo_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo",
        default=argparse.SUPPRESS,
        metavar="DIR",
        help="Path to the agent-skills repository (defaults to directory containing init.py).",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="init.py",
        description="Manage multi-machine agent-skills installation, adoption, and verification.",
    )
    _add_repo_arg(parser)

    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser(
        "status",
        help="Show read-only status of repo skills and local machine bindings.",
    )
    _add_repo_arg(status_parser)
    status_parser.add_argument(
        "--home",
        default=None,
        metavar="DIR",
        help="Override target home directory (defaults to current user's home).",
    )

    apply_parser = subparsers.add_parser(
        "apply",
        help="Link ~/.agents/skills -> shared/ and merge Antigravity skills.json.",
    )
    _add_repo_arg(apply_parser)
    apply_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview planned actions without modifying the filesystem.",
    )
    apply_parser.add_argument(
        "--replace",
        action="store_true",
        help="Back up conflicting ~/.agents/skills and colliding ~/.gemini/config/skills entries.",
    )
    apply_parser.add_argument(
        "--home",
        default=None,
        metavar="DIR",
        help="Override target home directory (defaults to current user's home).",
    )

    adopt_parser = subparsers.add_parser(
        "adopt",
        help="Import a loose skill directory into shared/ or a harness directory.",
    )
    _add_repo_arg(adopt_parser)
    adopt_parser.add_argument(
        "source_dir",
        help="Path to the source skill directory to adopt.",
    )
    adopt_parser.add_argument(
        "--scope",
        required=True,
        choices=("shared", *HARNESS_REGISTRY.keys()),
        help="Target skill scope within the repository.",
    )
    adopt_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing destination skill directory.",
    )

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Validate skill frontmatter, symlinks, markdown links, user paths, and Python syntax.",
    )
    _add_repo_arg(doctor_parser)

    return parser


def _resolve_cli_dir(raw: str | None, default: Path, label: str) -> Path | None:
    if raw is not None and ("\x00" in raw or not raw.strip()):
        print(f"ERROR: {label} path must not be empty or contain NUL bytes", file=sys.stderr)
        return None
    try:
        target = default if raw is None else Path(raw).expanduser()
    except RuntimeError as exc:
        print(f"ERROR: failed to expand {label} path {raw!r}: {exc}", file=sys.stderr)
        return None
    ancestor_err = _validate_dir_ancestors(target)
    if ancestor_err is not None:
        print(f"ERROR: invalid {label} path ({ancestor_err})", file=sys.stderr)
        return None
    resolved = safe_resolve(target)
    if resolved is None:
        print(f"ERROR: failed to resolve {label} path: {target}", file=sys.stderr)
        return None
    return resolved


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point returning an integer exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    default_repo = Path(__file__).resolve().parent
    repo_root = _resolve_cli_dir(getattr(args, "repo", None), default_repo, "--repo")
    if repo_root is None:
        return 1

    if args.command == "status":
        home = _resolve_cli_dir(args.home, Path.home(), "--home")
        if home is None:
            return 1
        return cmd_status(repo_root, home)

    if args.command == "apply":
        home = _resolve_cli_dir(args.home, Path.home(), "--home")
        if home is None:
            return 1
        return cmd_apply(repo_root, home, dry_run=args.dry_run, replace=args.replace)

    if args.command == "adopt":
        return cmd_adopt(repo_root, args.source_dir, scope=args.scope, force=args.force)

    if args.command == "doctor":
        return cmd_doctor(repo_root)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
