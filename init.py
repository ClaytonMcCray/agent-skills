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
from typing import NamedTuple
from urllib.parse import unquote


@dataclass(frozen=True)
class HarnessSpec:
    """Configuration describing a harness adapter and its local machine paths."""

    name: str
    repo_rel_dir: str
    config_rel_dir: str
    skills_dir_name: str
    install_mode: str = "symlink_farm"
    skills_json_name: str | None = None
    plugins_dir_name: str | None = None
    config_json_name: str | None = None
    global_agents_name: str = "AGENTS.md"


class ShadowingPlugin(NamedTuple):
    """A non-marketplace local plugin shadowing one or more repository skills."""

    plugin_path: Path
    colliding_skills: list[str]
    extra_unmigrated_skills: list[str]
    extra_runtime_components: tuple[str, ...] = ()


HARNESS_REGISTRY: dict[str, HarnessSpec] = {
    "agy": HarnessSpec(
        name="agy",
        repo_rel_dir="harnesses/agy",
        config_rel_dir=".gemini/config",
        skills_dir_name="skills",
        install_mode="agy_json",
        skills_json_name="skills.json",
        plugins_dir_name="plugins",
        config_json_name="config.json",
        global_agents_name="AGENTS.md",
    ),
    "claude": HarnessSpec(
        name="claude",
        repo_rel_dir="harnesses/claude",
        config_rel_dir=".claude",
        skills_dir_name="skills",
        install_mode="symlink_farm",
        global_agents_name="CLAUDE.md",
    ),
    "codex": HarnessSpec(
        name="codex",
        repo_rel_dir="harnesses/codex",
        config_rel_dir=".codex",
        skills_dir_name="skills",
        install_mode="symlink_farm",
        global_agents_name="AGENTS.md",
    ),
}

PRESERVED_SKILL_DIR_ENTRIES: dict[str, frozenset[str]] = {
    "claude": frozenset({"synced"}),
    "codex": frozenset(),
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
PATH_SCAN_SUFFIXES = frozenset({".md", ".py", ".json", ".toml"})
COMMONMARK_ASCII_PUNCT = frozenset(r"""!"#$%&'()*+,-./:;<=>?@[\]^_`{|}~""")
YAML_TOKEN_BOUNDARIES = frozenset({" ", "\t", "[", "{", ",", ":"})
MAX_FLOW_DEPTH = 32
GLOBAL_AGENTS_MAX_BYTES = 24_000
PLUGIN_RUNTIME_COMPONENT_NAMES = frozenset(
    {
        "mcp_config.json",
        "hooks.json",
        "hooks",
        "sidecars",
        "sidecars.json",
    }
)

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


def _harness_config_dir(home: Path, spec: HarnessSpec) -> Path:
    return home / spec.config_rel_dir


def _harness_skills_dir(home: Path, spec: HarnessSpec) -> Path:
    return home / spec.config_rel_dir / spec.skills_dir_name


def _harness_global_agents_path(home: Path, spec: HarnessSpec) -> Path:
    return home / spec.config_rel_dir / spec.global_agents_name


def _harness_display_prefix(spec: HarnessSpec) -> str:
    return f"~/{spec.config_rel_dir}"


def _preserved_skill_entries(harness_name: str) -> frozenset[str]:
    return PRESERVED_SKILL_DIR_ENTRIES.get(harness_name, frozenset())


def _reserved_skill_names_for_scope(scope: str) -> frozenset[str]:
    if scope == "shared":
        reserved: set[str] = set()
        for entries in PRESERVED_SKILL_DIR_ENTRIES.values():
            reserved.update(entries)
        return frozenset(reserved)
    return _preserved_skill_entries(scope)


def _agy_config_paths(home: Path) -> tuple[Path, Path, Path]:
    """Return (config_dir, skills_json_path, agy_skills_dir) for the Antigravity harness."""
    spec = HARNESS_REGISTRY["agy"]
    assert spec.skills_json_name is not None
    config_dir = _harness_config_dir(home, spec)
    return (
        config_dir,
        config_dir / spec.skills_json_name,
        config_dir / spec.skills_dir_name,
    )


def _agy_global_agents_path(home: Path) -> Path:
    return _harness_global_agents_path(home, HARNESS_REGISTRY["agy"])


def _agy_plugins_dir(home: Path) -> Path:
    spec = HARNESS_REGISTRY["agy"]
    assert spec.plugins_dir_name is not None
    return _harness_config_dir(home, spec) / spec.plugins_dir_name


def _agy_config_json_path(config_dir: Path) -> Path:
    config_json_name = HARNESS_REGISTRY["agy"].config_json_name
    assert config_json_name is not None
    return config_dir / config_json_name


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


def discover_harness_repo_skills(repo_root: Path, harness_name: str) -> dict[str, Path]:
    """Map skill names exposed to harness_name (shared + harnesses/<harness>) to repo dirs."""
    spec = HARNESS_REGISTRY[harness_name]
    skills: dict[str, Path] = {}
    for skill_dir in discover_skill_dirs(repo_root / "shared"):
        skills[skill_dir.name] = skill_dir
    for skill_dir in discover_skill_dirs(repo_root / spec.repo_rel_dir):
        skills[skill_dir.name] = skill_dir
    return skills


def _duplicate_harness_skill_names(repo_root: Path, harness_name: str) -> set[str]:
    spec = HARNESS_REGISTRY[harness_name]
    shared_names = {p.name for p in discover_skill_dirs(repo_root / "shared")}
    harness_names = {p.name for p in discover_skill_dirs(repo_root / spec.repo_rel_dir)}
    return shared_names & harness_names


def discover_agy_repo_skills(repo_root: Path) -> dict[str, Path]:
    """Map skill names exposed to Antigravity (shared + harnesses/agy) to repo dirs."""
    return discover_harness_repo_skills(repo_root, "agy")


def _duplicate_agy_skill_names(repo_root: Path) -> set[str]:
    return _duplicate_harness_skill_names(repo_root, "agy")


def format_skills_json_path(target: Path, home: Path | None = None) -> str:
    """Format a target path as an absolute POSIX path (never ~/...) for skills.json."""
    _ = home
    target_res = safe_resolve(target)
    if target_res is None:
        target_res = Path(os.path.abspath(str(target)))
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


def _desired_canonical_targets(repo_root: Path) -> set[str]:
    targets: set[str] = set()
    for desired in _desired_agy_targets(repo_root):
        resolved = safe_resolve(desired)
        if resolved is not None:
            targets.add(resolved.as_posix())
    return targets


def _is_tilde_path(raw_path: str) -> bool:
    return raw_path == "~" or raw_path.startswith("~/")


def _project_virtual_agents_path(
    candidate: Path,
    home: Path,
    virtual_agents_target: Path,
    *,
    strict: bool = False,
) -> tuple[bool, Path | None]:
    """Resolve an absolute candidate path with ~/.agents/skills redirected virtually.

    Returns (matched_virtual_link, resolved_path).
    """
    home_res = safe_resolve(home) or Path(os.path.abspath(str(home)))
    agents_dir_res = safe_resolve(home / ".agents") or (home_res / ".agents")
    virtual_link = Path(os.path.normpath(str(agents_dir_res / "skills")))

    parts = candidate.parts
    if not parts:
        return (False, None)
    cur = Path(parts[0])
    for idx in range(1, len(parts)):
        part = parts[idx]
        if part in ("", "."):
            continue
        if part == "..":
            cur = cur.parent
            continue
        nxt = cur / part
        if nxt == virtual_link:
            tail = parts[idx + 1 :]
            projected = virtual_agents_target.joinpath(*tail) if tail else virtual_agents_target
            return (True, safe_resolve(projected, strict=strict))
        if _safe_is_symlink(nxt):
            resolved_nxt = safe_resolve(nxt, strict=True)
            if resolved_nxt is None:
                return (False, None)
            cur = resolved_nxt
        else:
            cur = nxt
    return (False, None)


def _resolve_entry_path(
    raw_path: str, home: Path, *, virtual_agents_target: Path | None = None
) -> Path | None:
    """Resolve a skills.json entry path, optionally projecting ~/.agents/skills -> shared."""
    if virtual_agents_target is not None and "\x00" not in raw_path:
        candidate: Path | None = None
        if raw_path == "~":
            candidate = home
        elif raw_path.startswith("~/"):
            rel = raw_path[2:].lstrip("/")
            candidate = (home / rel) if rel else home
        elif raw_path.startswith("/"):
            candidate = Path(raw_path)

        if candidate is not None:
            matched, projected = _project_virtual_agents_path(
                candidate, home, virtual_agents_target, strict=False
            )
            if matched:
                return projected

    return resolve_configured_entry_path(raw_path, home)


def _normalize_tilde_entry_path(
    raw_path: str, home: Path, *, virtual_agents_target: Path | None = None
) -> str:
    """Resolve a '~' or '~/...' skills.json entry path to an absolute POSIX path."""
    resolved = _resolve_entry_path(raw_path, home, virtual_agents_target=virtual_agents_target)
    if resolved is not None:
        return resolved.as_posix()
    home_res = safe_resolve(home) or Path(os.path.abspath(str(home)))
    if raw_path == "~":
        return home_res.as_posix()
    rel = raw_path[2:].lstrip("/")
    target = (home_res / rel) if rel else home_res
    return Path(os.path.abspath(str(target))).as_posix()


class _SkillsJsonNormalization(NamedTuple):
    entries: list[object]
    changed: bool
    has_tilde: bool
    has_duplicates: bool
    has_non_canonical_repo: bool


def _plan_skills_json_normalization(
    data: dict[str, object],
    home: Path,
    *,
    canonical_targets: set[str] | frozenset[str] = frozenset(),
    virtual_agents_target: Path | None = None,
) -> _SkillsJsonNormalization:
    """Compute normalized/deduplicated skills.json entries without mutating data."""
    entries = data.get("entries")
    if not isinstance(entries, list):
        return _SkillsJsonNormalization([], False, False, False, False)

    changed = False
    has_tilde = False
    has_duplicates = False
    has_non_canonical_repo = False
    normalized: list[object] = []
    seen_idx: dict[tuple[str, str], int] = {}

    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            normalized.append(item)
            continue

        raw_path = item["path"]
        is_tilde = _is_tilde_path(raw_path)
        resolved = _resolve_entry_path(
            raw_path, home, virtual_agents_target=virtual_agents_target
        )
        if is_tilde:
            has_tilde = True
            new_path = _normalize_tilde_entry_path(
                raw_path, home, virtual_agents_target=virtual_agents_target
            )
        elif (
            resolved is not None
            and resolved.as_posix() in canonical_targets
            and raw_path != resolved.as_posix()
        ):
            has_non_canonical_repo = True
            new_path = resolved.as_posix()
        else:
            new_path = raw_path

        canon_path = resolved.as_posix() if resolved is not None else new_path
        other_fields = json.dumps(
            {k: v for k, v in item.items() if k != "path"},
            sort_keys=True,
            ensure_ascii=False,
        )
        key = (canon_path, other_fields)

        if key in seen_idx:
            has_duplicates = True
            changed = True
            prev_pos = seen_idx[key]
            prev_entry = normalized[prev_pos]
            if (
                isinstance(prev_entry, dict)
                and prev_entry.get("path") != canon_path
                and new_path == canon_path
            ):
                upgraded = dict(prev_entry)
                upgraded["path"] = canon_path
                normalized[prev_pos] = upgraded
            continue

        if new_path != raw_path:
            new_item: dict[str, object] = dict(item)
            new_item["path"] = new_path
            changed = True
        else:
            new_item = item

        seen_idx[key] = len(normalized)
        normalized.append(new_item)

    return _SkillsJsonNormalization(
        normalized, changed, has_tilde, has_duplicates, has_non_canonical_repo
    )


def _normalize_and_dedupe_skills_json_entries(
    data: dict[str, object],
    home: Path,
    *,
    canonical_targets: set[str] | frozenset[str] = frozenset(),
    virtual_agents_target: Path | None = None,
) -> bool:
    """Normalize tilde/alias paths and deduplicate identical entries in place."""
    plan = _plan_skills_json_normalization(
        data,
        home,
        canonical_targets=canonical_targets,
        virtual_agents_target=virtual_agents_target,
    )
    if plan.changed:
        data["entries"] = plan.entries
    return plan.changed


def _configured_resolved_paths(
    data: dict[str, object], home: Path, *, virtual_agents_target: Path | None = None
) -> set[Path]:
    resolved_paths: set[Path] = set()
    entries = data.get("entries")
    if not isinstance(entries, list):
        return resolved_paths
    for item in entries:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            resolved = _resolve_entry_path(
                item["path"], home, virtual_agents_target=virtual_agents_target
            )
            if resolved is not None:
                resolved_paths.add(resolved)
    return resolved_paths


def _missing_skills_json_entries(
    data: dict[str, object],
    repo_root: Path,
    home: Path,
    *,
    virtual_agents_target: Path | None = None,
) -> tuple[list[tuple[Path, str]], list[str]]:
    """Return (missing_targets_with_formatted_path, target_errors) for Antigravity."""
    configured = _configured_resolved_paths(
        data, home, virtual_agents_target=virtual_agents_target
    )
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


def _load_marketplace_plugin_names(config_dir: Path) -> tuple[set[str], str | None]:
    """Return (marketplace_plugin_names, error) from config.json, failing closed on corruption."""
    config_json_path = _agy_config_json_path(config_dir)
    try:
        st = os.lstat(config_json_path)
    except FileNotFoundError:
        return (set(), None)
    except OSError as exc:
        return (set(), f"{config_json_path}: inaccessible ({exc})")

    if stat.S_ISLNK(st.st_mode):
        resolved = safe_resolve(config_json_path, strict=True)
        if resolved is None:
            return (set(), f"{config_json_path}: broken or circular symlink")
        if not _safe_is_file(resolved):
            return (set(), f"{config_json_path}: symlink points to non-file ({resolved})")
        read_path = resolved
    elif not stat.S_ISREG(st.st_mode):
        return (set(), f"{config_json_path}: exists and is not a regular file")
    else:
        read_path = config_json_path

    try:
        raw_text = read_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return (set(), f"{config_json_path}: failed to read UTF-8 JSON: {exc}")

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return (
            set(),
            f"{config_json_path}: invalid JSON ({exc.msg} at line {exc.lineno}, col {exc.colno})",
        )

    if not isinstance(data, dict):
        return (set(), f"{config_json_path}: top-level JSON value must be an object")

    plugins = data.get("plugins")
    if plugins is None:
        return (set(), None)
    if not isinstance(plugins, dict):
        return (set(), f"{config_json_path}: 'plugins' must be a JSON object")

    marketplace: set[str] = set()
    for name, meta in plugins.items():
        if not isinstance(name, str) or not isinstance(meta, dict):
            return (set(), f"{config_json_path}: 'plugins[{name!r}]' must be a JSON object")
        if bool(meta.get("installedFrom")):
            marketplace.add(name)
    return (marketplace, None)


def _find_shadowing_agy_plugins(
    home: Path, repo_skill_map: dict[str, Path]
) -> tuple[list[ShadowingPlugin], str | None]:
    """Find non-marketplace local plugins in ~/.gemini/config/plugins/ shadowing repo skills."""
    config_dir, _, _ = _agy_config_paths(home)
    plugins_dir = _agy_plugins_dir(home)
    try:
        dir_st = os.lstat(plugins_dir)
    except FileNotFoundError:
        return ([], None)
    except OSError as exc:
        return ([], f"{plugins_dir}: inaccessible ({exc})")
    if stat.S_ISLNK(dir_st.st_mode) or not stat.S_ISDIR(dir_st.st_mode):
        return ([], None)

    marketplace_names, cfg_err = _load_marketplace_plugin_names(config_dir)
    if cfg_err is not None:
        return ([], cfg_err)
    plugin_entries, err = _iter_visible_entries_checked(plugins_dir)
    if err is not None:
        return ([], err)

    results: list[ShadowingPlugin] = []
    for plugin_entry in plugin_entries:
        if plugin_entry.name in marketplace_names:
            continue
        try:
            p_st = os.lstat(plugin_entry)
        except OSError as exc:
            return ([], f"{plugin_entry}: inaccessible ({exc})")
        if stat.S_ISLNK(p_st.st_mode):
            if not _safe_is_dir(plugin_entry):
                continue
        elif not stat.S_ISDIR(p_st.st_mode):
            continue

        plugin_skills_dir = plugin_entry / "skills"
        try:
            os.lstat(plugin_skills_dir)
        except FileNotFoundError:
            continue
        except OSError as exc:
            return ([], f"{plugin_skills_dir}: inaccessible ({exc})")
        if not _safe_is_dir(plugin_skills_dir):
            continue

        skill_entries, s_err = _iter_visible_entries_checked(plugin_skills_dir)
        if s_err is not None:
            return ([], s_err)

        colliding_skills: list[str] = []
        extra_unmigrated_skills: list[str] = []
        for skill_entry in skill_entries:
            try:
                s_st = os.lstat(skill_entry)
            except OSError as exc:
                return ([], f"{skill_entry}: inaccessible ({exc})")
            if not (stat.S_ISDIR(s_st.st_mode) or stat.S_ISLNK(s_st.st_mode)):
                continue
            if skill_entry.name in repo_skill_map:
                colliding_skills.append(skill_entry.name)
            else:
                extra_unmigrated_skills.append(skill_entry.name)

        if colliding_skills:
            plugin_children, child_err = _iter_visible_entries_checked(plugin_entry)
            if child_err is not None:
                return ([], child_err)
            extra_runtime_components = tuple(
                child.name
                for child in plugin_children
                if child.name in PLUGIN_RUNTIME_COMPONENT_NAMES
            )
            results.append(
                ShadowingPlugin(
                    plugin_entry,
                    colliding_skills,
                    extra_unmigrated_skills,
                    extra_runtime_components,
                )
            )

    return (results, None)


def _collect_skill_tree_entries(root: Path) -> dict[str, tuple[str, Path]] | None:
    """Collect non-pruned relative entries (kind, path) beneath root without following symlinks."""
    entries: dict[str, tuple[str, Path]] = {}
    had_error = False

    def on_walk_error(_exc: OSError) -> None:
        nonlocal had_error
        had_error = True

    for dirpath_str, dirnames, filenames in os.walk(
        root, followlinks=False, onerror=on_walk_error
    ):
        dirpath = Path(dirpath_str)
        kept: list[str] = []
        for dirname in dirnames:
            if _is_pruned_name(dirname) or dirname.startswith(".adopt-"):
                continue
            full_dir = dirpath / dirname
            rel = full_dir.relative_to(root).as_posix()
            try:
                st = os.lstat(full_dir)
            except OSError:
                return None
            if stat.S_ISLNK(st.st_mode):
                entries[rel] = ("symlink", full_dir)
            else:
                entries[rel] = ("dir", full_dir)
                kept.append(dirname)
        dirnames[:] = kept

        for filename in filenames:
            if _is_pruned_name(filename) or filename.startswith(".adopt-"):
                continue
            full_file = dirpath / filename
            rel = full_file.relative_to(root).as_posix()
            try:
                st = os.lstat(full_file)
            except OSError:
                return None
            if stat.S_ISLNK(st.st_mode):
                entries[rel] = ("symlink", full_file)
            elif stat.S_ISREG(st.st_mode):
                entries[rel] = ("file", full_file)
            else:
                entries[rel] = ("other", full_file)

    return None if had_error else entries


def _files_differ(path_a: Path, path_b: Path) -> bool:
    """Return True if two regular files differ in user exec bit, size, or content bytes."""
    try:
        st_a = os.lstat(path_a)
        st_b = os.lstat(path_b)
        if bool(st_a.st_mode & stat.S_IXUSR) != bool(st_b.st_mode & stat.S_IXUSR):
            return True
        if st_a.st_size != st_b.st_size:
            return True
        with path_a.open("rb") as fa, path_b.open("rb") as fb:
            while True:
                chunk_a = fa.read(65536)
                chunk_b = fb.read(65536)
                if chunk_a != chunk_b:
                    return True
                if not chunk_a:
                    return False
    except OSError:
        return True


def _skill_trees_differ(dir_a: Path, dir_b: Path) -> bool:
    """Return True if two skill directory trees differ in non-pruned files or symlinks."""
    res_a = safe_resolve(dir_a, strict=True)
    res_b = safe_resolve(dir_b, strict=True)
    if res_a is None or res_b is None:
        return True
    if not _safe_is_dir(res_a) or not _safe_is_dir(res_b):
        return True
    if res_a == res_b:
        return False

    entries_a = _collect_skill_tree_entries(res_a)
    entries_b = _collect_skill_tree_entries(res_b)
    if entries_a is None or entries_b is None:
        return True
    if entries_a.keys() != entries_b.keys():
        return True

    for rel, (kind_a, path_a) in entries_a.items():
        kind_b, path_b = entries_b[rel]
        if kind_a != kind_b:
            return True
        if kind_a == "symlink":
            try:
                if os.readlink(path_a) != os.readlink(path_b):
                    return True
            except OSError:
                return True
        elif kind_a == "file":
            if _files_differ(path_a, path_b):
                return True
        elif kind_a == "other":
            return True
    return False


def _scan_agents_skills_dir(
    agents_skills: Path, shared_dir: Path
) -> tuple[list[str], list[str], list[str]]:
    """Compare a real ~/.agents/skills directory against repo shared/.

    Returns (unmigrated_names, differing_names, warnings).
    """
    existing_entries, list_err = _iter_visible_entries_checked(agents_skills)
    if list_err is not None:
        return ([], [], [list_err])

    shared_map = {p.name: p for p in discover_skill_dirs(shared_dir)}
    unmigrated: list[str] = []
    differing: list[str] = []
    warnings: list[str] = []
    for entry in existing_entries:
        try:
            entry_st = os.lstat(entry)
        except OSError as exc:
            warnings.append(f"could not inspect {entry}: {exc}")
            continue
        if not (stat.S_ISDIR(entry_st.st_mode) or stat.S_ISLNK(entry_st.st_mode)):
            continue
        if entry.name not in shared_map:
            unmigrated.append(entry.name)
        elif _skill_trees_differ(entry, shared_map[entry.name]):
            differing.append(entry.name)
    return (sorted(unmigrated), sorted(differing), warnings)


def _inspect_shadow_container_status(label: str, container_dir: Path) -> bool:
    """Print status for non-scannable shadow containers; return True when ready to scan."""
    try:
        st = os.lstat(container_dir)
    except FileNotFoundError:
        print(f"{label} ({container_dir}): no shadowing collisions")
        return False
    except OSError as exc:
        print(f"{label} ({container_dir}): invalid (inaccessible: {exc})")
        return False

    if stat.S_ISLNK(st.st_mode):
        raw_link = _safe_readlink(container_dir)
        resolved = safe_resolve(container_dir, strict=True)
        if resolved is None:
            print(f"{label} ({container_dir}): conflict (broken or circular symlink -> {raw_link})")
        else:
            print(f"{label} ({container_dir}): symlink -> {resolved} (not traversed for shadowing)")
        return False
    if not stat.S_ISDIR(st.st_mode):
        print(f"{label} ({container_dir}): conflict (non-directory file)")
        return False
    return True


def _preflight_shadow_container_apply(
    container_dir: Path, scan_label: str, *, replace: bool
) -> tuple[bool, bool]:
    """Validate a shadow container before apply; return (should_scan, status_ok)."""
    try:
        st = os.lstat(container_dir)
    except FileNotFoundError:
        return (False, True)
    except OSError as exc:
        print(f"ERROR: cannot inspect {container_dir}: {exc}", file=sys.stderr)
        return (False, False)

    if stat.S_ISLNK(st.st_mode):
        raw_link = _safe_readlink(container_dir)
        if not replace:
            print(
                f"WARNING: {container_dir} is a symlink (-> {raw_link}); "
                f"skipping {scan_label} collision scan.",
                file=sys.stderr,
            )
            return (False, True)
        print(
            f"ERROR: {container_dir} is a symlink (-> {raw_link}); "
            "refusing to move entries through symlinked directory.",
            file=sys.stderr,
        )
        return (False, False)

    if not stat.S_ISDIR(st.st_mode):
        if not replace:
            print(
                f"WARNING: {container_dir} exists as a non-directory file; "
                f"skipping {scan_label} collision scan.",
                file=sys.stderr,
            )
            return (False, True)
        print(
            f"ERROR: {container_dir} exists as a non-directory file; cannot inspect shadows.",
            file=sys.stderr,
        )
        return (False, False)

    return (True, True)


def _global_agents_status(agents_md_path: Path, expected_global_agents: Path | None) -> str:
    """Return a read-only status string for a harness global instruction file."""
    try:
        agents_md_st = os.lstat(agents_md_path)
    except FileNotFoundError:
        return "not installed"
    except OSError as exc:
        return f"invalid (inaccessible: {exc})"

    if stat.S_ISLNK(agents_md_st.st_mode):
        resolved_md = safe_resolve(agents_md_path, strict=True)
        raw_md_link = _safe_readlink(agents_md_path)
        if resolved_md is None:
            return f"conflict (broken or circular symlink -> {raw_md_link})"
        if expected_global_agents is not None and resolved_md == expected_global_agents:
            return f"ok (symlink -> {resolved_md})"
        return f"conflict (symlink -> {resolved_md}, expected {expected_global_agents})"
    if stat.S_ISREG(agents_md_st.st_mode):
        return (
            "conflict (regular file; run 'init.py apply --replace' to back up, "
            "or pass '--no-global-agents' to keep)"
        )
    if stat.S_ISDIR(agents_md_st.st_mode):
        return (
            "conflict (directory; run 'init.py apply --replace' to back up, "
            "or pass '--no-global-agents' to keep)"
        )
    return (
        "conflict (non-regular file; run 'init.py apply --replace' to back up, "
        "or pass '--no-global-agents' to keep)"
    )


def _is_strictly_inside(path: Path, root: Path) -> bool:
    return path != root and path.is_relative_to(root)


def _resolve_symlink_live_target(
    link_path: Path,
    *,
    home: Path | None = None,
    virtual_agents_target: Path | None = None,
) -> Path | None:
    """Resolve a symlink's live target, projecting ~/.agents/skills when virtualized."""
    if virtual_agents_target is not None and home is not None:
        try:
            raw_target = os.readlink(link_path)
        except OSError:
            return None
        if not raw_target or "\x00" in raw_target:
            return None
        parent_res = safe_resolve(link_path.parent, strict=True)
        if parent_res is None:
            return None
        raw_candidate = (
            Path(raw_target) if os.path.isabs(raw_target) else (parent_res / raw_target)
        )
        matched, projected = _project_virtual_agents_path(
            raw_candidate, home, virtual_agents_target, strict=True
        )
        if matched:
            return projected
    return safe_resolve(link_path, strict=True)


def _is_repo_owned_symlink(
    link_path: Path,
    repo_root: Path,
    repo_root_res: Path,
    *,
    home: Path | None = None,
    virtual_agents_target: Path | None = None,
) -> bool:
    """Return True if symlink link_path targets a path strictly inside the repository."""
    try:
        raw_target = os.readlink(link_path)
    except OSError:
        return False
    if not raw_target or "\x00" in raw_target:
        return False

    parent_res = safe_resolve(link_path.parent, strict=True) or link_path.parent
    raw_candidate = Path(raw_target) if os.path.isabs(raw_target) else (parent_res / raw_target)

    if virtual_agents_target is not None and home is not None:
        matched, projected = _project_virtual_agents_path(
            raw_candidate, home, virtual_agents_target, strict=False
        )
        if matched:
            return projected is not None and _is_strictly_inside(projected, repo_root_res)

    resolved_link = safe_resolve(link_path, strict=True)
    if resolved_link is not None:
        return _is_strictly_inside(resolved_link, repo_root_res)

    resolved = safe_resolve(raw_candidate, strict=False)
    if resolved is not None:
        return _is_strictly_inside(resolved, repo_root_res)

    repo_abs = Path(os.path.abspath(str(repo_root)))
    fallback = Path(os.path.abspath(str(raw_candidate)))
    return _is_strictly_inside(fallback, repo_root_res) or _is_strictly_inside(fallback, repo_abs)


def _status_agy_harness(
    repo_root: Path, home: Path, expected_global_agents: Path | None
) -> None:
    """Print read-only Antigravity status lines."""
    config_dir, skills_json_path, agy_skills_dir = _agy_config_paths(home)
    agents_md_path = _agy_global_agents_path(home)
    plugins_dir = _agy_plugins_dir(home)
    repo_agy_skills = discover_agy_repo_skills(repo_root)
    config_ancestor_err = _validate_dir_ancestors(config_dir, stop_at=home)
    if config_ancestor_err is not None:
        print(f"~/.gemini/config/skills.json ({skills_json_path}): invalid ({config_ancestor_err})")
        print(f"~/.gemini/config/AGENTS.md ({agents_md_path}): invalid ({config_ancestor_err})")
        print(f"~/.gemini/config/skills ({agy_skills_dir}): invalid ({config_ancestor_err})")
        print(f"~/.gemini/config/plugins ({plugins_dir}): invalid ({config_ancestor_err})")
        return

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
                norm_plan = _plan_skills_json_normalization(
                    data, home, canonical_targets=_desired_canonical_targets(repo_root)
                )
                hidden = _hidden_repo_skills(data, repo_agy_skills, home)
                if target_errors:
                    status_detail = f"invalid repo target ({'; '.join(target_errors)})"
                else:
                    issues: list[str] = []
                    if missing:
                        missing_labels = ", ".join(formatted for _, formatted in missing)
                        issues.append(f"missing entries ({missing_labels})")
                    if norm_plan.has_tilde:
                        issues.append(
                            "needs path normalization "
                            "(non-absolute '~/...' entries rejected by Antigravity)"
                        )
                    if norm_plan.has_non_canonical_repo:
                        issues.append(
                            "needs path normalization (non-canonical repo alias entries)"
                        )
                    if norm_plan.has_duplicates:
                        issues.append("needs deduplication (duplicate entries)")
                    if issues:
                        status_detail = "; ".join(issues)
                        if hidden:
                            status_detail += (
                                f"; filtered out by include_only/exclude: {', '.join(hidden)}"
                            )
                    elif hidden:
                        status_detail = (
                            f"configured (filtered out by include_only/exclude: "
                            f"{', '.join(hidden)})"
                        )
                    else:
                        status_detail = "configured"
                print(
                    f"~/.gemini/config/skills.json ({skills_json_path}){link_note}: {status_detail}"
                )

    md_status = _global_agents_status(agents_md_path, expected_global_agents)
    print(f"~/.gemini/config/AGENTS.md ({agents_md_path}): {md_status}")

    if _inspect_shadow_container_status("~/.gemini/config/skills", agy_skills_dir):
        shadowing, scan_err = _find_shadowing_agy_skills(home, repo_agy_skills)
        if scan_err is not None:
            print(f"~/.gemini/config/skills ({agy_skills_dir}): invalid ({scan_err})")
        elif shadowing:
            names = ", ".join(entry.name for entry in shadowing)
            print(f"~/.gemini/config/skills ({agy_skills_dir}): shadowing repo skills ({names})")
        else:
            print(f"~/.gemini/config/skills ({agy_skills_dir}): no shadowing collisions")

    if _inspect_shadow_container_status("~/.gemini/config/plugins", plugins_dir):
        shadowing_plugins, plugin_err = _find_shadowing_agy_plugins(home, repo_agy_skills)
        if plugin_err is not None:
            print(f"~/.gemini/config/plugins ({plugins_dir}): invalid ({plugin_err})")
        elif shadowing_plugins:
            parts: list[str] = []
            for sp in shadowing_plugins:
                label = f"{sp.plugin_path.name}: {', '.join(sp.colliding_skills)}"
                blockers: list[str] = []
                if sp.extra_unmigrated_skills:
                    blockers.append(f"unmigrated: {', '.join(sp.extra_unmigrated_skills)}")
                if sp.extra_runtime_components:
                    blockers.append(
                        f"non-skill components: {', '.join(sp.extra_runtime_components)}"
                    )
                if blockers:
                    label += f" ({'; '.join(blockers)})"
                parts.append(label)
            print(
                f"~/.gemini/config/plugins ({plugins_dir}): "
                f"shadowing repo skills ({'; '.join(parts)})"
            )
        else:
            print(f"~/.gemini/config/plugins ({plugins_dir}): no shadowing collisions")


def _status_symlink_farm_harness(
    spec: HarnessSpec,
    repo_root: Path,
    repo_root_res: Path,
    home: Path,
    expected_global_agents: Path | None,
) -> None:
    """Print read-only status lines for a per-skill symlink-farm harness."""
    config_dir = _harness_config_dir(home, spec)
    skills_dir = _harness_skills_dir(home, spec)
    agents_md_path = _harness_global_agents_path(home, spec)
    display_prefix = _harness_display_prefix(spec)
    skills_label = f"{display_prefix}/{spec.skills_dir_name}"
    agents_label = f"{display_prefix}/{spec.global_agents_name}"

    config_ancestor_err = _validate_dir_ancestors(config_dir, stop_at=home)
    if config_ancestor_err is not None:
        print(f"{skills_label} ({skills_dir}): invalid ({config_ancestor_err})")
        print(f"{agents_label} ({agents_md_path}): invalid ({config_ancestor_err})")
        return

    container_errs: list[str] = []
    for rel_parts in (("shared",), Path(spec.repo_rel_dir).parts):
        err = _validate_in_repo_container(repo_root_res, rel_parts)
        if err is not None:
            container_errs.append(err)
    if not container_errs:
        for container in (repo_root / "shared", repo_root / spec.repo_rel_dir):
            if _safe_exists(container):
                _, container_list_err = _iter_visible_entries_checked(container)
                if container_list_err is not None:
                    container_errs.append(container_list_err)

    if container_errs:
        skills_status = f"invalid repo target ({'; '.join(container_errs)})"
    else:
        preserved = _preserved_skill_entries(spec.name)
        desired_map = discover_harness_repo_skills(repo_root, spec.name)
        duplicate_names = _duplicate_harness_skill_names(repo_root, spec.name)
        reserved_collisions = sorted(name for name in desired_map if name in preserved)

        try:
            st = os.lstat(skills_dir)
        except FileNotFoundError:
            issues: list[str] = []
            if duplicate_names:
                issues.append(f"duplicate repo skills ({', '.join(sorted(duplicate_names))})")
            if reserved_collisions:
                issues.append(f"reserved repo skill names ({', '.join(reserved_collisions)})")
            missing_names = [
                n for n in sorted(desired_map) if n not in preserved and n not in duplicate_names
            ]
            if missing_names:
                issues.append(f"missing symlinks ({', '.join(missing_names)})")
            skills_status = "; ".join(issues) if issues else "not installed"
        except OSError as exc:
            skills_status = f"invalid (inaccessible: {exc})"
        else:
            if stat.S_ISLNK(st.st_mode):
                raw_link = _safe_readlink(skills_dir)
                resolved = safe_resolve(skills_dir, strict=True)
                if resolved is None:
                    skills_status = (
                        f"conflict (broken or circular symlink -> {raw_link}; "
                        "run 'init.py apply --replace' to back up)"
                    )
                else:
                    skills_status = (
                        f"conflict (symlink -> {resolved}; "
                        "run 'init.py apply --replace' to back up)"
                    )
            elif not stat.S_ISDIR(st.st_mode):
                skills_status = (
                    "conflict (non-directory file; run 'init.py apply --replace' to back up)"
                )
            else:
                visible_entries, list_err = _iter_visible_entries_checked(skills_dir)
                if list_err is not None:
                    skills_status = f"invalid ({list_err})"
                else:
                    existing_stats: dict[str, tuple[Path, os.stat_result]] = {}
                    stat_err: str | None = None
                    for entry in visible_entries:
                        if entry.name in preserved:
                            continue
                        try:
                            existing_stats[entry.name] = (entry, os.lstat(entry))
                        except OSError as exc:
                            stat_err = f"inaccessible: {entry}: {exc}"
                            break

                    if stat_err is not None:
                        skills_status = f"invalid ({stat_err})"
                    else:
                        ok_count = 0
                        missing: list[str] = []
                        conflicts: list[str] = []
                        stale: list[str] = []

                        for name, target_dir in sorted(desired_map.items()):
                            if name in preserved or name in duplicate_names:
                                continue
                            target_res = safe_resolve(target_dir, strict=True)
                            existing = existing_stats.get(name)
                            if existing is None:
                                missing.append(name)
                                continue
                            entry_path, entry_st = existing
                            if stat.S_ISLNK(entry_st.st_mode):
                                existing_res = safe_resolve(entry_path, strict=True)
                                if target_res is not None and existing_res == target_res:
                                    ok_count += 1
                                else:
                                    conflicts.append(name)
                            else:
                                conflicts.append(name)

                        for name, (entry_path, entry_st) in sorted(existing_stats.items()):
                            if name in desired_map:
                                continue
                            if stat.S_ISLNK(entry_st.st_mode) and _is_repo_owned_symlink(
                                entry_path, repo_root, repo_root_res
                            ):
                                stale.append(name)

                        issues = []
                        if duplicate_names:
                            issues.append(
                                f"duplicate repo skills ({', '.join(sorted(duplicate_names))})"
                            )
                        if reserved_collisions:
                            issues.append(
                                f"reserved repo skill names ({', '.join(reserved_collisions)})"
                            )
                        if missing:
                            issues.append(f"missing ({', '.join(missing)})")
                        if conflicts:
                            issues.append(f"conflict ({', '.join(conflicts)})")
                        if stale:
                            issues.append(f"stale repo symlinks ({', '.join(stale)})")
                        skills_status = (
                            "; ".join(issues)
                            if issues
                            else f"configured ({ok_count} skill symlink(s))"
                        )

    print(f"{skills_label} ({skills_dir}): {skills_status}")
    md_status = _global_agents_status(agents_md_path, expected_global_agents)
    print(f"{agents_label} ({agents_md_path}): {md_status}")


def cmd_status(repo_root: Path, home: Path) -> int:
    """Print a read-only report of repo skills, ~/.agents/skills, and harness bindings."""
    if not _safe_is_dir(repo_root):
        print(f"ERROR: repository path is not a directory: {repo_root}", file=sys.stderr)
        return 1
    repo_root_res = safe_resolve(repo_root, strict=True)
    if repo_root_res is None:
        print(f"ERROR: failed to resolve repository path: {repo_root}", file=sys.stderr)
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
                unmigrated, differing, scan_warnings = _scan_agents_skills_dir(
                    agents_skills, shared_dir
                )
                details: list[str] = []
                if unmigrated:
                    details.append(f"unmigrated: {', '.join(unmigrated)}")
                if differing:
                    details.append(f"differs from repo shared/: {', '.join(differing)}")
                if scan_warnings:
                    details.append(f"scan warnings: {'; '.join(scan_warnings)}")
                detail_note = f"; {'; '.join(details)}" if details else ""
                agents_status = (
                    f"conflict (real directory{detail_note}; "
                    "run 'init.py apply --replace' to back up)"
                )
            else:
                agents_status = (
                    "conflict (non-directory file; run 'init.py apply --replace' to back up)"
                )
    print(f"\n~/.agents/skills ({agents_skills}): {agents_status}")

    expected_global_agents = safe_resolve(repo_root / "global" / "AGENTS.md")
    for spec in HARNESS_REGISTRY.values():
        if spec.install_mode == "agy_json":
            _status_agy_harness(repo_root, home, expected_global_agents)
        else:
            _status_symlink_farm_harness(
                spec, repo_root, repo_root_res, home, expected_global_agents
            )

    return 0


def _backup_and_symlink(
    link_path: Path,
    target_res: Path,
    backup_dir: Path,
    backup_stem: str,
    timestamp: str,
    *,
    is_real_dir: bool,
) -> bool:
    """Atomically back up link_path into backup_dir and replace it with a symlink to target_res."""
    try:
        backup_path = (
            _create_unique_backup_dir(backup_dir, backup_stem, timestamp)
            if is_real_dir
            else _create_unique_backup_file_slot(backup_dir, backup_stem, timestamp)
        )
    except OSError as exc:
        print(f"ERROR: failed to reserve backup slot in {backup_dir}: {exc}", file=sys.stderr)
        return False

    try:
        os.rename(link_path, backup_path)
    except OSError as exc:
        if is_real_dir:
            with contextlib.suppress(OSError):
                backup_path.rmdir()
        else:
            with contextlib.suppress(OSError):
                backup_path.unlink()
        print(f"ERROR: failed to back up {link_path} -> {backup_path}: {exc}", file=sys.stderr)
        return False

    try:
        link_path.symlink_to(target_res)
    except BaseException as exc:
        if not _path_exists_or_symlink(link_path):
            try:
                os.rename(backup_path, link_path)
            except OSError as rb_exc:
                print(
                    f"ERROR: rollback failed ({backup_path} -> {link_path}): {rb_exc}",
                    file=sys.stderr,
                )
        else:
            print(
                f"ERROR: rollback skipped because {link_path} reappeared; "
                f"original backup preserved at {backup_path}",
                file=sys.stderr,
            )
        if isinstance(exc, OSError):
            print(
                f"ERROR: failed to symlink {link_path} -> {target_res}: {exc}",
                file=sys.stderr,
            )
            return False
        raise

    print(f"Backed up {link_path} -> {backup_path}")
    print(f"Linked {link_path} -> {target_res}")
    return True


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
        unmigrated, differing, scan_warnings = _scan_agents_skills_dir(agents_skills, shared_res)
        for warning in scan_warnings:
            print(
                f"WARNING: could not scan {agents_skills} for local skill differences ({warning})",
                file=sys.stderr,
            )
        preview_backup = unique_backup_path(agents_dir, "skills", timestamp)
        if unmigrated:
            print(
                f"WARNING: unmigrated skills in {agents_skills} not in repo shared/: "
                f"{', '.join(unmigrated)}. They will be preserved under {preview_backup}; "
                "import them with 'init.py adopt <backup>/<name> --scope shared'.",
                file=sys.stderr,
            )
        if differing:
            print(
                f"WARNING: local skills in {agents_skills} differ from repo shared/: "
                f"{', '.join(differing)}. They will be preserved under {preview_backup}; "
                "inspect or import updates with "
                "'init.py adopt <backup>/<name> --scope shared --force'.",
                file=sys.stderr,
            )

    if dry_run:
        preview_backup = unique_backup_path(agents_dir, "skills", timestamp)
        print(
            f"[dry-run] Would move {agents_skills} -> {preview_backup} "
            f"and symlink {agents_skills} -> {shared_res}"
        )
        return True

    return _backup_and_symlink(
        agents_skills, shared_res, agents_dir, "skills", timestamp, is_real_dir=is_real_dir
    )


def _apply_agy_skills_json(
    repo_root: Path,
    repo_root_res: Path,
    home: Path,
    *,
    dry_run: bool,
    virtual_agents_target: Path | None = None,
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

    changed = False
    if "entries" not in data:
        data["entries"] = []
        changed = True

    if _normalize_and_dedupe_skills_json_entries(
        data,
        home,
        canonical_targets=_desired_canonical_targets(repo_root),
        virtual_agents_target=virtual_agents_target,
    ):
        changed = True

    missing, target_errors = _missing_skills_json_entries(
        data, repo_root, home, virtual_agents_target=virtual_agents_target
    )
    if target_errors:
        for err in target_errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return (False, None)

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


def _resolve_repo_global_agents(repo_root: Path, repo_root_res: Path) -> Path | None:
    """Validate and resolve <repo>/global/AGENTS.md as a regular non-symlink file."""
    global_container_err = _validate_in_repo_container(repo_root_res, ("global",))
    if global_container_err is not None:
        print(f"ERROR: {global_container_err}", file=sys.stderr)
        return None

    global_agents = repo_root / "global" / "AGENTS.md"
    try:
        global_st = os.lstat(global_agents)
    except FileNotFoundError:
        print(f"ERROR: repository global/AGENTS.md not found: {global_agents}", file=sys.stderr)
        return None
    except OSError as exc:
        print(f"ERROR: cannot inspect {global_agents}: {exc}", file=sys.stderr)
        return None
    if stat.S_ISLNK(global_st.st_mode):
        print(
            f"ERROR: repository global/AGENTS.md must not be a symlink: {global_agents}",
            file=sys.stderr,
        )
        return None
    if not stat.S_ISREG(global_st.st_mode):
        print(
            f"ERROR: repository global/AGENTS.md must be a regular file: {global_agents}",
            file=sys.stderr,
        )
        return None

    global_agents_res = safe_resolve(global_agents, strict=True)
    if global_agents_res is None:
        print(f"ERROR: failed to resolve {global_agents}", file=sys.stderr)
        return None
    return global_agents_res


def _apply_harness_global_agents(
    spec: HarnessSpec,
    global_agents_res: Path | None,
    repo_root_res: Path,
    home: Path,
    timestamp: str,
    *,
    dry_run: bool,
    replace: bool,
    no_global_agents: bool,
) -> bool:
    """Link a harness global instruction file -> <repo>/global/AGENTS.md unless opted out."""
    config_dir = _harness_config_dir(home, spec)
    agents_md_path = _harness_global_agents_path(home, spec)
    if no_global_agents:
        print(f"Skipped {agents_md_path} (--no-global-agents)")
        return True
    if global_agents_res is None:
        return False

    ancestor_err = _validate_dir_ancestors(config_dir, stop_at=home)
    if ancestor_err is not None:
        print(f"ERROR: cannot link {agents_md_path} ({ancestor_err}).", file=sys.stderr)
        return False

    config_dir_res = safe_resolve(config_dir, strict=False)
    if config_dir_res is not None and (
        config_dir_res == repo_root_res or config_dir_res.is_relative_to(repo_root_res)
    ):
        print(
            f"ERROR: refusing to modify {agents_md_path} because {config_dir} resolves inside "
            f"repository {repo_root_res}.",
            file=sys.stderr,
        )
        return False

    try:
        st = os.lstat(agents_md_path)
    except FileNotFoundError:
        is_link = False
        exists = False
        is_real_dir = False
    except OSError as exc:
        print(f"ERROR: cannot inspect {agents_md_path}: {exc}", file=sys.stderr)
        return False
    else:
        is_link = stat.S_ISLNK(st.st_mode)
        exists = _safe_exists(agents_md_path)
        is_real_dir = (not is_link) and stat.S_ISDIR(st.st_mode)

    if not is_link and not exists:
        if dry_run:
            print(f"[dry-run] Would symlink {agents_md_path} -> {global_agents_res}")
            return True
        try:
            config_dir.mkdir(parents=True, exist_ok=True)
            agents_md_path.symlink_to(global_agents_res)
        except OSError as exc:
            print(
                f"ERROR: failed to symlink {agents_md_path} -> {global_agents_res}: {exc}",
                file=sys.stderr,
            )
            return False
        print(f"Linked {agents_md_path} -> {global_agents_res}")
        return True

    if is_link and exists and safe_resolve(agents_md_path, strict=True) == global_agents_res:
        print(f"OK: {agents_md_path} already links to {global_agents_res}")
        return True

    if not replace:
        kind = "symlink" if is_link else ("directory" if is_real_dir else "file")
        print(
            f"CONFLICT: {agents_md_path} already exists as a {kind}; leaving untouched "
            "(re-run with --replace to back up and link, or pass --no-global-agents to skip).",
            file=sys.stderr,
        )
        return False

    if not is_link and exists:
        existing_res = safe_resolve(agents_md_path, strict=True)
        if (
            existing_res is None
            or _paths_overlap(existing_res, repo_root_res)
            or _paths_overlap(existing_res, global_agents_res)
        ):
            print(
                f"ERROR: refusing to back up {agents_md_path} because it overlaps "
                f"repository {repo_root_res}.",
                file=sys.stderr,
            )
            return False

    if dry_run:
        preview_backup = unique_backup_path(config_dir, agents_md_path.name, timestamp)
        print(
            f"[dry-run] Would move {agents_md_path} -> {preview_backup} "
            f"and symlink {agents_md_path} -> {global_agents_res}"
        )
        return True

    return _backup_and_symlink(
        agents_md_path,
        global_agents_res,
        config_dir,
        agents_md_path.name,
        timestamp,
        is_real_dir=is_real_dir,
    )


def _validate_replacement_repo_skill(
    skill_name: str,
    repo_skill_dir: Path,
    *,
    repo_root: Path,
    repo_root_res: Path,
    home: Path,
    duplicate_names: set[str],
    skills_json_path: Path,
    skills_json_data: dict[str, object],
) -> str | None:
    """Validate that a repo skill safely replaces a shadowed skill; return error or None."""
    if skill_name in duplicate_names:
        return f"'{skill_name}' is duplicated in both shared/ and harnesses/agy/"
    if not _is_skill_exposed_by_skills_json(skills_json_data, repo_skill_dir, home):
        return f"{skills_json_path} filters out '{skill_name}' via include_only/exclude"
    validation_errors = validate_skill_dir(repo_skill_dir) + _validate_skill_tree_symlinks(
        repo_skill_dir, repo_root, repo_root_res
    )
    if validation_errors:
        return f"repo skill {repo_skill_dir} failed validation: {'; '.join(validation_errors)}"
    return None


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

    should_scan, preflight_ok = _preflight_shadow_container_apply(
        agy_skills_dir, "loose-skill", replace=replace
    )
    if not should_scan:
        return preflight_ok

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

            rep_err = _validate_replacement_repo_skill(
                entry.name,
                repo_skill_dir,
                repo_root=repo_root,
                repo_root_res=repo_root_res,
                home=home,
                duplicate_names=duplicate_names,
                skills_json_path=skills_json_path,
                skills_json_data=skills_json_data,
            )
            if rep_err is not None:
                print(
                    f"ERROR: refusing to move shadowing skill {entry} because {rep_err}.",
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


def _apply_agy_plugin_shadow_backups(
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
    """Detect or safely back up non-marketplace local plugins shadowing repo skills."""
    config_dir, skills_json_path, _ = _agy_config_paths(home)
    plugins_dir = _agy_plugins_dir(home)
    if _validate_dir_ancestors(config_dir, stop_at=home) is not None:
        return True

    should_scan, preflight_ok = _preflight_shadow_container_apply(
        plugins_dir, "local-plugin", replace=replace
    )
    if not should_scan:
        return preflight_ok

    repo_skill_map = discover_agy_repo_skills(repo_root)
    shadowing_plugins, scan_err = _find_shadowing_agy_plugins(home, repo_skill_map)
    if scan_err is not None:
        print(f"ERROR: {scan_err}", file=sys.stderr)
        return False
    if not shadowing_plugins:
        return True

    if not replace:
        details = "; ".join(
            f"{sp.plugin_path.name} ({', '.join(sp.colliding_skills)})"
            for sp in shadowing_plugins
        )
        print(
            f"WARNING: local plugins in {plugins_dir} shadow repo skills: "
            f"{details}. Re-run with --replace to back them up.",
            file=sys.stderr,
        )
        return True

    plugins_res = safe_resolve(plugins_dir, strict=True)
    if plugins_res is None or _paths_overlap(plugins_res, repo_root_res):
        print(
            f"ERROR: refusing to move plugins from {plugins_dir} because it overlaps "
            f"repository {repo_root_res}.",
            file=sys.stderr,
        )
        return False

    if not skills_json_ready or skills_json_data is None:
        names = ", ".join(sp.plugin_path.name for sp in shadowing_plugins)
        print(
            f"WARNING: refusing to move shadowing plugins ({names}) because "
            f"{skills_json_path} was not updated.",
            file=sys.stderr,
        )
        return False

    duplicate_names = _duplicate_agy_skill_names(repo_root)
    ok = True
    backup_dir: Path | None = None
    moved_count = 0

    try:
        for sp in shadowing_plugins:
            if sp.extra_unmigrated_skills or sp.extra_runtime_components:
                reasons: list[str] = []
                if sp.extra_unmigrated_skills:
                    reasons.append(
                        f"unmigrated skills not in repo: {', '.join(sp.extra_unmigrated_skills)}"
                    )
                if sp.extra_runtime_components:
                    reasons.append(
                        "non-skill runtime components: "
                        f"{', '.join(sp.extra_runtime_components)}"
                    )
                print(
                    f"ERROR: refusing to move shadowing plugin {sp.plugin_path} because it "
                    f"contains {'; '.join(reasons)}.",
                    file=sys.stderr,
                )
                ok = False
                continue

            if not _safe_is_symlink(sp.plugin_path):
                plugin_res = safe_resolve(sp.plugin_path, strict=True)
                if plugin_res is None or _paths_overlap(plugin_res, repo_root_res):
                    print(
                        f"ERROR: refusing to move shadowing plugin {sp.plugin_path} because it "
                        f"overlaps repository {repo_root_res}.",
                        file=sys.stderr,
                    )
                    ok = False
                    continue

            plugin_valid = True
            for skill_name in sp.colliding_skills:
                rep_err = _validate_replacement_repo_skill(
                    skill_name,
                    repo_skill_map[skill_name],
                    repo_root=repo_root,
                    repo_root_res=repo_root_res,
                    home=home,
                    duplicate_names=duplicate_names,
                    skills_json_path=skills_json_path,
                    skills_json_data=skills_json_data,
                )
                if rep_err is not None:
                    print(
                        f"ERROR: refusing to move shadowing plugin {sp.plugin_path} because "
                        f"{rep_err}.",
                        file=sys.stderr,
                    )
                    plugin_valid = False
                    break
            if not plugin_valid:
                ok = False
                continue

            if backup_dir is None:
                if dry_run:
                    backup_dir = unique_backup_path(config_dir, "plugins", timestamp)
                else:
                    try:
                        backup_dir = _create_unique_backup_dir(config_dir, "plugins", timestamp)
                    except OSError as exc:
                        print(
                            f"ERROR: failed to create backup directory in {config_dir}: {exc}",
                            file=sys.stderr,
                        )
                        return False

            dest = backup_dir / sp.plugin_path.name
            if dry_run:
                print(f"[dry-run] Would move shadowing plugin {sp.plugin_path} -> {dest}")
                continue

            try:
                _move_entry_to_backup(sp.plugin_path, dest, backup_dir)
            except OSError as exc:
                print(
                    f"ERROR: failed to back up shadowing plugin {sp.plugin_path} -> {dest}: {exc}",
                    file=sys.stderr,
                )
                ok = False
                continue

            moved_count += 1
            print(f"Backed up shadowing plugin {sp.plugin_path} -> {dest}")
    finally:
        if backup_dir is not None and not dry_run and moved_count == 0:
            with contextlib.suppress(OSError):
                backup_dir.rmdir()

    return ok


def _backup_and_create_dir(
    target_dir: Path,
    backup_parent: Path,
    backup_stem: str,
    timestamp: str,
) -> bool:
    """Atomically back up a non-directory entry at target_dir and create a real directory."""
    try:
        backup_path = _create_unique_backup_file_slot(backup_parent, backup_stem, timestamp)
    except OSError as exc:
        print(
            f"ERROR: failed to reserve backup slot in {backup_parent}: {exc}",
            file=sys.stderr,
        )
        return False

    try:
        os.rename(target_dir, backup_path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            backup_path.unlink()
        print(
            f"ERROR: failed to back up {target_dir} -> {backup_path}: {exc}",
            file=sys.stderr,
        )
        return False

    try:
        target_dir.mkdir(parents=False, exist_ok=False)
    except BaseException as exc:
        if not _path_exists_or_symlink(target_dir):
            try:
                os.rename(backup_path, target_dir)
            except OSError as rb_exc:
                print(
                    f"ERROR: rollback failed ({backup_path} -> {target_dir}): {rb_exc}",
                    file=sys.stderr,
                )
        else:
            print(
                f"ERROR: rollback skipped because {target_dir} reappeared; "
                f"original backup preserved at {backup_path}",
                file=sys.stderr,
            )
        if isinstance(exc, OSError):
            print(
                f"ERROR: failed to create directory {target_dir}: {exc}",
                file=sys.stderr,
            )
            return False
        raise

    print(f"Backed up {target_dir} -> {backup_path}")
    print(f"Created {target_dir}")
    return True


def _apply_symlink_farm_harness(
    spec: HarnessSpec,
    repo_root: Path,
    repo_root_res: Path,
    home: Path,
    timestamp: str,
    *,
    dry_run: bool,
    replace: bool,
    virtual_agents_target: Path | None = None,
) -> bool:
    """Maintain per-skill symlinks inside ~/.<harness>/skills/ without disturbing built-ins."""
    harness_container_err = _validate_in_repo_container(
        repo_root_res, Path(spec.repo_rel_dir).parts
    )
    if harness_container_err is not None:
        print(f"ERROR: {harness_container_err}", file=sys.stderr)
        return False

    for container in (repo_root / "shared", repo_root / spec.repo_rel_dir):
        if _safe_exists(container):
            _, container_list_err = _iter_visible_entries_checked(container)
            if container_list_err is not None:
                print(f"ERROR: {container_list_err}", file=sys.stderr)
                return False

    config_dir = _harness_config_dir(home, spec)
    skills_dir = _harness_skills_dir(home, spec)
    ancestor_err = _validate_dir_ancestors(config_dir, stop_at=home)
    if ancestor_err is not None:
        print(f"ERROR: cannot configure {skills_dir} ({ancestor_err}).", file=sys.stderr)
        return False

    config_dir_res = safe_resolve(config_dir, strict=False)
    if config_dir_res is not None and (
        config_dir_res == repo_root_res or config_dir_res.is_relative_to(repo_root_res)
    ):
        print(
            f"ERROR: refusing to modify {skills_dir} because {config_dir} resolves inside "
            f"repository {repo_root_res}.",
            file=sys.stderr,
        )
        return False

    desired_map = discover_harness_repo_skills(repo_root, spec.name)
    duplicate_names = _duplicate_harness_skill_names(repo_root, spec.name)
    preserved = _preserved_skill_entries(spec.name)

    valid_targets: dict[str, Path] = {}
    repo_skill_errors: list[str] = []
    for dup in sorted(duplicate_names):
        repo_skill_errors.append(
            f"refusing to link '{dup}' for harness '{spec.name}' because it is "
            f"duplicated in both shared/ and {spec.repo_rel_dir}/."
        )
    for name, target_dir in sorted(desired_map.items()):
        if name in duplicate_names:
            continue
        if name in preserved:
            repo_skill_errors.append(
                f"refusing to link skill '{name}' for harness '{spec.name}' "
                f"because '{name}' is a reserved harness entry."
            )
            continue
        validation_errors = validate_skill_dir(target_dir) + _validate_skill_tree_symlinks(
            target_dir, repo_root, repo_root_res
        )
        if validation_errors:
            repo_skill_errors.append(
                f"refusing to link {target_dir} for harness '{spec.name}' "
                f"(failed validation: {'; '.join(validation_errors)})."
            )
            continue
        target_res = safe_resolve(target_dir, strict=True)
        if target_res is None:
            repo_skill_errors.append(f"failed to resolve repo skill {target_dir}.")
            continue
        valid_targets[name] = target_res

    skills_dir_exists = False
    replacing_container = False
    replaced_container = False
    try:
        st = os.lstat(skills_dir)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"ERROR: cannot inspect {skills_dir}: {exc}", file=sys.stderr)
        return False
    else:
        is_link = stat.S_ISLNK(st.st_mode)
        if not is_link and stat.S_ISDIR(st.st_mode):
            skills_dir_res = safe_resolve(skills_dir, strict=True)
            if skills_dir_res is None or _paths_overlap(skills_dir_res, repo_root_res):
                print(
                    f"ERROR: refusing to modify {skills_dir} because it overlaps "
                    f"repository {repo_root_res}.",
                    file=sys.stderr,
                )
                return False
            skills_dir_exists = True
        else:
            kind = "symlink" if is_link else "file"
            if not replace:
                print(
                    f"CONFLICT: {skills_dir} already exists as a {kind}; leaving untouched "
                    "(re-run with --replace to back up and create skills directory).",
                    file=sys.stderr,
                )
                return False
            if not is_link and _safe_exists(skills_dir):
                existing_res = safe_resolve(skills_dir, strict=True)
                if existing_res is None or _paths_overlap(existing_res, repo_root_res):
                    print(
                        f"ERROR: refusing to back up {skills_dir} because it overlaps "
                        f"repository {repo_root_res}.",
                        file=sys.stderr,
                    )
                    return False
            if repo_skill_errors:
                for err in repo_skill_errors:
                    print(f"ERROR: {err}", file=sys.stderr)
                print(
                    f"ERROR: refusing to replace {skills_dir} because one or more repository "
                    f"skills for harness '{spec.name}' failed validation.",
                    file=sys.stderr,
                )
                return False
            if dry_run:
                preview_backup = unique_backup_path(config_dir, spec.skills_dir_name, timestamp)
                print(
                    f"[dry-run] Would move {skills_dir} -> {preview_backup} "
                    f"and create {skills_dir}"
                )
                replacing_container = True
                replaced_container = True
            else:
                if not _backup_and_create_dir(
                    skills_dir, config_dir, spec.skills_dir_name, timestamp
                ):
                    return False
                skills_dir_exists = True
                replaced_container = True

    ok = not repo_skill_errors
    for err in repo_skill_errors:
        print(f"ERROR: {err}", file=sys.stderr)

    pruned_count = 0
    if skills_dir_exists:
        existing_entries, list_err = _iter_visible_entries_checked(skills_dir)
        if list_err is not None:
            print(f"ERROR: {list_err}", file=sys.stderr)
            return False
        for entry in existing_entries:
            if entry.name in preserved or entry.name in desired_map:
                continue
            try:
                entry_st = os.lstat(entry)
            except OSError as exc:
                print(f"ERROR: cannot inspect {entry}: {exc}", file=sys.stderr)
                ok = False
                continue
            if not stat.S_ISLNK(entry_st.st_mode):
                continue
            if _is_repo_owned_symlink(
                entry,
                repo_root,
                repo_root_res,
                home=home,
                virtual_agents_target=virtual_agents_target,
            ):
                if dry_run:
                    print(f"[dry-run] Would remove stale symlink {entry}")
                    pruned_count += 1
                else:
                    try:
                        entry.unlink()
                    except OSError as exc:
                        print(
                            f"ERROR: failed to remove stale symlink {entry}: {exc}",
                            file=sys.stderr,
                        )
                        ok = False
                    else:
                        print(f"Removed stale symlink {entry}")
                        pruned_count += 1

    already_ok = 0
    linked_count = 0
    backup_dir: Path | None = None
    moved_count = 0

    try:
        for name, target_res in valid_targets.items():
            link_path = skills_dir / name
            if replacing_container:
                is_link = False
                exists = False
                is_real_dir = False
            else:
                try:
                    link_st = os.lstat(link_path)
                except FileNotFoundError:
                    is_link = False
                    exists = False
                    is_real_dir = False
                except OSError as exc:
                    print(f"ERROR: cannot inspect {link_path}: {exc}", file=sys.stderr)
                    ok = False
                    continue
                else:
                    is_link = stat.S_ISLNK(link_st.st_mode)
                    exists = _safe_exists(link_path)
                    is_real_dir = (not is_link) and stat.S_ISDIR(link_st.st_mode)

            if not is_link and not exists:
                if dry_run:
                    print(f"[dry-run] Would symlink {link_path} -> {target_res}")
                    linked_count += 1
                    continue
                try:
                    skills_dir.mkdir(parents=True, exist_ok=True)
                    link_path.symlink_to(target_res)
                except OSError as exc:
                    print(
                        f"ERROR: failed to symlink {link_path} -> {target_res}: {exc}",
                        file=sys.stderr,
                    )
                    ok = False
                    continue
                print(f"Linked {link_path} -> {target_res}")
                linked_count += 1
                continue

            if is_link:
                live_target = _resolve_symlink_live_target(
                    link_path,
                    home=home,
                    virtual_agents_target=virtual_agents_target,
                )
                if live_target == target_res:
                    already_ok += 1
                    continue

            if not replace:
                kind = "symlink" if is_link else ("directory" if is_real_dir else "file")
                print(
                    f"CONFLICT: {link_path} already exists as a {kind}; leaving untouched "
                    "(re-run with --replace to back up and link).",
                    file=sys.stderr,
                )
                ok = False
                continue

            if not is_link and exists:
                existing_res = safe_resolve(link_path, strict=True)
                if (
                    existing_res is None
                    or _paths_overlap(existing_res, repo_root_res)
                    or _paths_overlap(existing_res, target_res)
                ):
                    print(
                        f"ERROR: refusing to back up {link_path} because it overlaps "
                        f"repository {repo_root_res}.",
                        file=sys.stderr,
                    )
                    ok = False
                    continue

            if backup_dir is None:
                if dry_run:
                    backup_dir = unique_backup_path(config_dir, spec.skills_dir_name, timestamp)
                else:
                    try:
                        backup_dir = _create_unique_backup_dir(
                            config_dir, spec.skills_dir_name, timestamp
                        )
                    except OSError as exc:
                        print(
                            f"ERROR: failed to create backup directory in {config_dir}: {exc}",
                            file=sys.stderr,
                        )
                        return False

            dest = backup_dir / name
            if dry_run:
                print(
                    f"[dry-run] Would move {link_path} -> {dest} "
                    f"and symlink {link_path} -> {target_res}"
                )
                linked_count += 1
                continue

            try:
                _move_entry_to_backup(link_path, dest, backup_dir)
            except OSError as exc:
                print(
                    f"ERROR: failed to back up {link_path} -> {dest}: {exc}",
                    file=sys.stderr,
                )
                ok = False
                continue

            try:
                link_path.symlink_to(target_res)
            except BaseException as exc:
                if not _path_exists_or_symlink(link_path):
                    try:
                        _move_entry_to_backup(dest, link_path, skills_dir)
                    except OSError as rb_exc:
                        moved_count += 1
                        print(
                            f"ERROR: rollback failed ({dest} -> {link_path}): {rb_exc}",
                            file=sys.stderr,
                        )
                else:
                    moved_count += 1
                    print(
                        f"ERROR: rollback skipped because {link_path} reappeared; "
                        f"original backup preserved at {dest}",
                        file=sys.stderr,
                    )
                if isinstance(exc, OSError):
                    print(
                        f"ERROR: failed to symlink {link_path} -> {target_res}: {exc}",
                        file=sys.stderr,
                    )
                    ok = False
                    continue
                raise

            moved_count += 1
            linked_count += 1
            print(f"Backed up {link_path} -> {dest}")
            print(f"Linked {link_path} -> {target_res}")
    finally:
        if backup_dir is not None and not dry_run and moved_count == 0:
            with contextlib.suppress(OSError):
                backup_dir.rmdir()

    if ok and linked_count == 0 and pruned_count == 0 and not replaced_container:
        print(f"OK: {skills_dir} already links all {already_ok} skill(s)")
    elif already_ok > 0:
        print(f"OK: {skills_dir} kept {already_ok} existing skill symlink(s)")

    return ok


def cmd_apply(
    repo_root: Path,
    home: Path,
    *,
    dry_run: bool,
    replace: bool,
    no_global_agents: bool = False,
) -> int:
    """Link ~/.agents/skills -> shared/, configure harnesses, and link global rules."""
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
    virtual_agents_target = shared_res if (dry_run and agents_ok) else None

    if no_global_agents:
        global_agents_res: Path | None = None
        global_agents_resolved_ok = True
    else:
        global_agents_res = _resolve_repo_global_agents(repo_root, repo_root_res)
        global_agents_resolved_ok = global_agents_res is not None

    all_ok = agents_ok and global_agents_resolved_ok
    for spec in HARNESS_REGISTRY.values():
        if spec.install_mode == "agy_json":
            skills_json_ready, skills_json_data = _apply_agy_skills_json(
                repo_root,
                repo_root_res,
                home,
                dry_run=dry_run,
                virtual_agents_target=virtual_agents_target,
            )
            harness_global_ok = _apply_harness_global_agents(
                spec,
                global_agents_res,
                repo_root_res,
                home,
                timestamp,
                dry_run=dry_run,
                replace=replace,
                no_global_agents=no_global_agents,
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
            plugin_shadows_ok = _apply_agy_plugin_shadow_backups(
                repo_root,
                repo_root_res,
                home,
                timestamp,
                skills_json_ready=skills_json_ready,
                skills_json_data=skills_json_data,
                dry_run=dry_run,
                replace=replace,
            )
            all_ok = (
                all_ok
                and skills_json_ready
                and harness_global_ok
                and shadows_ok
                and plugin_shadows_ok
            )
        else:
            farm_ok = _apply_symlink_farm_harness(
                spec,
                repo_root,
                repo_root_res,
                home,
                timestamp,
                dry_run=dry_run,
                replace=replace,
                virtual_agents_target=virtual_agents_target,
            )
            harness_global_ok = _apply_harness_global_agents(
                spec,
                global_agents_res,
                repo_root_res,
                home,
                timestamp,
                dry_run=dry_run,
                replace=replace,
                no_global_agents=no_global_agents,
            )
            all_ok = all_ok and farm_ok and harness_global_ok

    return 0 if all_ok else 1


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

    if name in _reserved_skill_names_for_scope(scope):
        print(
            f"ERROR: skill name '{name}' is reserved for scope '{scope}'",
            file=sys.stderr,
        )
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


def _check_global_agents(repo_root: Path, repo_root_res: Path) -> list[str]:
    """Validate global/ container and global/AGENTS.md regular UTF-8 file <= 24,000 bytes."""
    container_err = _validate_in_repo_container(repo_root_res, ("global",))
    if container_err is not None:
        return [f"global: {container_err}"]

    global_agents = repo_root / "global" / "AGENTS.md"
    try:
        st = os.lstat(global_agents)
    except FileNotFoundError:
        return ["global/AGENTS.md: missing global rules file"]
    except OSError as exc:
        return [f"global/AGENTS.md: inaccessible: {exc}"]

    if stat.S_ISLNK(st.st_mode):
        return ["global/AGENTS.md: must be a regular file, not a symlink"]
    if not stat.S_ISREG(st.st_mode):
        return ["global/AGENTS.md: must be a regular file"]

    try:
        raw_bytes = global_agents.read_bytes()
    except OSError as exc:
        return [f"global/AGENTS.md: failed to read file: {exc}"]

    errors: list[str] = []
    size = len(raw_bytes)
    if size > GLOBAL_AGENTS_MAX_BYTES:
        errors.append(
            f"global/AGENTS.md: file size ({size} bytes) exceeds "
            f"{GLOBAL_AGENTS_MAX_BYTES}-byte limit"
        )
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        errors.append(f"global/AGENTS.md: failed to read UTF-8 text: {exc}")
        return errors

    if not text.strip():
        errors.append("global/AGENTS.md: file must not be empty")
    return errors


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

    # Global AGENTS.md validation
    errors.extend(_check_global_agents(repo_root, repo_root_res))

    # Skill frontmatter & structure across shared/* and harnesses/*/*
    shared_names, shared_errs = _check_skill_container(repo_root / "shared", repo_root)
    total_skills = len(shared_names)
    errors.extend(shared_errs)
    for reserved in sorted(shared_names & _reserved_skill_names_for_scope("shared")):
        errors.append(
            f"shared/{reserved}: skill name {reserved!r} is reserved by a harness skill directory"
        )

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
            for reserved in sorted(
                harness_names & _reserved_skill_names_for_scope(harness_dir.name)
            ):
                errors.append(
                    f"{rel}/{reserved}: skill name {reserved!r} is reserved by "
                    f"harness '{harness_dir.name}'"
                )

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

    # Hardcoded machine user path scan across .md, .py, .json, .toml files
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
        help=(
            "Link ~/.agents/skills -> shared/, configure agy/claude/codex skills, "
            "and link global rules."
        ),
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
        help=(
            "Back up conflicting ~/.agents/skills, global rule files, "
            "and colliding skills/plugins entries."
        ),
    )
    apply_parser.add_argument(
        "--no-global-agents",
        "--skip-global-agents",
        dest="no_global_agents",
        action="store_true",
        help=(
            "Do not install or overwrite ~/.gemini/config/AGENTS.md, "
            "~/.claude/CLAUDE.md, or ~/.codex/AGENTS.md."
        ),
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
        return cmd_apply(
            repo_root,
            home,
            dry_run=args.dry_run,
            replace=args.replace,
            no_global_agents=args.no_global_agents,
        )

    if args.command == "adopt":
        return cmd_adopt(repo_root, args.source_dir, scope=args.scope, force=args.force)

    if args.command == "doctor":
        return cmd_doctor(repo_root)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
