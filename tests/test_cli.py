import contextlib
import io
import subprocess
import unittest

import review_module

review = review_module.load()
PROJECT = "reviewer"


def run(*argv):
    """Run main() with argv, returning (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = review.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class KnownAuthorsTest(unittest.TestCase):
    def test_lists_every_author_with_its_aliases(self):
        code, out, _ = run("--known-authors")
        self.assertEqual(code, 0)
        for author, aliases in review.KNOWN_AUTHORS.items():
            self.assertIn(author, out)
            for alias in aliases:
                self.assertIn(alias, out)

    def test_takes_precedence_over_a_review_request(self):
        code, out, _ = run("opus5", PROJECT, "the uncommitted changes", "--known-authors")
        self.assertEqual(code, 0)
        self.assertIn("Known authors:", out)


class AuthorValidationTest(unittest.TestCase):
    def test_unknown_author_points_at_known_authors(self):
        code, _, err = run("gpt-2", PROJECT, "the uncommitted changes")
        self.assertEqual(code, 2)
        self.assertIn("gpt-2", err)
        self.assertIn("review --known-authors", err)

    def test_canonical_author_is_accepted(self):
        code, _, _ = run("gpt-5.6", PROJECT, "the uncommitted changes", "--dry-run")
        self.assertEqual(code, 0)

    def test_alias_resolves_to_canonical_author(self):
        self.assertEqual(review.resolve_author("opus5"), "claude-opus-5")
        code, _, _ = run("gpt5", PROJECT, "the uncommitted changes", "--dry-run")
        self.assertEqual(code, 0)

    def test_author_matching_ignores_case_and_surrounding_space(self):
        self.assertEqual(review.resolve_author("  Claude-Opus-5 "), "claude-opus-5")

    def test_unknown_author_resolves_to_none(self):
        self.assertIsNone(review.resolve_author("gpt-2"))


class AliasIndexTest(unittest.TestCase):
    def test_every_configured_name_and_alias_resolves(self):
        for author, aliases in review.KNOWN_AUTHORS.items():
            self.assertEqual(review.resolve_author(author), author)
            for alias in aliases:
                self.assertEqual(review.resolve_author(alias), author)

    def test_alias_may_not_shadow_another_authors_canonical_name(self):
        with self.assertRaises(ValueError):
            review.build_alias_index({"gpt-5.6": ("codex",), "codex": ()})

    def test_duplicate_alias_across_authors_is_rejected(self):
        with self.assertRaises(ValueError):
            review.build_alias_index({"gpt-5.6": ("gpt",), "glm-5.2": ("gpt",)})


class UsageTest(unittest.TestCase):
    def test_missing_description_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                review.main(["claude-opus-5", PROJECT])
        self.assertEqual(raised.exception.code, 2)

    def test_no_arguments_at_all_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                review.main([])
        self.assertEqual(raised.exception.code, 2)


class ExecutableTest(unittest.TestCase):
    def test_running_the_script_exits_2_on_an_unknown_author(self):
        result = subprocess.run(
            [str(review_module.SCRIPT), "gpt-2", PROJECT, "the uncommitted changes"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("review --known-authors", result.stderr)


if __name__ == "__main__":
    unittest.main()
