"""Shared pytest fixtures for agent-skills tests."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

_ISOLATED_GIT_ENV_KEYS: tuple[str, ...] = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_TEMPLATE_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_SYSTEM",
    "GIT_EXTERNAL_DIFF",
    "GIT_DIFF_OPTS",
    "GIT_LITERAL_PATHSPECS",
    "GIT_GLOB_PATHSPECS",
    "GIT_NOGLOB_PATHSPECS",
    "GIT_ICASE_PATHSPECS",
    "GIT_NO_LAZY_FETCH",
    "GIT_TERMINAL_PROMPT",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
)


@pytest.fixture(autouse=True)
def isolate_home(tmp_path: Path) -> Iterator[Path]:
    """Prevent any test from touching real home, temp dirs, or host git/XDG configuration."""
    guarded_home = tmp_path / "guarded_home"
    guarded_home.mkdir()
    guarded_tmp = tmp_path / "tmp"
    guarded_tmp.mkdir()
    with pytest.MonkeyPatch.context() as mp:
        for key in _ISOLATED_GIT_ENV_KEYS:
            mp.delenv(key, raising=False)
        for key in list(os.environ):
            if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
                mp.delenv(key, raising=False)
        mp.setenv("HOME", str(guarded_home))
        mp.setenv("XDG_CONFIG_HOME", str(guarded_home / ".config"))
        mp.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        mp.setenv("GIT_CONFIG_NOSYSTEM", "1")
        mp.setenv("GIT_ATTR_NOSYSTEM", "1")
        mp.setenv("TMPDIR", str(guarded_tmp))
        mp.setenv("TEMP", str(guarded_tmp))
        mp.setenv("TMP", str(guarded_tmp))
        mp.setattr(tempfile, "tempdir", None)
        mp.setattr(Path, "home", classmethod(lambda cls: guarded_home))
        yield guarded_home
