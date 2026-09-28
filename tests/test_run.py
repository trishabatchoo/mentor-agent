"""Offline tests for run.py: profile selection, student-name/transcript/
session-datetime precedence, and missing-input validation.

These tests never import agent.py and never call the Anthropic API. The
end-to-end tests inject a stand-in 'main' module into sys.modules so
run.main()'s `import main` resolves to that stand-in instead of the real
main.py (which imports agent.py, which requires ANTHROPIC_API_KEY at
import time).
"""

import io
import os
import sys
import types
import unittest
import unittest.mock
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run  # noqa: E402


class EnvironmentIsolationMixin:
    """Snapshot and restore os.environ and sys.modules['main'] per test.

    Only needed by tests that call run.main() (which mutates the real
    process environment and imports a 'main' module); tests that pass
    their own plain dict as `env` don't touch process state and don't
    need this.
    """

    def setUp(self):
        super().setUp()
        self._env_snapshot = dict(os.environ)
        self._had_main_module = "main" in sys.modules
        self._main_module_snapshot = sys.modules.get("main")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_snapshot)
        if self._had_main_module:
            sys.modules["main"] = self._main_module_snapshot
        else:
            sys.modules.pop("main", None)
        super().tearDown()


class ParseArgsTests(unittest.TestCase):
    def test_example_profile_no_overrides(self):
        args = run.parse_args(["example"])
        self.assertEqual(args.profile, "example")
        self.assertIsNone(args.student)
        self.assertIsNone(args.transcript)
        self.assertIsNone(args.session_datetime)

    def test_local_profile_with_all_overrides(self):
        args = run.parse_args(
            [
                "local",
                "--student",
                "Amya",
                "--transcript",
                "fixtures/private/amya-september-8.txt",
                "--session-datetime",
                "September 8, 2026 11:00 AM ET",
            ]
        )
        self.assertEqual(args.profile, "local")
        self.assertEqual(args.student, "Amya")
        self.assertEqual(args.transcript, "fixtures/private/amya-september-8.txt")
        self.assertEqual(args.session_datetime, "September 8, 2026 11:00 AM ET")

    def test_unknown_profile_exits_nonzero_with_usage(self):
        with redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as ctx:
                run.parse_args(["bogus"])
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("invalid choice", stderr.getvalue())

    def test_missing_profile_exits_nonzero_with_usage(self):
        with redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as ctx:
                run.parse_args([])
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("usage", stderr.getvalue().lower())


class StudentNamePrecedenceTests(unittest.TestCase):
    def test_cli_flag_beats_env_var(self):
        env = {"MENTOR_STUDENT_NAME": "FromEnv"}
        self.assertEqual(
            run.resolve_student_name("local", "FromCli", env), "FromCli"
        )

    def test_env_var_used_when_no_cli_flag(self):
        env = {"MENTOR_STUDENT_NAME": "FromEnv"}
        self.assertEqual(
            run.resolve_student_name("example", None, env), "FromEnv"
        )

    def test_example_defaults_to_jordan(self):
        self.assertEqual(run.resolve_student_name("example", None, {}), "Jordan")

    def test_local_has_no_default_student(self):
        with self.assertRaises(SystemExit):
            run.resolve_student_name("local", None, {})

    def test_local_accepts_env_var(self):
        env = {"MENTOR_STUDENT_NAME": "Amya"}
        self.assertEqual(run.resolve_student_name("local", None, env), "Amya")

    def test_local_error_message_is_actionable(self):
        with self.assertRaises(SystemExit) as ctx:
            run.resolve_student_name("local", None, {})
        message = str(ctx.exception)
        self.assertIn("--student", message)
        self.assertIn("MENTOR_STUDENT_NAME", message)


class TranscriptPathPrecedenceTests(unittest.TestCase):
    def test_cli_flag_beats_env_var(self):
        env = {"TRANSCRIPT_PATH": "fixtures/private/from-env.txt"}
        resolved = run.resolve_transcript_path("local", "fixtures/private/from-cli.txt", env)
        self.assertEqual(resolved, run.PROJECT_DIR / "fixtures/private/from-cli.txt")

    def test_env_var_used_when_no_cli_flag(self):
        env = {"TRANSCRIPT_PATH": "fixtures/private/from-env.txt"}
        resolved = run.resolve_transcript_path("example", None, env)
        self.assertEqual(resolved, run.PROJECT_DIR / "fixtures/private/from-env.txt")

    def test_example_defaults_to_bundled_transcript(self):
        resolved = run.resolve_transcript_path("example", None, {})
        self.assertEqual(
            resolved, run.PROJECT_DIR / run.DEFAULT_TRANSCRIPT_PATH
        )

    def test_local_has_no_default_and_errors(self):
        with self.assertRaises(SystemExit) as ctx:
            run.resolve_transcript_path("local", None, {})
        message = str(ctx.exception)
        self.assertIn("--transcript", message)
        self.assertIn("TRANSCRIPT_PATH", message)

    def test_local_accepts_env_var(self):
        env = {"TRANSCRIPT_PATH": "fixtures/private/amya.txt"}
        resolved = run.resolve_transcript_path("local", None, env)
        self.assertEqual(resolved, run.PROJECT_DIR / "fixtures/private/amya.txt")

    def test_relative_cli_path_resolved_against_project_dir(self):
        resolved = run.resolve_transcript_path(
            "local", "fixtures/private/amya-september-8.txt", {}
        )
        self.assertTrue(resolved.is_absolute())
        self.assertEqual(
            resolved,
            run.PROJECT_DIR / "fixtures/private/amya-september-8.txt",
        )

    def test_absolute_cli_path_used_unmodified(self):
        absolute = Path("/tmp/some-other-place/transcript.txt")
        resolved = run.resolve_transcript_path("local", str(absolute), {})
        self.assertEqual(resolved, absolute)

    def test_absolute_env_path_used_unmodified(self):
        absolute = Path("/tmp/some-other-place/transcript.txt")
        env = {"TRANSCRIPT_PATH": str(absolute)}
        resolved = run.resolve_transcript_path("example", None, env)
        self.assertEqual(resolved, absolute)


class SessionDatetimePrecedenceTests(unittest.TestCase):
    def test_cli_flag_beats_env_var(self):
        env = {"SESSION_DATETIME": "FromEnv"}
        self.assertEqual(
            run.resolve_session_datetime("local", "FromCli", env), "FromCli"
        )

    def test_env_var_used_when_no_cli_flag(self):
        env = {"SESSION_DATETIME": "FromEnv"}
        self.assertEqual(
            run.resolve_session_datetime("example", None, env), "FromEnv"
        )

    def test_example_defaults_to_bundled_datetime(self):
        self.assertEqual(
            run.resolve_session_datetime("example", None, {}),
            run.DEFAULT_SESSION_DATETIME,
        )

    def test_local_has_no_default_and_errors(self):
        with self.assertRaises(SystemExit) as ctx:
            run.resolve_session_datetime("local", None, {})
        message = str(ctx.exception)
        self.assertIn("--session-datetime", message)
        self.assertIn("SESSION_DATETIME", message)

    def test_local_accepts_env_var(self):
        env = {"SESSION_DATETIME": "September 8, 2026 11:00 AM ET"}
        self.assertEqual(
            run.resolve_session_datetime("local", None, env),
            "September 8, 2026 11:00 AM ET",
        )


class ValidateProfileFilesTests(unittest.TestCase):
    def test_example_profile_files_all_present(self):
        transcript_path = run.PROJECT_DIR / run.DEFAULT_TRANSCRIPT_PATH
        self.assertEqual(run.validate_profile_files("example", transcript_path), [])

    def test_missing_transcript_is_reported_by_path(self):
        # fixtures/private/ is gitignored and not expected to exist in a
        # fresh checkout, so this should always be reported as missing.
        transcript_path = run.PROJECT_DIR / "fixtures/private/does-not-exist.txt"
        missing = run.validate_profile_files("local", transcript_path)
        self.assertIn(str(transcript_path), missing)

    def test_missing_entries_are_bare_paths_not_file_contents(self):
        transcript_path = run.PROJECT_DIR / "fixtures/private/does-not-exist.txt"
        missing = run.validate_profile_files("local", transcript_path)
        self.assertGreater(len(missing), 0)
        for entry in missing:
            self.assertIsInstance(entry, str)
            self.assertNotIn("\n", entry)


class ApplyProfileEnvTests(unittest.TestCase):
    def test_example_profile_sets_expected_keys(self):
        env = {"ANTHROPIC_API_KEY": "sk-should-not-move", "UNRELATED": "kept"}
        transcript_path = run.PROJECT_DIR / run.DEFAULT_TRANSCRIPT_PATH
        run.apply_profile_env(
            "example", "Jordan", transcript_path, run.DEFAULT_SESSION_DATETIME, env
        )

        self.assertEqual(env["MENTOR_STUDENT_NAME"], "Jordan")
        self.assertTrue(env["PROMPTS_DIR"].endswith("prompts/example"))
        self.assertTrue(
            env["STUDENTS_DATA_PATH"].endswith("data/students.example.json")
        )
        self.assertTrue(env["REFERENCE_ROOT"].endswith("references/example"))
        self.assertEqual(env["TRANSCRIPT_PATH"], str(transcript_path))
        self.assertEqual(env["SESSION_DATETIME"], run.DEFAULT_SESSION_DATETIME)
        # Parent-shell secrets and unrelated vars must survive untouched.
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-should-not-move")
        self.assertEqual(env["UNRELATED"], "kept")

    def test_local_profile_sets_expected_keys(self):
        env = {}
        transcript_path = run.PROJECT_DIR / "fixtures/private/amya.txt"
        run.apply_profile_env(
            "local", "Amya", transcript_path, "September 8, 2026 11:00 AM ET", env
        )

        self.assertEqual(env["MENTOR_STUDENT_NAME"], "Amya")
        self.assertTrue(env["PROMPTS_DIR"].endswith("prompts/local"))
        self.assertTrue(
            env["STUDENTS_DATA_PATH"].endswith("data/students.local.json")
        )
        self.assertTrue(env["REFERENCE_ROOT"].endswith("references/local"))
        self.assertEqual(env["TRANSCRIPT_PATH"], str(transcript_path))
        self.assertEqual(env["SESSION_DATETIME"], "September 8, 2026 11:00 AM ET")


class MainEndToEndTests(EnvironmentIsolationMixin, unittest.TestCase):
    def _install_fake_app_main(self):
        """Replace the 'main' module run.py imports with a stand-in that
        records the environment it saw instead of calling run_agent /
        the Anthropic API.
        """
        calls = []
        fake_module = types.ModuleType("main")

        def fake_main():
            calls.append(dict(os.environ))

        fake_module.main = fake_main
        sys.modules["main"] = fake_module
        return calls

    def test_env_applied_before_app_main_runs(self):
        calls = self._install_fake_app_main()
        run.main(["example"])

        self.assertEqual(len(calls), 1)
        seen_env = calls[0]
        self.assertEqual(seen_env["MENTOR_STUDENT_NAME"], "Jordan")
        self.assertTrue(seen_env["PROMPTS_DIR"].endswith("prompts/example"))
        self.assertTrue(
            seen_env["TRANSCRIPT_PATH"].endswith(
                "fixtures/example/post_session_transcript.txt"
            )
        )
        self.assertEqual(seen_env["SESSION_DATETIME"], run.DEFAULT_SESSION_DATETIME)

    def test_cli_overrides_reach_environment(self):
        # fixtures/private/ is gitignored and not expected to exist in this
        # checkout; file validation is covered separately by
        # ValidateProfileFilesTests, so it's stubbed out here to isolate
        # CLI-override propagation.
        calls = self._install_fake_app_main()
        with unittest.mock.patch.object(run, "validate_profile_files", return_value=[]):
            run.main(
                [
                    "local",
                    "--student",
                    "Amya",
                    "--transcript",
                    "fixtures/private/amya-september-8.txt",
                    "--session-datetime",
                    "September 8, 2026 11:00 AM ET",
                ]
            )
        seen_env = calls[0]
        self.assertEqual(seen_env["MENTOR_STUDENT_NAME"], "Amya")
        self.assertEqual(
            seen_env["TRANSCRIPT_PATH"],
            str(run.PROJECT_DIR / "fixtures/private/amya-september-8.txt"),
        )
        self.assertEqual(seen_env["SESSION_DATETIME"], "September 8, 2026 11:00 AM ET")

    def test_env_var_fallbacks_reach_environment_for_local(self):
        calls = self._install_fake_app_main()
        os.environ["MENTOR_STUDENT_NAME"] = "Priya"
        os.environ["TRANSCRIPT_PATH"] = "fixtures/private/priya.txt"
        os.environ["SESSION_DATETIME"] = "September 9, 2026 3:00 PM ET"
        with unittest.mock.patch.object(run, "validate_profile_files", return_value=[]):
            run.main(["local"])
        seen_env = calls[0]
        self.assertEqual(seen_env["MENTOR_STUDENT_NAME"], "Priya")
        self.assertEqual(
            seen_env["TRANSCRIPT_PATH"],
            str(run.PROJECT_DIR / "fixtures/private/priya.txt"),
        )
        self.assertEqual(seen_env["SESSION_DATETIME"], "September 9, 2026 3:00 PM ET")

    def test_missing_local_student_exits_before_touching_app(self):
        calls = self._install_fake_app_main()
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                run.main(["local"])
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertEqual(calls, [])

    def test_missing_local_transcript_exits_before_touching_app(self):
        calls = self._install_fake_app_main()
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                run.main(["local", "--student", "Amya"])
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertEqual(calls, [])

    def test_missing_local_session_datetime_exits_before_touching_app(self):
        calls = self._install_fake_app_main()
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                run.main(
                    [
                        "local",
                        "--student",
                        "Amya",
                        "--transcript",
                        "fixtures/private/amya-september-8.txt",
                    ]
                )
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertEqual(calls, [])

    def test_missing_files_exit_before_touching_app(self):
        # The 'local' profile's transcript (fixtures/private/...) is
        # gitignored and not expected to exist in this checkout, so this
        # should fail validation before the app ever runs.
        calls = self._install_fake_app_main()
        with redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as ctx:
                run.main(
                    [
                        "local",
                        "--student",
                        "Amya",
                        "--transcript",
                        "fixtures/private/amya-september-8.txt",
                        "--session-datetime",
                        "September 8, 2026 11:00 AM ET",
                    ]
                )
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertEqual(calls, [])
        self.assertIn("missing", stderr.getvalue().lower())

    def test_parent_env_secrets_survive_a_full_run(self):
        calls = self._install_fake_app_main()
        os.environ["ANTHROPIC_API_KEY"] = "sk-test-sentinel"
        os.environ["ANTHROPIC_MODEL"] = "model-sentinel"
        run.main(["example"])

        self.assertEqual(calls[0]["ANTHROPIC_API_KEY"], "sk-test-sentinel")
        self.assertEqual(calls[0]["ANTHROPIC_MODEL"], "model-sentinel")


if __name__ == "__main__":
    unittest.main()
