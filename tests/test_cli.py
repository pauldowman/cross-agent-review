import contextlib
import io
import subprocess
import unittest

import review_module

review = review_module.load()

PROJECT = "reviewer"
GOAL = "add a review tool"


def run(*argv):
    """Run main() with argv, returning (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = review.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class AnyAuthorTest(unittest.TestCase):
    """A model names itself; the tool records whatever it is told."""

    def test_an_unrecognized_model_name_is_accepted(self):
        code, out, _ = run(
            "some-model-nobody-configured-7", PROJECT, GOAL, "x", "--dry-run"
        )
        self.assertEqual(code, review.EXIT_OK)
        self.assertTrue(out)

    def test_a_model_name_with_unusual_characters_is_accepted(self):
        code, _, _ = run("vendor/model:2026-08@preview", PROJECT, GOAL, "x", "--dry-run")
        self.assertEqual(code, review.EXIT_OK)

    def test_surrounding_whitespace_is_trimmed(self):
        code, _, _ = run("  claude-opus-5  ", PROJECT, GOAL, "x", "--dry-run")
        self.assertEqual(code, review.EXIT_OK)

    def test_the_help_text_asks_for_a_precise_versioned_name(self):
        help_text = review.build_parser().format_help()
        self.assertIn("precise model name", help_text)
        self.assertIn("including its version", help_text)


class UsageTest(unittest.TestCase):
    def assert_usage_error(self, *argv):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as raised:
                review.main(list(argv))
        self.assertEqual(raised.exception.code, 2)
        return err.getvalue()

    def test_every_positional_is_required(self):
        for argv in (
            [],
            ["claude-opus-5"],
            ["claude-opus-5", PROJECT],
            ["claude-opus-5", PROJECT, GOAL],
        ):
            with self.subTest(argv=argv):
                self.assertIn("required", self.assert_usage_error(*argv))

    def test_the_error_names_all_four_arguments(self):
        message = self.assert_usage_error()
        for name in ("author", "project", "goal", "description"):
            self.assertIn(name, message)


class ExecutableTest(unittest.TestCase):
    def test_running_the_script_reports_a_usage_error_without_arguments(self):
        result = subprocess.run(
            [str(review_module.SCRIPT)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("required", result.stderr)

    def test_the_script_is_named_for_the_tool(self):
        self.assertEqual(review_module.SCRIPT.name, review.TOOL_NAME)


if __name__ == "__main__":
    unittest.main()
