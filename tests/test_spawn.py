import contextlib
import io
import os
import pathlib
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import review_module

PROJECT = "reviewer"
GOAL = "add a review tool"
LONG_ENOUGH_REVIEW = "this review body is long enough to look real"
GRADED_REVIEW = f"<grade>B</grade>\n<review>\n{LONG_ENOUGH_REVIEW}\n</review>"

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "fake_harness.py"


def process_is_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_for_exit(pid, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and process_is_alive(pid):
        time.sleep(0.05)
    return not process_is_alive(pid)


def wait_for_pidfile(path, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            return int(path.read_text())
        except (FileNotFoundError, ValueError):
            time.sleep(0.05)
    raise AssertionError(f"fixture never recorded a pid in {path}")


def run_main(review, *argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = review.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class SpawnTestCase(unittest.TestCase):
    """Each test gets a freshly loaded module whose tables it may rewrite."""

    def setUp(self):
        self.review = review_module.load()
        self.harness_argv = {}
        self.use_fake_harness()
        self.use_temporary_database()

    def use_temporary_database(self):
        """Keep every test off the real ~/.local/share/review/reviews.db."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = pathlib.Path(directory.name) / "reviews.db"
        self.set_env(REVIEW_DB=str(self.db_path))

    def use_fake_harness(self, argv=None, family="plain"):
        self.fake_reviewer = self.install_harness(
            "fake",
            family,
            argv or (sys.executable, str(FIXTURE), self.review.PROMPT_PLACEHOLDER),
        )
        self.route_to(self.fake_reviewer)

    def install_harness(self, model, family, argv):
        self.harness_argv[family, model] = argv

        def build(selected_model):
            return self.review.Harness(
                family, self.harness_argv[family, selected_model]
            )

        self.review.HARNESSES[family] = build
        return self.review.Reviewer(family, model)

    def route_to(self, *reviewers):
        """Send every author to these reviewers, bypassing the config file."""
        rules = [(re.compile("."), tuple(reviewers))]
        self.review.load_rules = lambda path=None: rules

    def set_env(self, **values):
        patcher = mock.patch.dict(os.environ, values)
        patcher.start()
        self.addCleanup(patcher.stop)


class PromptTest(SpawnTestCase):
    def test_prompt_carries_the_description_the_cwd_and_a_read_only_instruction(self):
        prompt = self.review.build_prompt(GOAL, "the uncommitted changes", "/some/where")
        self.assertIn("the uncommitted changes", prompt)
        self.assertIn("/some/where", prompt)
        self.assertIn("Make no changes", prompt)

    def test_prompt_tells_the_reviewer_to_resolve_the_description_itself(self):
        prompt = self.review.build_prompt(GOAL, "the current branch", "/some/where")
        self.assertIn("do not rely on the description alone", prompt)


class ArgvTest(SpawnTestCase):
    def test_prompt_replaces_the_placeholder(self):
        argv = self.review.resolve_argv(self.fake_reviewer, "PROMPT TEXT")
        self.assertIn("PROMPT TEXT", argv)
        self.assertNotIn(self.review.PROMPT_PLACEHOLDER, argv)

    def test_placeholder_is_matched_by_value_not_identity(self):
        self.use_fake_harness(argv=("bin", "".join(["<prom", "pt>"])))
        self.assertEqual(
            self.review.resolve_argv(self.fake_reviewer, "TEXT"), ["bin", "TEXT"]
        )

    def test_template_without_a_placeholder_is_rejected(self):
        self.use_fake_harness(argv=("bin", "--flag"))
        with self.assertRaises(ValueError):
            self.review.resolve_argv(self.fake_reviewer, "TEXT")

    def test_template_with_two_placeholders_is_rejected(self):
        placeholder = self.review.PROMPT_PLACEHOLDER
        self.use_fake_harness(argv=("bin", placeholder, placeholder))
        with self.assertRaises(ValueError):
            self.review.resolve_argv(self.fake_reviewer, "TEXT")

    def test_claude_argv_puts_the_prompt_before_every_long_flag(self):
        reviewer = self.review.Reviewer("claude", "any-versioned-model")
        argv = self.review.resolve_argv(reviewer, "PROMPT TEXT")
        prompt_index = argv.index("PROMPT TEXT")
        flag_indexes = [i for i, part in enumerate(argv) if part.startswith("--")]
        self.assertTrue(flag_indexes)
        self.assertLess(prompt_index, min(flag_indexes))


class ConfiguredTableTest(unittest.TestCase):
    """Checks the shipped tables, not the fixtures the other tests install."""

    def setUp(self):
        self.review = review_module.load()

    def test_every_harness_template_resolves(self):
        for family, builder in self.review.HARNESSES.items():
            with self.subTest(harness=family):
                reviewer = self.review.Reviewer(family, "model-under-test")
                harness = builder(reviewer.model)
                needs_file = self.review.OUTPUT_PLACEHOLDER in harness.argv
                output = pathlib.Path("/tmp/review-output") if needs_file else None
                argv = self.review.resolve_argv(reviewer, "PROMPT", output)
                self.assertIn("PROMPT", argv)
                self.assertNotIn(self.review.PROMPT_PLACEHOLDER, argv)
                self.assertNotIn(self.review.OUTPUT_PLACEHOLDER, argv)



class TimeoutConfigurationTest(SpawnTestCase):
    def test_unset_uses_the_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(self.review.TIMEOUT_ENV_VAR, None)
            self.assertEqual(
                self.review.reviewer_timeout(), self.review.DEFAULT_TIMEOUT_SECONDS
            )

    def test_empty_is_treated_as_unset(self):
        self.set_env(REVIEW_TIMEOUT="")
        self.assertEqual(
            self.review.reviewer_timeout(), self.review.DEFAULT_TIMEOUT_SECONDS
        )

    def test_a_number_is_honored(self):
        self.set_env(REVIEW_TIMEOUT="12")
        self.assertEqual(self.review.reviewer_timeout(), 12)

    def test_a_non_number_is_a_usage_error_not_a_traceback(self):
        self.set_env(REVIEW_TIMEOUT="abc")
        code, _, err = run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")
        self.assertEqual(code, self.review.EXIT_USAGE)
        self.assertIn("REVIEW_TIMEOUT", err)

    def test_a_non_positive_value_is_a_usage_error(self):
        self.set_env(REVIEW_TIMEOUT="-5")
        code, _, err = run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")
        self.assertEqual(code, self.review.EXIT_USAGE)
        self.assertIn("greater than zero", err)


class DryRunTest(SpawnTestCase):
    def test_prints_the_command_without_running_it(self):
        self.use_fake_harness(
            argv=("/nonexistent/harness", self.review.PROMPT_PLACEHOLDER)
        )
        code, out, _ = run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch", "--dry-run")
        self.assertEqual(code, self.review.EXIT_OK)
        self.assertIn("/nonexistent/harness", out)


class RunReviewerTest(SpawnTestCase):
    def test_harness_output_is_returned(self):
        self.set_env(FAKE_HARNESS_MODE="echo", FAKE_HARNESS_OUTPUT=GRADED_REVIEW)
        run = self.review.run_reviewer(self.fake_reviewer, "prompt", timeout=30)
        self.assertEqual(run.status, self.review.STATUS_OK)
        self.assertEqual(run.text, LONG_ENOUGH_REVIEW)
        self.assertEqual(run.returncode, 0)

    def test_exit_code_is_captured(self):
        self.set_env(FAKE_HARNESS_MODE="nonzero")
        run = self.review.run_reviewer(self.fake_reviewer, "prompt", timeout=30)
        self.assertEqual(run.returncode, 3)

    def test_harness_family_is_recorded(self):
        self.set_env(FAKE_HARNESS_MODE="echo")
        run = self.review.run_reviewer(self.fake_reviewer, "prompt", timeout=30)
        self.assertEqual(run.family, "plain")

    def test_missing_harness_binary_is_reported_not_raised(self):
        self.use_fake_harness(
            argv=("/nonexistent/harness", self.review.PROMPT_PLACEHOLDER)
        )
        run = self.review.run_reviewer(self.fake_reviewer, "prompt", timeout=30)
        self.assertEqual(run.status, self.review.STATUS_HARNESS_MISSING)
        self.assertIn("/nonexistent/harness", run.notice)

    def test_a_harness_that_is_not_executable_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            unrunnable = pathlib.Path(tmp) / "harness"
            unrunnable.write_text("#!/bin/sh\necho hi\n")
            unrunnable.chmod(0o644)
            self.use_fake_harness(
                argv=(str(unrunnable), self.review.PROMPT_PLACEHOLDER)
            )
            run = self.review.run_reviewer(self.fake_reviewer, "prompt", timeout=30)
            self.assertEqual(run.status, self.review.STATUS_HARNESS_MISSING)

    def test_timeout_kills_the_harness_and_its_grandchild(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = pathlib.Path(tmp) / "grandchild.pid"
            self.set_env(FAKE_HARNESS_MODE="hang", FAKE_HARNESS_PIDFILE=str(pidfile))

            run = self.review.run_reviewer(self.fake_reviewer, "prompt", timeout=1)

            self.assertEqual(run.status, self.review.STATUS_TIMEOUT)
            self.assertIn("timed out after 1s", run.notice)
            grandchild = wait_for_pidfile(pidfile)
            self.assertTrue(
                wait_for_exit(grandchild),
                f"grandchild {grandchild} survived the timeout kill",
            )

    def test_a_detached_descendant_does_not_hang_the_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = pathlib.Path(tmp) / "grandchild.pid"
            self.set_env(
                FAKE_HARNESS_MODE="detached", FAKE_HARNESS_PIDFILE=str(pidfile)
            )

            started = time.monotonic()
            run = self.review.run_reviewer(self.fake_reviewer, "prompt", timeout=1)
            elapsed = time.monotonic() - started

            survivor = wait_for_pidfile(pidfile)
            self.addCleanup(self.kill_if_alive, survivor)

            self.assertEqual(run.status, self.review.STATUS_TIMEOUT)
            self.assertLess(
                elapsed,
                1 + self.review.DRAIN_TIMEOUT_SECONDS + 5,
                "a descendant that escaped the process group blocked the drain",
            )

    def kill_if_alive(self, pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def test_child_environment_marks_a_review_as_active(self):
        self.set_env(FAKE_HARNESS_MODE="env")
        run = self.review.run_reviewer(self.fake_reviewer, "prompt", timeout=30)
        self.assertIn("REVIEW_ACTIVE=1", run.text)


class MainOutputTest(SpawnTestCase):
    def test_review_text_is_printed_to_stdout(self):
        self.set_env(FAKE_HARNESS_MODE="echo", FAKE_HARNESS_OUTPUT=GRADED_REVIEW)
        code, out, _ = run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")
        self.assertEqual(code, self.review.EXIT_OK)
        self.assertIn(LONG_ENOUGH_REVIEW, out)

    def test_a_failed_reviewer_reports_on_stderr_and_exits_all_failed(self):
        self.use_fake_harness(
            argv=("/nonexistent/harness", self.review.PROMPT_PLACEHOLDER)
        )
        code, out, err = run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")
        self.assertEqual(code, self.review.EXIT_ALL_FAILED)
        self.assertEqual(out, "")
        self.assertIn("harness_missing", err)


class RecursionGuardTest(SpawnTestCase):
    def test_refuses_to_run_inside_a_review(self):
        self.use_fake_harness(
            argv=("/nonexistent/harness", self.review.PROMPT_PLACEHOLDER)
        )
        self.set_env(REVIEW_ACTIVE="1")
        code, _, err = run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")
        self.assertEqual(code, self.review.EXIT_RECURSION)
        self.assertIn("refusing to run inside a review", err)

    def test_guard_is_checked_before_anything_is_spawned(self):
        self.set_env(FAKE_HARNESS_MODE="echo", REVIEW_ACTIVE="1")
        code, out, _ = run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")
        self.assertEqual(code, self.review.EXIT_RECURSION)
        self.assertEqual(out, "")




class EndToEndTest(SpawnTestCase):
    def test_dry_run_of_the_real_script_shows_the_configured_harness(self):
        environment = dict(os.environ)
        environment.pop("REVIEW_ACTIVE", None)
        result = subprocess.run(
            [str(review_module.SCRIPT), "gpt-5.6", PROJECT, GOAL, "the branch", "--dry-run"],
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("claude", result.stdout)
        self.assertIn("--permission-mode", result.stdout)


if __name__ == "__main__":
    unittest.main()
