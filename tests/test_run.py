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

    # Keys run.py's profile resolution reads or writes. Cleared at the
    # start of every test so a developer's real shell exports (e.g. a
    # locally-set MENTOR_STUDENT_NAME) can't change test outcomes.
    _MANAGED_ENV_KEYS = (
        "MENTOR_STUDENT_NAME",
        "TRANSCRIPT_PATH",
        "SESSION_DATETIME",
        "PROMPTS_DIR",
        "STUDENTS_DATA_PATH",
        "REFERENCE_ROOT",
        "STUDENT_CONTEXT_BACKEND",
        "NOTION_API_KEY",
        "NOTION_PARENT_PAGE_ID",
    )

    # Placeholder Notion settings for tests that run the 'local' profile
    # past its environment check. Never used to contact Notion.
    def set_placeholder_notion_env(self):
        os.environ["NOTION_API_KEY"] = "placeholder-notion-key"
        os.environ["NOTION_PARENT_PAGE_ID"] = "placeholder-parent-page"

    def setUp(self):
        super().setUp()
        self._env_snapshot = dict(os.environ)
        for key in self._MANAGED_ENV_KEYS:
            os.environ.pop(key, None)
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
                "Casey",
                "--transcript",
                "fixtures/private/session-2026-09-08.txt",
                "--session-datetime",
                "September 8, 2026 11:00 AM ET",
            ]
        )
        self.assertEqual(args.profile, "local")
        self.assertEqual(args.student, "Casey")
        self.assertEqual(args.transcript, "fixtures/private/session-2026-09-08.txt")
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
        env = {"MENTOR_STUDENT_NAME": "Casey"}
        self.assertEqual(run.resolve_student_name("local", None, env), "Casey")

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
        env = {"TRANSCRIPT_PATH": "fixtures/private/session.txt"}
        resolved = run.resolve_transcript_path("local", None, env)
        self.assertEqual(resolved, run.PROJECT_DIR / "fixtures/private/session.txt")

    def test_relative_cli_path_resolved_against_project_dir(self):
        resolved = run.resolve_transcript_path(
            "local", "fixtures/private/session-2026-09-08.txt", {}
        )
        self.assertTrue(resolved.is_absolute())
        self.assertEqual(
            resolved,
            run.PROJECT_DIR / "fixtures/private/session-2026-09-08.txt",
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
        env = {"ANTHROPIC_API_KEY": "test-api-key-sentinel", "UNRELATED": "kept"}
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
        self.assertEqual(env["STUDENT_CONTEXT_BACKEND"], "json")
        self.assertEqual(env["TRANSCRIPT_PATH"], str(transcript_path))
        self.assertEqual(env["SESSION_DATETIME"], run.DEFAULT_SESSION_DATETIME)
        # Parent-shell secrets and unrelated vars must survive untouched.
        self.assertEqual(env["ANTHROPIC_API_KEY"], "test-api-key-sentinel")
        self.assertEqual(env["UNRELATED"], "kept")

    def test_local_profile_sets_expected_keys(self):
        env = {}
        transcript_path = run.PROJECT_DIR / "fixtures/private/session.txt"
        run.apply_profile_env(
            "local", "Casey", transcript_path, "September 8, 2026 11:00 AM ET", env
        )

        self.assertEqual(env["MENTOR_STUDENT_NAME"], "Casey")
        self.assertTrue(env["PROMPTS_DIR"].endswith("prompts/local"))
        # The local profile reads student context from Notion, so it has
        # no student data file (intentional change from the JSON setup).
        self.assertNotIn("STUDENTS_DATA_PATH", env)
        self.assertEqual(env["STUDENT_CONTEXT_BACKEND"], "notion")
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
        self.set_placeholder_notion_env()
        with unittest.mock.patch.object(run, "validate_profile_files", return_value=[]):
            run.main(
                [
                    "local",
                    "--student",
                    "Casey",
                    "--transcript",
                    "fixtures/private/session-2026-09-08.txt",
                    "--session-datetime",
                    "September 8, 2026 11:00 AM ET",
                ]
            )
        seen_env = calls[0]
        self.assertEqual(seen_env["MENTOR_STUDENT_NAME"], "Casey")
        self.assertEqual(
            seen_env["TRANSCRIPT_PATH"],
            str(run.PROJECT_DIR / "fixtures/private/session-2026-09-08.txt"),
        )
        self.assertEqual(seen_env["SESSION_DATETIME"], "September 8, 2026 11:00 AM ET")

    def test_env_var_fallbacks_reach_environment_for_local(self):
        calls = self._install_fake_app_main()
        os.environ["MENTOR_STUDENT_NAME"] = "Priya"
        os.environ["TRANSCRIPT_PATH"] = "fixtures/private/priya.txt"
        os.environ["SESSION_DATETIME"] = "September 9, 2026 3:00 PM ET"
        self.set_placeholder_notion_env()
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
                run.main(["local", "--student", "Casey"])
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
                        "Casey",
                        "--transcript",
                        "fixtures/private/session-2026-09-08.txt",
                    ]
                )
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertEqual(calls, [])

    def test_missing_files_exit_before_touching_app(self):
        # An unmistakably nonexistent transcript path, so this test's
        # outcome doesn't depend on whether a real private transcript
        # happens to exist locally.
        calls = self._install_fake_app_main()
        with redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as ctx:
                run.main(
                    [
                        "local",
                        "--student",
                        "Casey",
                        "--transcript",
                        "fixtures/private/definitely-does-not-exist-test-transcript.txt",
                        "--session-datetime",
                        "September 8, 2026 11:00 AM ET",
                    ]
                )
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertEqual(calls, [])
        self.assertIn("missing", stderr.getvalue().lower())

    def test_parent_env_secrets_survive_a_full_run(self):
        calls = self._install_fake_app_main()
        os.environ["ANTHROPIC_API_KEY"] = "test-api-key-sentinel"
        os.environ["ANTHROPIC_MODEL"] = "model-sentinel"
        run.main(["example"])

        self.assertEqual(calls[0]["ANTHROPIC_API_KEY"], "test-api-key-sentinel")
        self.assertEqual(calls[0]["ANTHROPIC_MODEL"], "model-sentinel")


class BackendSelectionTests(unittest.TestCase):
    def test_profiles_select_backends_explicitly(self):
        self.assertEqual(run.PROFILES["example"]["STUDENT_CONTEXT_BACKEND"], "json")
        self.assertEqual(run.PROFILES["local"]["STUDENT_CONTEXT_BACKEND"], "notion")

    def test_example_profile_keeps_its_student_data_file(self):
        self.assertEqual(
            run.PROFILES["example"]["STUDENTS_DATA_PATH"], "data/students.example.json"
        )
        self.assertNotIn("STUDENTS_DATA_PATH", run.PROFILES["local"])

    def test_local_does_not_require_a_student_data_file(self):
        transcript_path = run.PROJECT_DIR / "fixtures/private/does-not-exist.txt"
        missing = run.validate_profile_files("local", transcript_path)
        self.assertFalse(any("students" in entry for entry in missing))

    def test_example_needs_no_notion_environment(self):
        self.assertEqual(run.missing_backend_env("example", {}), [])

    def test_local_reports_missing_notion_variable_names(self):
        self.assertEqual(
            run.missing_backend_env("local", {}),
            ["NOTION_API_KEY", "NOTION_PARENT_PAGE_ID"],
        )
        self.assertEqual(
            run.missing_backend_env(
                "local", {"NOTION_API_KEY": "k", "NOTION_PARENT_PAGE_ID": ""}
            ),
            ["NOTION_PARENT_PAGE_ID"],
        )
        self.assertEqual(
            run.missing_backend_env(
                "local", {"NOTION_API_KEY": "k", "NOTION_PARENT_PAGE_ID": "p"}
            ),
            [],
        )

    def test_local_removes_stale_inherited_students_path(self):
        env = {"STUDENTS_DATA_PATH": "/stale/students.json"}
        run.apply_profile_env("local", "Casey", Path("/t.txt"), "now", env)
        self.assertNotIn("STUDENTS_DATA_PATH", env)

    def test_apply_profile_env_leaves_notion_settings_untouched(self):
        env = {"NOTION_API_KEY": "SENTINEL-NOTION-KEY", "NOTION_VERSION": "v"}
        run.apply_profile_env("local", "Casey", Path("/t.txt"), "now", env)
        self.assertEqual(env["NOTION_API_KEY"], "SENTINEL-NOTION-KEY")
        self.assertEqual(env["NOTION_VERSION"], "v")


class BackendEnvEndToEndTests(EnvironmentIsolationMixin, unittest.TestCase):
    def test_missing_notion_env_exits_before_touching_app(self):
        calls = []
        fake_module = types.ModuleType("main")
        fake_module.main = lambda: calls.append(True)
        sys.modules["main"] = fake_module
        # Only the parent page id is missing; the key's value must never
        # be echoed in the error output.
        os.environ["NOTION_API_KEY"] = "SENTINEL-NOTION-KEY"
        with unittest.mock.patch.object(run, "validate_profile_files", return_value=[]):
            with redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit) as ctx:
                    run.main(
                        [
                            "local",
                            "--student",
                            "Casey",
                            "--transcript",
                            "fixtures/private/session.txt",
                            "--session-datetime",
                            "September 8, 2026 11:00 AM ET",
                        ]
                    )
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertEqual(calls, [])
        output = stderr.getvalue()
        self.assertIn("NOTION_PARENT_PAGE_ID", output)
        self.assertNotIn("NOTION_API_KEY", output)
        self.assertNotIn("SENTINEL-NOTION-KEY", output)


if __name__ == "__main__":
    unittest.main()
