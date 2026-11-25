"""Tests for claude_review.py behavior and Claude/Codex harness adapter contracts."""

from __future__ import annotations

import contextlib
import ctypes
import fcntl
import importlib.util
import io
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import init

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "harnesses" / "codex" / "claude-review" / "scripts" / "claude_review.py"

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

sys.stdin.reconfigure(encoding="utf-8")
mode = os.environ.get("FAKE_CASE", "ok")
assert "ANTHROPIC_API_KEY" not in os.environ
assert "ANTHROPIC_BASE_URL" not in os.environ
assert "CLAUDE_CODE_USE_BEDROCK" not in os.environ
if "auth" in sys.argv:
    if mode == "auth_plain_error":
        print("Not logged in")
        sys.exit(1)
    if mode == "auth_bad_json_ok":
        print("not JSON")
        sys.exit(0)
    print(
        json.dumps(
            {
                "loggedIn": True,
                "authMethod": "api_key" if mode == "api" else "claude.ai",
                "apiProvider": "firstParty",
                "subscriptionType": "max",
                "email": "private@example.test",
            }
        )
    )
    sys.exit(0)
Path(os.environ["FAKE_STARTED"]).write_text(str(os.getpid()))
if mode == "slow":
    time.sleep(0.2)
prompt = sys.stdin.read()
Path(os.environ["FAKE_PROMPT"]).write_text(prompt)


def option(flag, default=None):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


sid = option("--session-id", option("--resume", "direct-test"))
native_dir = Path(os.environ["FAKE_STARTED"]).parent / "native-sessions"
native_dir.mkdir(exist_ok=True)
native = native_dir / (sid + ".json")
history = json.loads(native.read_text()) if "--resume" in sys.argv else []
history.append(prompt)
native.write_text(json.dumps(history))
assert "--no-session-persistence" not in sys.argv
if mode == "timeout":
    time.sleep(60)
if mode == "bad_json":
    print("not JSON")
    sys.exit(0)
result = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": (
        "Critical finding: Unicode λ, `literal` and $(not-a-command).\n"
        "  Keep this indentation.\n"
    ),
    "session_id": sid,
    "structured_output": {
        "summary": "Check access logic λ.",
        "findings": [
            {
                "id": "R1",
                "severity": "high",
                "status": "open",
                "location": "check.py:2",
                "finding": "Wrong comparison",
                "validation": "Test owner and non-owner",
            }
        ],
        "limitations": [],
        "full_report": (
            "FULL-REPORT-ONLY λ\n  Preserve indentation.\n" + "Long report detail. " * 500
        ),
    },
    "modelUsage": {"claude-fable-example": {"inputTokens": 100}},
    "usage": {"input_tokens": 100},
    "permission_denials": [],
    "total_cost_usd": 0.01,
}
if mode == "error":
    result.update(subtype="error_during_execution", is_error=True, result="Quota exhausted")
if mode == "denied":
    result["permission_denials"] = [
        {"tool_name": "Read", "tool_input": {"file_path": "/outside"}}
    ]
if mode == "many_findings":
    base_finding = result["structured_output"]["findings"][0]
    result["structured_output"]["findings"] = [
        dict(base_finding, id=f"R{i}") for i in range(1, 10)
    ]
if mode == "bad_schema":
    result["structured_output"]["summary"] = 123
if mode == "wrong_session":
    result["session_id"] = "wrong-session"
if len(history) > 1:
    result["structured_output"]["summary"] = "Resumed with prior evidence."
    result["structured_output"]["findings"][0]["status"] = "resolved"
print(json.dumps(result))
sys.exit(9 if mode == "exit_error" else 0)
'''


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        claude = self.bin / "claude"
        claude.write_text(FAKE_CLAUDE, encoding="utf-8")
        claude.chmod(0o700)
        self.env = dict(
            os.environ,
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            FAKE_STARTED=str(self.root / "started"),
            FAKE_PROMPT=str(self.root / "received"),
            ANTHROPIC_API_KEY="must-not-be-used",
            ANTHROPIC_BASE_URL="https://wrong.test",
            CLAUDE_CODE_USE_BEDROCK="1",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def run_case(self, case="ok", **options):
        command = [
            sys.executable,
            str(SCRIPT),
            "--cwd",
            str(self.root),
            "--output-root",
            str(self.root / "results"),
        ]
        for key, value in options.items():
            command.extend(["--" + key, str(value)])
        proc = subprocess.run(
            command,
            input="Review `x` and $(touch nope); preserve λ.",
            text=True,
            capture_output=True,
            env=dict(self.env, FAKE_CASE=case),
            timeout=15,
        )
        self.assertTrue(proc.stdout, proc.stderr)
        result = json.loads(proc.stdout)
        return proc, result, Path(result["artifacts_dir"])

    def test_result_fidelity_and_explicit_options(self):
        proc, result, run = self.run_case(model="opus", effort="xhigh", mode="plan")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["actual_models"], ["claude-fable-example"])
        report = (run / "report.md").read_text(encoding="utf-8")
        raw_claude = json.loads((run / "claude.json").read_text(encoding="utf-8"))
        self.assertEqual(report, raw_claude["structured_output"]["full_report"])
        review_data = json.loads((run / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(result["findings"], review_data["findings"])
        self.assertNotIn("FULL-REPORT-ONLY", proc.stdout)
        self.assertNotIn("full_report", proc.stdout)
        self.assertNotIn("model_usage", proc.stdout)
        self.assertEqual(result["session_id"], raw_claude["session_id"])
        self.assertTrue(result["resumable"])
        self.assertIn("$(touch nope)", (self.root / "received").read_text(encoding="utf-8"))
        self.assertFalse((self.root / "nope").exists())
        self.assertNotIn(
            "private@example.test", (run / "auth-check.json").read_text(encoding="utf-8")
        )
        self.assertEqual(run.stat().st_mode & 0o777, 0o700)
        args = json.loads((run / "command.json").read_text(encoding="utf-8"))
        for key, expected in [
            ("--model", "opus"),
            ("--effort", "xhigh"),
            ("--tools", "Read,Glob,Grep"),
        ]:
            self.assertEqual(args[args.index(key) + 1], expected)
        self.assertNotIn("--dangerously-skip-permissions", args)
        self.assertNotIn("--bare", args)
        self.assertNotIn("--no-session-persistence", args)

    def test_refuses_api_auth_before_any_model_call(self):
        proc, result, _ = self.run_case("api")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(result["status"], "auth_error")
        self.assertFalse((self.root / "started").exists())

    def test_auth_status_non_json_classification(self):
        proc, result, _ = self.run_case("auth_plain_error")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(result["status"], "auth_error")
        self.assertFalse((self.root / "started").exists())

        proc_ok, result_ok, _ = self.run_case("auth_bad_json_ok")
        self.assertNotEqual(proc_ok.returncode, 0)
        self.assertEqual(result_ok["status"], "protocol_error")
        self.assertFalse((self.root / "started").exists())

    def test_stop_process_escalates_to_sigkill_when_interrupted_during_grace_wait(self):
        spec = importlib.util.spec_from_file_location("review_stop_check", SCRIPT)
        assert spec is not None and spec.loader is not None
        runner = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, "dont_write_bytecode", True):
            spec.loader.exec_module(runner)

        reaped = False

        class StubProc:
            pid = 424242

            def wait(self, timeout=None):
                nonlocal reaped
                if timeout is not None:
                    raise KeyboardInterrupt
                reaped = True
                return -9

            def poll(self):
                return -9 if reaped else None

        sent_signals: list[tuple[int, int]] = []
        with (
            mock.patch.object(
                runner.os, "killpg", side_effect=lambda pgid, sig: sent_signals.append((pgid, sig))
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            runner.stop_process(StubProc())
        self.assertEqual(
            sent_signals,
            [(424242, signal.SIGTERM), (424242, signal.SIGKILL)],
        )
        self.assertTrue(reaped)

    def test_non_success_results_are_not_reviews(self):
        cases = [
            ("error", "claude_error"),
            ("exit_error", "claude_error"),
            ("bad_json", "protocol_error"),
            ("denied", "incomplete"),
            ("bad_schema", "protocol_error"),
            ("wrong_session", "protocol_error"),
        ]
        for case, status in cases:
            with self.subTest(case=case):
                proc, result, _ = self.run_case(case)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(result["status"], status)
                if case != "denied":
                    self.assertFalse(result["resumable"])

    def test_timeout_terminates_claude(self):
        proc, result, _ = self.run_case("timeout", timeout=1)
        self.assertEqual(proc.returncode, 124)
        self.assertEqual(result["status"], "timeout")
        pid = int((self.root / "started").read_text(encoding="utf-8"))
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_cancellation_terminates_claude_and_records_status(self):
        command = [
            sys.executable,
            str(SCRIPT),
            "--cwd",
            str(self.root),
            "--output-root",
            str(self.root / "results"),
        ]
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(self.env, FAKE_CASE="timeout"),
        )
        try:
            assert proc.stdin is not None
            proc.stdin.write("Review this request.")
            proc.stdin.close()
            proc.stdin = None
            deadline = time.monotonic() + 5
            while not (self.root / "started").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((self.root / "started").exists())
            pid = int((self.root / "started").read_text(encoding="utf-8"))
            proc.terminate()
            stdout, stderr = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 130, stderr)
            self.assertEqual(json.loads(stdout)["status"], "cancelled")
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_large_utf8_prompt_survives_heartbeats_before_cli_reads(self):
        spec = importlib.util.spec_from_file_location("review_launcher", SCRIPT)
        assert spec is not None and spec.loader is not None
        runner = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, "dont_write_bytecode", True):
            spec.loader.exec_module(runner)
        prompt = "Unicode λ `literal` $(touch nope)\n" * 10000
        progress = io.StringIO()
        with (
            mock.patch.dict(os.environ, dict(self.env, FAKE_CASE="slow"), clear=True),
            mock.patch.object(runner, "HEARTBEAT_SECONDS", 0.03),
            contextlib.redirect_stderr(progress),
        ):
            code = runner.execute(
                [str(self.bin / "claude")],
                cwd=self.root,
                env=runner.subscription_env(),
                prompt=prompt,
                timeout=5,
                stdout_path=self.root / "out.json",
                stderr_path=self.root / "out.stderr",
            )
        self.assertEqual(code, 0)
        self.assertIn("still running", progress.getvalue())
        self.assertEqual((self.root / "received").read_text(encoding="utf-8"), prompt)

    def test_utf8_result_with_ascii_stdio_environment(self):
        self.env["PYTHONIOENCODING"] = "ascii"
        proc, result, _ = self.run_case()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("λ", result["summary"])

    def test_resume_inherits_session_and_settings_and_preserves_prior_report(self):
        _, first, first_run = self.run_case(model="opus", effort="xhigh", mode="plan")
        old_report = (first_run / "report.md").read_bytes()
        proc, second, second_run = self.run_case(resume=first["session_id"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(second["request"]["model"], "opus")
        self.assertEqual(second["request"]["effort"], "xhigh")
        self.assertEqual(second["request"]["mode"], "plan")
        self.assertTrue(second["resumed"])
        self.assertNotEqual(first_run, second_run)
        self.assertEqual((first_run / "report.md").read_bytes(), old_report)
        self.assertEqual(second["findings"][0]["id"], "R1")
        self.assertEqual(second["findings"][0]["status"], "resolved")
        command = json.loads((second_run / "command.json").read_text(encoding="utf-8"))
        self.assertIn("--resume", command)
        self.assertNotIn("--session-id", command)
        session_meta = json.loads(Path(second["session_file"]).read_text(encoding="utf-8"))
        self.assertEqual(session_meta["completed_turns"], 2)

    def test_resume_by_metadata_path_can_override_model_and_effort(self):
        _, first, _ = self.run_case()
        proc, second, _ = self.run_case(resume=first["session_file"], model="opus", effort="max")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(second["request"]["model"], "opus")
        self.assertEqual(second["request"]["effort"], "max")

    def test_compact_findings_explicitly_report_omissions(self):
        proc, result, run = self.run_case("many_findings", **{"max-findings": 2})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(result["findings"]), 2)
        self.assertEqual(result["findings_total"], 9)
        self.assertEqual(result["findings_omitted"], 7)
        full_findings = json.loads((run / "review.json").read_text(encoding="utf-8"))["findings"]
        self.assertEqual(len(full_findings), 9)

    def plain_resume(self, token, *extra):
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--resume",
                token,
                "--output-root",
                str(self.root / "results"),
                *extra,
            ],
            input="Check the revised plan.",
            text=True,
            capture_output=True,
            env=self.env,
            timeout=10,
        )

    def test_unknown_or_unfinished_session_cannot_silently_start_over(self):
        proc = self.plain_resume("00000000-0000-0000-0000-000000000000")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Unknown review session", proc.stderr)
        _, failed, _ = self.run_case("timeout", timeout=1)
        proc = self.plain_resume(failed["session_id"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no completed resumable turn", proc.stderr)

    def test_resume_rejects_wrong_workspace_and_concurrent_turns(self):
        _, first, _ = self.run_case()
        proc = self.plain_resume(first["session_id"], "--cwd", str(self.bin))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("original workspace", proc.stderr)
        with Path(first["session_file"]).with_name("session.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            proc = self.plain_resume(first["session_id"])
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("already has a running turn", proc.stderr)

    def test_missing_native_history_is_an_error_without_fresh_fallback(self):
        _, first, _ = self.run_case()
        (self.root / "native-sessions" / (first["session_id"] + ".json")).unlink()
        proc, result, run = self.run_case(resume=first["session_id"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(result["status"], "protocol_error")
        self.assertFalse(result["resumable"])
        args = json.loads((run / "command.json").read_text(encoding="utf-8"))
        self.assertIn("--resume", args)
        self.assertNotIn("--session-id", args)

    @unittest.skipUnless(sys.platform == "linux", "Linux prctl LP64 binding")
    def test_linux_prctl_prebound_with_lp64_argtypes(self):
        spec = importlib.util.spec_from_file_location("review_prctl_check", SCRIPT)
        assert spec is not None and spec.loader is not None
        runner = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, "dont_write_bytecode", True):
            spec.loader.exec_module(runner)
        self.assertIsNotNone(runner.PRCTL)
        self.assertEqual(
            tuple(runner.PRCTL.argtypes),
            (
                ctypes.c_int,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_ulong,
            ),
        )
        self.assertIs(runner.PRCTL.restype, ctypes.c_int)

    @unittest.skipUnless(sys.platform == "linux", "Linux parent-death signal reset")
    def test_linux_parent_death_guard_resets_signals_before_prctl(self):
        spec = importlib.util.spec_from_file_location("review_pdeath_reset", SCRIPT)
        assert spec is not None and spec.loader is not None
        runner = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, "dont_write_bytecode", True):
            spec.loader.exec_module(runner)

        events: list[tuple] = []

        def fake_signal(sig, handler):
            events.append(("signal", sig, handler))

        def fake_prctl(*args):
            events.append(("prctl", *args))
            return 0

        parent_pid = os.getpid()
        with (
            mock.patch.object(runner.signal, "signal", side_effect=fake_signal),
            mock.patch.object(runner, "PRCTL", side_effect=fake_prctl),
            mock.patch.object(runner.os, "getppid", return_value=parent_pid),
        ):
            guard = runner.parent_death_guard(parent_pid)
            assert guard is not None
            guard()

        self.assertEqual(
            events,
            [
                ("signal", signal.SIGINT, signal.SIG_DFL),
                ("signal", signal.SIGTERM, signal.SIG_DFL),
                ("prctl", 1, signal.SIGTERM, 0, 0, 0),
            ],
        )

    @unittest.skipUnless(sys.platform == "linux", "Linux parent-death signal")
    def test_hard_killed_launcher_does_not_leave_claude_running(self):
        command = [
            sys.executable,
            str(SCRIPT),
            "--cwd",
            str(self.root),
            "--output-root",
            str(self.root / "results"),
        ]
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(self.env, FAKE_CASE="timeout"),
        )
        pid = None
        try:
            assert proc.stdin is not None
            proc.stdin.write("Review this request.")
            proc.stdin.close()
            proc.stdin = None
            deadline = time.monotonic() + 5
            while not (self.root / "started").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((self.root / "started").exists())
            pid = int((self.root / "started").read_text(encoding="utf-8"))
            proc.kill()
            proc.communicate(timeout=5)
            state = None
            for _ in range(100):
                try:
                    state = (
                        Path(f"/proc/{pid}/stat")
                        .read_text(encoding="utf-8")
                        .rsplit(") ", 1)[1]
                        .split()[0]
                    )
                except (FileNotFoundError, ProcessLookupError):
                    state = None
                if state is None or state == "Z":
                    break
                time.sleep(0.02)
            self.assertIn(state, [None, "Z"], "Claude remained running after launcher death")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
            if pid:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, 9)


class HarnessAdapterContractTests(unittest.TestCase):
    """Contract tests covering all 6 Claude/Codex orchestration adapters and claude-review."""

    ORCHESTRATION_ADAPTERS: tuple[tuple[str, str], ...] = (
        ("claude", "dev-swarm"),
        ("claude", "review"),
        ("claude", "dev-swarm-zircon"),
        ("codex", "dev-swarm"),
        ("codex", "review"),
        ("codex", "dev-swarm-zircon"),
    )

    ALL_NEW_SKILLS: tuple[tuple[str, str], ...] = (
        *ORCHESTRATION_ADAPTERS,
        ("codex", "claude-review"),
    )

    COMMON_PRIMITIVES: tuple[str, ...] = (
        "MODEL_MAP",
        "SPAWN_WORKER",
        "WAIT_FOR_WORKERS",
        "SWITCH_MODEL",
        "KILL_WORKER",
        "HANDOFF",
        "READ_ARTIFACT",
    )

    SWARM_EXTRA_PRIMITIVES: tuple[str, ...] = (
        "CLEANROOM_BARRIER",
        "DECISION_LEDGER",
        "STATE_LEDGER",
    )

    AGY_ONLY_TERMS: tuple[str, ...] = (
        "swarm_wait",
        "read_board",
        "post_message",
        "Neon xhigh",
        "Neon (xhigh)",
        "Carbon High",
        "Carbon (High)",
        "gemini-4-neon-xhigh",
        "gemini-4-carbon-high",
        "claude-opus-5-5-high",
        'workspace: "share"',
        "open_board",
        "customization://swarm",
        "<artifactDir>",
        "<appDataDir>",
    )

    ZIRCON_DOMAIN_RE = re.compile(r"\b(?:fuchsia|zircon|fidl|babeldiff|jj)\b", re.IGNORECASE)
    JJ_VCS_RE = re.compile(r"\bjj\b", re.IGNORECASE)

    def test_all_seven_skills_pass_validation_and_portability_checks(self):
        for harness, skill in self.ALL_NEW_SKILLS:
            skill_dir = REPO_ROOT / "harnesses" / harness / skill
            with self.subTest(harness=harness, skill=skill):
                self.assertEqual(init.validate_skill_dir(skill_dir), [])
                for path in sorted(skill_dir.rglob("*")):
                    if path.is_symlink() or not path.is_file():
                        continue
                    text = path.read_text(encoding="utf-8")
                    self.assertIsNone(
                        init.HARDCODED_USER_PATH_RE.search(text),
                        f"Hardcoded user path in {path.relative_to(REPO_ROOT)}",
                    )

    def test_orchestration_adapter_line_budget_symlinks_and_primitives(self):
        for harness, skill in self.ORCHESTRATION_ADAPTERS:
            skill_dir = REPO_ROOT / "harnesses" / harness / skill
            skill_md = skill_dir / "SKILL.md"
            text = skill_md.read_text(encoding="utf-8")
            line_count = len(text.splitlines())
            with self.subTest(harness=harness, skill=skill):
                self.assertGreaterEqual(
                    line_count, 60, f"{skill_md} too short ({line_count} lines)"
                )
                self.assertLessEqual(
                    line_count, 100, f"{skill_md} exceeds thin-adapter cap ({line_count} lines)"
                )
                core_link = skill_dir / "core"
                self.assertTrue(core_link.is_symlink(), f"{core_link} must be a symlink")
                self.assertEqual(os.readlink(core_link), f"../../../core/{skill}")
                self.assertEqual(
                    core_link.resolve(strict=True),
                    (REPO_ROOT / "core" / skill).resolve(),
                )

                required = list(self.COMMON_PRIMITIVES)
                if skill in {"dev-swarm", "dev-swarm-zircon"}:
                    required.extend(self.SWARM_EXTRA_PRIMITIVES)
                for primitive in required:
                    self.assertIn(primitive, text, f"Missing {primitive} in {skill_md}")

                for forbidden in self.AGY_ONLY_TERMS:
                    self.assertNotIn(
                        forbidden,
                        text,
                        f"Antigravity-only term {forbidden!r} leaked into {skill_md}",
                    )

                if harness == "claude":
                    self.assertIn("opus", text)
                    self.assertIn("fable", text)
                    self.assertIn("haiku", text)
                else:
                    self.assertIn("gpt-6-sol", text)
                    self.assertIn("gpt-6-astra", text)
                    self.assertIn("fork_context = false", text)
                self.assertIn("realpath", text)

    def test_generic_adapters_contain_zero_zircon_domain_terms(self):
        for harness in ("claude", "codex"):
            for skill in ("dev-swarm", "review"):
                skill_md = REPO_ROOT / "harnesses" / harness / skill / "SKILL.md"
                text = skill_md.read_text(encoding="utf-8")
                with self.subTest(harness=harness, skill=skill):
                    match = self.ZIRCON_DOMAIN_RE.search(text)
                    self.assertIsNone(
                        match,
                        f"Domain term {match.group(0)!r} leaked into {skill_md}"
                        if match
                        else "",
                    )
            zircon_md = REPO_ROOT / "harnesses" / harness / "dev-swarm-zircon" / "SKILL.md"
            zircon_text = zircon_md.read_text(encoding="utf-8")
            with self.subTest(harness=harness, skill="dev-swarm-zircon"):
                self.assertIsNone(
                    self.JJ_VCS_RE.search(zircon_text),
                    f"Non-git VCS term 'jj' leaked into {zircon_md}",
                )

    def test_codex_claude_review_assets_permissions_and_toml(self):
        skill_dir = REPO_ROOT / "harnesses" / "codex" / "claude-review"
        script_path = skill_dir / "scripts" / "claude_review.py"
        toml_path = skill_dir / "references" / "claude-review.toml"
        self.assertTrue(script_path.is_file())
        self.assertTrue(script_path.stat().st_mode & stat.S_IXUSR, "claude_review.py must be +x")
        self.assertFalse(
            (skill_dir / "scripts" / "test_claude_review.py").exists(),
            "Tests must live in tests/, not inside scripts/",
        )
        parsed = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        self.assertEqual(parsed["name"], "claude-review")
        self.assertEqual(parsed["model"], "gpt-6-sol")
        self.assertEqual(parsed["model_reasoning_effort"], "low")
        self.assertIn("claude_review.py", parsed["developer_instructions"])

    def test_global_agents_is_harness_and_model_agnostic(self):
        global_agents = REPO_ROOT / "global" / "AGENTS.md"
        text = global_agents.read_text(encoding="utf-8")
        self.assertIsNone(init.HARDCODED_USER_PATH_RE.search(text))
        for forbidden in (*self.AGY_ONLY_TERMS, "narrator", "monitor"):
            self.assertNotIn(
                forbidden,
                text,
                f"Harness-specific term {forbidden!r} leaked into global/AGENTS.md",
            )
        for required in (
            "dev-swarm",
            "dev-swarm-zircon",
            "review",
            "MODEL_MAP",
            "manage_worktree.py",
        ):
            self.assertIn(required, text)


if __name__ == "__main__":
    unittest.main()
