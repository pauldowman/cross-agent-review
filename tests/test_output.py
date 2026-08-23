import json
import unittest

import review_module
from test_spawn import GRADED_REVIEW, LONG_ENOUGH_REVIEW, SpawnTestCase, run_main


def claude_envelope(**fields):
    return json.dumps({"result": GRADED_REVIEW, "is_error": False, **fields})


class ExtractClaudeTest(unittest.TestCase):
    def setUp(self):
        self.review = review_module.load()

    def test_result_field_is_the_review(self):
        extracted = self.review.extract_claude(claude_envelope())
        self.assertEqual(extracted.text, GRADED_REVIEW)
        self.assertIsNone(extracted.error)

    def test_cost_is_captured(self):
        extracted = self.review.extract_claude(claude_envelope(total_cost_usd=0.0412))
        self.assertEqual(extracted.cost_usd, 0.0412)

    def test_absent_cost_is_none(self):
        self.assertIsNone(self.review.extract_claude(claude_envelope()).cost_usd)

    def test_an_error_envelope_is_not_a_review_even_with_a_populated_result(self):
        stdout = json.dumps(
            {
                "result": "Claude ran out of turns before finishing",
                "is_error": True,
                "subtype": "error_max_turns",
            }
        )
        extracted = self.review.extract_claude(stdout)
        self.assertEqual(extracted.text, "")
        self.assertIn("error_max_turns", extracted.error)

    def test_non_json_output_is_an_error(self):
        extracted = self.review.extract_claude("Error: something went wrong")
        self.assertIn("did not return JSON", extracted.error)

    def test_json_that_is_not_an_object_is_an_error(self):
        extracted = self.review.extract_claude("[1, 2, 3]")
        self.assertIn("unexpected JSON", extracted.error)

    def test_a_non_text_result_is_an_error_not_a_crash(self):
        for result in ({"text": "hi"}, ["a", "b"], 7):
            with self.subTest(result=result):
                stdout = json.dumps({"result": result, "is_error": False})
                extracted = self.review.extract_claude(stdout)
                self.assertEqual(extracted.text, "")
                self.assertIn("result", extracted.error)

    def test_a_non_text_result_classifies_rather_than_raising(self):
        stdout = json.dumps({"result": {"text": "hi"}, "is_error": False})
        extracted = self.review.extract_claude(stdout)
        status, _ = self.review.classify(extracted, 0)
        self.assertEqual(status, self.review.STATUS_HARNESS_ERROR)

    def test_the_unparsable_output_is_kept_for_diagnosis(self):
        extracted = self.review.extract_claude("Segmentation fault")
        self.assertIn("Segmentation fault", extracted.error)

    def test_cost_survives_an_error_envelope(self):
        stdout = json.dumps(
            {"result": "out of credit", "is_error": True, "total_cost_usd": 0.02}
        )
        self.assertEqual(self.review.extract_claude(stdout).cost_usd, 0.02)


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.review = review_module.load()

    def classify(self, text, returncode=0, error=None, cost=None):
        extracted = self.review.Extracted(text, cost, error)
        return self.review.classify(extracted, returncode)[0]

    def test_a_real_review_is_ok(self):
        self.assertEqual(self.classify(LONG_ENOUGH_REVIEW), self.review.STATUS_OK)

    def test_empty_output_is_empty_output(self):
        self.assertEqual(self.classify(""), self.review.STATUS_EMPTY_OUTPUT)

    def test_whitespace_only_output_is_empty_output(self):
        self.assertEqual(self.classify("   \n  "), self.review.STATUS_EMPTY_OUTPUT)

    def test_implausibly_short_output_is_empty_output(self):
        self.assertEqual(self.classify("ok"), self.review.STATUS_EMPTY_OUTPUT)

    def test_a_non_zero_exit_is_nonzero_exit(self):
        self.assertEqual(
            self.classify(LONG_ENOUGH_REVIEW, returncode=3),
            self.review.STATUS_NONZERO_EXIT,
        )

    def test_a_self_reported_error_at_exit_zero_is_a_harness_error(self):
        self.assertEqual(
            self.classify("", returncode=0, error="claude reported an error"),
            self.review.STATUS_HARNESS_ERROR,
        )

    def test_a_non_zero_exit_outranks_a_self_reported_error(self):
        status, notice = self.review.classify(
            self.review.Extracted("", None, "claude did not return JSON"), 1
        )
        self.assertEqual(status, self.review.STATUS_NONZERO_EXIT)
        self.assertIn("harness exited 1", notice)
        self.assertIn("did not return JSON", notice)


class ClassifiedRunTest(SpawnTestCase):
    """The classification a full run_reviewer() call produces."""

    def use_claude_family(self):
        self.use_fake_harness(family="claude")

    def test_a_claude_envelope_becomes_a_review(self):
        self.use_claude_family()
        self.set_env(FAKE_HARNESS_MODE="echo", FAKE_HARNESS_OUTPUT=claude_envelope())
        run = self.review.run_reviewer("fake", "prompt", timeout=30)
        self.assertEqual(run.status, self.review.STATUS_OK)
        self.assertEqual(run.text, LONG_ENOUGH_REVIEW)

    def test_a_claude_error_envelope_is_not_returned_as_a_review(self):
        self.use_claude_family()
        self.set_env(
            FAKE_HARNESS_MODE="echo",
            FAKE_HARNESS_OUTPUT=json.dumps(
                {"result": "credit balance too low", "is_error": True}
            ),
        )
        run = self.review.run_reviewer("fake", "prompt", timeout=30)
        self.assertEqual(run.status, self.review.STATUS_HARNESS_ERROR)
        self.assertEqual(run.text, "")
        self.assertIn("credit balance too low", run.notice)

    def test_cost_reaches_the_run(self):
        self.use_claude_family()
        self.set_env(
            FAKE_HARNESS_MODE="echo",
            FAKE_HARNESS_OUTPUT=claude_envelope(total_cost_usd=0.0412),
        )
        run = self.review.run_reviewer("fake", "prompt", timeout=30)
        self.assertEqual(run.cost_usd, 0.0412)

    def test_a_harness_without_cost_reporting_records_none(self):
        self.set_env(FAKE_HARNESS_MODE="echo", FAKE_HARNESS_OUTPUT=LONG_ENOUGH_REVIEW)
        run = self.review.run_reviewer("fake", "prompt", timeout=30)
        self.assertIsNone(run.cost_usd)

    def test_empty_harness_output_is_not_a_review(self):
        self.set_env(FAKE_HARNESS_MODE="empty")
        run = self.review.run_reviewer("fake", "prompt", timeout=30)
        self.assertEqual(run.status, self.review.STATUS_EMPTY_OUTPUT)

    def test_a_non_zero_exit_is_not_a_review(self):
        self.set_env(FAKE_HARNESS_MODE="nonzero")
        run = self.review.run_reviewer("fake", "prompt", timeout=30)
        self.assertEqual(run.status, self.review.STATUS_NONZERO_EXIT)
        self.assertIn("harness failed", run.stderr)

    def test_a_failed_classification_stops_main_from_printing_a_review(self):
        self.set_env(FAKE_HARNESS_MODE="empty")
        code, out, err = run_main(self.review, "gpt-5.6", "the branch")
        self.assertEqual(code, self.review.EXIT_ALL_FAILED)
        self.assertEqual(out, "")
        self.assertIn("empty_output", err)


if __name__ == "__main__":
    unittest.main()
