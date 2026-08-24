import pathlib
import tempfile
import unittest

import review_module
from test_grade import reply
from test_spawn import SpawnTestCase, run_main

REVIEW_BODY = "The codex reviewer found an unhandled error path in the parser."

# Each harness states its permission posture explicitly rather than
# inheriting whatever the user's config last set.
PERMISSION_FLAGS = {
    "claude": ("--permission-mode", "plan"),
    "codex": ("-s", "danger-full-access"),
}


class ConfiguredHarnessTest(unittest.TestCase):
    """The shipped routing table, not the fixtures other tests install."""

    def setUp(self):
        self.review = review_module.load()

    def test_every_configured_reviewer_has_a_harness(self):
        for author, reviewers in self.review.REVIEWERS.items():
            for reviewer in reviewers:
                with self.subTest(author=author, reviewer=reviewer):
                    self.assertIn(reviewer, self.review.HARNESS)

    def test_every_known_author_has_reviewers(self):
        for author in self.review.KNOWN_AUTHORS:
            with self.subTest(author=author):
                self.assertTrue(self.review.REVIEWERS.get(author))

    def test_no_author_is_routed_to_itself(self):
        for author, reviewers in self.review.REVIEWERS.items():
            with self.subTest(author=author):
                self.assertNotIn(author, reviewers)

    def test_every_author_has_a_declared_harness_family(self):
        self.assertEqual(
            set(self.review.AUTHOR_FAMILY), set(self.review.KNOWN_AUTHORS)
        )

    def test_an_author_is_never_reviewed_only_by_its_own_harness(self):
        for author, reviewers in self.review.REVIEWERS.items():
            with self.subTest(author=author):
                own = self.review.AUTHOR_FAMILY[author]
                families = {self.review.HARNESS[r].family for r in reviewers}
                self.assertTrue(
                    families - {own}, f"{author} is only reviewed by {own} itself"
                )

    def test_every_harness_pins_its_model_explicitly(self):
        for reviewer, harness in self.review.HARNESS.items():
            with self.subTest(reviewer=reviewer):
                pinned = [f for f in ("--model", "-m") if f in harness.argv]
                self.assertTrue(pinned, f"{reviewer} does not pin a model")
                model = harness.argv[harness.argv.index(pinned[0]) + 1]
                self.assertIn(
                    model, reviewer, f"{reviewer} runs {model}, which its key hides"
                )

    def test_every_harness_states_its_permissions_explicitly(self):
        for reviewer, harness in self.review.HARNESS.items():
            with self.subTest(reviewer=reviewer):
                flag, value = PERMISSION_FLAGS[harness.family]
                self.assertIn(flag, harness.argv)
                self.assertEqual(harness.argv[harness.argv.index(flag) + 1], value)

    def test_every_harness_family_has_an_extractor(self):
        for reviewer, harness in self.review.HARNESS.items():
            with self.subTest(reviewer=reviewer):
                self.assertIn(harness.family, self.review.EXTRACTORS)


class DryRunOfConfiguredReviewersTest(unittest.TestCase):
    """--dry-run must work for the reviewers that actually ship."""

    def setUp(self):
        self.review = review_module.load()

    def test_every_author_can_be_dry_run(self):
        pristine = self.review
        for author in pristine.KNOWN_AUTHORS:
            with self.subTest(author=author):
                code, out, _ = run_main(pristine, author, "the branch", "--dry-run")
                self.assertEqual(code, pristine.EXIT_OK)
                for reviewer in pristine.REVIEWERS[author]:
                    self.assertIn(reviewer, out)

    def test_the_codex_command_shows_where_its_output_goes(self):
        _, out, _ = run_main(self.review, "opus5", "the branch", "--dry-run")
        self.assertIn(f"-o '{self.review.OUTPUT_PLACEHOLDER}'", out)

    def test_a_dry_run_creates_no_output_files(self):
        before = set(pathlib.Path(tempfile.gettempdir()).glob("review-*"))
        run_main(self.review, "opus5", "the branch", "--dry-run")
        self.assertEqual(
            set(pathlib.Path(tempfile.gettempdir()).glob("review-*")) - before, set()
        )


class ExtractCodexTest(unittest.TestCase):
    def setUp(self):
        self.review = review_module.load()

    def test_the_reply_comes_from_the_output_file_not_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "out.txt"
            path.write_text(reply("B", REVIEW_BODY))

            banner = "OpenAI Codex v0.149.0\n--------\ntokens used\n5,683\n"
            extracted = self.review.extract_codex(banner, path)

            self.assertIn(REVIEW_BODY, extracted.text)
            self.assertNotIn("tokens used", extracted.text)
            self.assertIsNone(extracted.error)

    def test_a_partial_multibyte_write_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "partial.txt"
            path.write_bytes("a review cut off mid-character \u2014".encode()[:-1])

            extracted = self.review.extract_codex("", path)

            self.assertIsNone(extracted.error)
            self.assertIn("cut off", extracted.text)

    def test_a_missing_output_file_is_an_error_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "never-written.txt"
            extracted = self.review.extract_codex("", path)

            self.assertEqual(extracted.text, "")
            self.assertIn("no output file", extracted.error)

    def test_no_output_path_at_all_is_an_error(self):
        extracted = self.review.extract_codex("some stdout", None)
        self.assertIn("not given an output file", extracted.error)

    def test_codex_reports_no_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "out.txt"
            path.write_text(reply("A", REVIEW_BODY))
            self.assertIsNone(self.review.extract_codex("", path).cost_usd)


class OutputFileTest(SpawnTestCase):
    def test_a_harness_without_an_output_placeholder_gets_no_file(self):
        self.assertIsNone(self.review.allocate_output_file("claude-opus-5"))

    def test_a_harness_with_an_output_placeholder_gets_its_own_file(self):
        first = self.review.allocate_output_file(f"codex-{self.review.CODEX_MODEL}")
        second = self.review.allocate_output_file(f"codex-{self.review.CODEX_MODEL}")
        self.addCleanup(first.unlink, True)
        self.addCleanup(second.unlink, True)

        self.assertNotEqual(first, second)
        self.assertTrue(first.exists())

    def test_the_output_file_is_removed_after_the_run(self):
        seen = []
        original = self.review.allocate_output_file

        def remember(reviewer):
            path = original(reviewer)
            seen.append(path)
            return path

        self.review.allocate_output_file = remember
        self.review.HARNESS["writer"] = self.review.Harness(
            "codex",
            (
                "/bin/sh",
                "-c",
                'printf %s "reviewed" > "$1"',
                self.review.PROMPT_PLACEHOLDER,
                self.review.OUTPUT_PLACEHOLDER,
            ),
        )

        self.review.run_reviewer("writer", "prompt", timeout=30)

        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0].exists(), "the run left its output file behind")

    def test_the_prompt_reaches_the_harness_and_the_reply_comes_back(self):
        self.review.HARNESS["writer"] = self.review.Harness(
            "codex",
            (
                "/bin/sh",
                "-c",
                'printf %s "$1" > "$2"',
                "sh",
                self.review.PROMPT_PLACEHOLDER,
                self.review.OUTPUT_PLACEHOLDER,
            ),
        )

        run = self.review.run_reviewer(
            "writer", reply("C", REVIEW_BODY), timeout=30
        )

        self.assertEqual(run.status, self.review.STATUS_OK)
        self.assertEqual(run.grade, "C")
        self.assertEqual(run.text, REVIEW_BODY)


if __name__ == "__main__":
    unittest.main()
