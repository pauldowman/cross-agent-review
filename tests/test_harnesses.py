import json
import pathlib
import tempfile
import unittest

import review_module
from test_grade import reply
from test_spawn import GOAL, PROJECT, SpawnTestCase, run_main

REVIEW_BODY = "The codex reviewer found an unhandled error path in the parser."

# Each harness states its permission posture explicitly rather than
# inheriting whatever the user's config last set.
PERMISSION_FLAGS = {
    "claude": ("--permission-mode", "plan"),
    "codex": ("-s", "danger-full-access"),
    "opencode": ("--agent", "plan"),
}


class ConfiguredHarnessTest(unittest.TestCase):
    """The shipped routing table, not the fixtures other tests install."""

    def setUp(self):
        self.review = review_module.load()

    def test_every_harness_pins_its_model_explicitly(self):
        model = "model-selected-by-config"
        for family, builder in self.review.HARNESSES.items():
            with self.subTest(harness=family):
                harness = builder(model)
                pinned = [f for f in ("--model", "-m") if f in harness.argv]
                self.assertTrue(pinned, f"{family} does not pin a model")
                selected = harness.argv[harness.argv.index(pinned[0]) + 1]
                self.assertEqual(selected, model)

    def test_every_harness_states_its_permissions_explicitly(self):
        for family, builder in self.review.HARNESSES.items():
            with self.subTest(harness=family):
                harness = builder("model-under-test")
                flag, value = PERMISSION_FLAGS[harness.family]
                self.assertIn(flag, harness.argv)
                self.assertEqual(harness.argv[harness.argv.index(flag) + 1], value)

    def test_every_harness_family_has_an_extractor(self):
        for family in self.review.HARNESSES:
            with self.subTest(harness=family):
                self.assertIn(family, self.review.EXTRACTORS)


class DryRunOfConfiguredReviewersTest(unittest.TestCase):
    """--dry-run must work for the reviewers that actually ship."""

    def setUp(self):
        self.review = review_module.load()

    def test_any_author_can_be_dry_run(self):
        for author in ("claude-opus-5", "gpt-5.6-sol", "some-unknown-model"):
            with self.subTest(author=author):
                code, out, _ = run_main(
                    self.review, author, PROJECT, GOAL, "the branch", "--dry-run"
                )
                self.assertEqual(code, self.review.EXIT_OK)
                self.assertTrue(out)

    def test_the_codex_command_shows_where_its_output_goes(self):
        _, out, _ = run_main(
            self.review, "claude-opus-5", PROJECT, GOAL, "the branch", "--dry-run"
        )
        self.assertIn(f"-o '{self.review.OUTPUT_PLACEHOLDER}'", out)

    def test_the_opencode_command_requests_json_events(self):
        _, out, _ = run_main(
            self.review, "claude-opus-5", PROJECT, GOAL, "the branch", "--dry-run"
        )
        self.assertIn("opencode/x-preview-f-free via opencode", out)
        self.assertIn("--format json", out)

    def test_a_dry_run_creates_no_output_files(self):
        before = set(pathlib.Path(tempfile.gettempdir()).glob("review-*"))
        run_main(
            self.review, "claude-opus-5", PROJECT, GOAL, "the branch", "--dry-run"
        )
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


class ExtractOpenCodeTest(unittest.TestCase):
    def setUp(self):
        self.review = review_module.load()

    def event(self, event_type, **fields):
        return json.dumps({"type": event_type, **fields})

    def text_event(self, text):
        return self.event("text", part={"type": "text", "text": text})

    def test_the_last_text_event_is_the_review(self):
        stdout = "\n".join(
            (
                self.text_event("I will inspect the diff."),
                self.event("tool_use", part={"tool": "bash"}),
                self.text_event(REVIEW_BODY),
            )
        )
        extracted = self.review.extract_opencode(stdout)
        self.assertEqual(extracted.text, REVIEW_BODY)
        self.assertIsNone(extracted.error)

    def test_an_error_event_is_not_a_review(self):
        stdout = self.event(
            "error", error={"data": {"message": "Unexpected server error"}}
        )
        extracted = self.review.extract_opencode(stdout)
        self.assertEqual(extracted.text, "")
        self.assertIn("Unexpected server error", extracted.error)

    def test_invalid_json_is_an_error(self):
        extracted = self.review.extract_opencode("not json")
        self.assertIn("invalid JSON", extracted.error)

    def test_a_stream_without_text_is_an_error(self):
        extracted = self.review.extract_opencode(
            self.event("tool_use", part={"tool": "bash"})
        )
        self.assertIn("no text event", extracted.error)

    def test_a_malformed_text_event_is_an_error(self):
        extracted = self.review.extract_opencode(
            self.event("text", part={"type": "text", "text": 42})
        )
        self.assertIn("malformed text event", extracted.error)

    def test_opencode_reports_no_cost(self):
        self.assertIsNone(self.review.extract_opencode(self.text_event(REVIEW_BODY)).cost_usd)


class OutputFileTest(SpawnTestCase):
    def test_a_harness_without_an_output_placeholder_gets_no_file(self):
        reviewer = self.review.Reviewer("claude", "model-under-test")
        self.assertIsNone(self.review.allocate_output_file(reviewer))

    def test_a_harness_with_an_output_placeholder_gets_its_own_file(self):
        reviewer = self.review.Reviewer("codex", "model-under-test")
        first = self.review.allocate_output_file(reviewer)
        second = self.review.allocate_output_file(reviewer)
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
        reviewer = self.install_harness(
            "writer",
            "codex",
            (
                "/bin/sh",
                "-c",
                'printf %s "reviewed" > "$1"',
                self.review.PROMPT_PLACEHOLDER,
                self.review.OUTPUT_PLACEHOLDER,
            ),
        )

        self.review.run_reviewer(reviewer, "prompt", timeout=30)

        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0].exists(), "the run left its output file behind")

    def test_the_prompt_reaches_the_harness_and_the_reply_comes_back(self):
        reviewer = self.install_harness(
            "writer",
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
            reviewer, reply("C", REVIEW_BODY), timeout=30
        )

        self.assertEqual(run.status, self.review.STATUS_OK)
        self.assertEqual(run.grade, "C")
        self.assertEqual(run.text, REVIEW_BODY)


if __name__ == "__main__":
    unittest.main()
