import os
import pathlib
import tempfile
import unittest
from unittest import mock

import review_module
from test_spawn import GOAL, PROJECT, run_main



class ConfigPathTest(unittest.TestCase):
    def setUp(self):
        self.review = review_module.load()

    def test_defaults_to_the_xdg_config_directory(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(self.review.CONFIG_ENV_VAR, None)
            os.environ.pop("XDG_CONFIG_HOME", None)
            self.assertEqual(
                self.review.config_path(),
                pathlib.Path.home() / ".config/cross-agent-review/reviewers.toml",
            )

    def test_xdg_config_home_is_honored(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/somewhere/config"}):
            os.environ.pop(self.review.CONFIG_ENV_VAR, None)
            self.assertEqual(
                self.review.config_path(),
                pathlib.Path("/somewhere/config/cross-agent-review/reviewers.toml"),
            )

    def test_the_explicit_override_wins(self):
        with mock.patch.dict(os.environ, {self.review.CONFIG_ENV_VAR: "/tmp/r.toml"}):
            self.assertEqual(self.review.config_path(), pathlib.Path("/tmp/r.toml"))


class LoadRulesTestCase(unittest.TestCase):
    def setUp(self):
        self.review = review_module.load()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = pathlib.Path(self.directory.name) / "reviewers.toml"

    def write(self, text):
        self.path.write_text(text)
        return self.path

    def error_from(self, text):
        with self.assertRaises(self.review.ConfigurationError) as raised:
            self.review.load_rules(self.write(text))
        return str(raised.exception)


class MissingConfigTest(LoadRulesTestCase):
    def test_a_missing_config_explains_where_it_goes_and_what_it_holds(self):
        with self.assertRaises(self.review.ConfigurationError) as raised:
            self.review.load_rules(self.path)

        message = str(raised.exception)
        self.assertIn(str(self.path), message, "the message must name the location")
        self.assertIn("[[rule]]", message, "the message must show the format")
        self.assertIn("pattern", message)
        self.assertIn("reviewers", message)

    def test_the_example_in_the_message_is_itself_valid(self):
        with self.assertRaises(self.review.ConfigurationError) as raised:
            self.review.load_rules(self.path)

        example = str(raised.exception).split("<<'EOF'\n", 1)[1].rsplit("EOF", 1)[0]
        self.assertTrue(self.review.load_rules(self.write(example)))

    def test_a_missing_config_is_a_usage_error_not_a_traceback(self):
        with mock.patch.dict(os.environ, {self.review.CONFIG_ENV_VAR: str(self.path)}):
            code, _, err = run_main(
                self.review, "claude-opus-5", PROJECT, GOAL, "the branch"
            )
        self.assertEqual(code, self.review.EXIT_USAGE)
        self.assertIn("no reviewer configuration", err)


class MalformedConfigTest(LoadRulesTestCase):
    def test_invalid_toml_is_reported_with_the_path(self):
        message = self.error_from("[[rule]\npattern =")
        self.assertIn("not valid TOML", message)
        self.assertIn(str(self.path), message)

    def test_a_config_with_no_rules_is_rejected(self):
        self.assertIn("no [[rule]] entries", self.error_from("# nothing here\n"))

    def test_a_rule_without_a_pattern_is_rejected(self):
        message = self.error_from('[[rule]]\nreviewers = ["claude-opus-5"]\n')
        self.assertIn("`pattern`", message)
        self.assertIn("rule 1", message)

    def test_a_rule_without_reviewers_is_rejected(self):
        self.assertIn("`reviewers`", self.error_from('[[rule]]\npattern = "."\n'))

    def test_an_empty_reviewer_list_is_rejected(self):
        self.assertIn(
            "`reviewers`", self.error_from('[[rule]]\npattern = "."\nreviewers = []\n')
        )

    def test_an_unknown_reviewer_is_rejected_and_the_known_ones_listed(self):
        message = self.error_from('[[rule]]\npattern = "."\nreviewers = ["gpt-9"]\n')
        self.assertIn("gpt-9", message)
        self.assertIn("claude-opus-5", message, "the message must list valid names")

    def test_an_invalid_regex_is_rejected(self):
        message = self.error_from(
            '[[rule]]\npattern = "([unclosed"\nreviewers = ["claude-opus-5"]\n'
        )
        self.assertIn("invalid pattern", message)

    def test_the_offending_rule_is_identified_by_position(self):
        message = self.error_from(
            '[[rule]]\npattern = "."\nreviewers = ["claude-opus-5"]\n\n'
            '[[rule]]\nreviewers = ["claude-opus-5"]\n'
        )
        self.assertIn("rule 2", message)


class MatchingTest(LoadRulesTestCase):
    def rules(self, text):
        return self.review.load_rules(self.write(text))

    def test_the_pattern_matches_anywhere_in_the_model_name(self):
        rules = self.rules('[[rule]]\npattern = "opus"\nreviewers = ["claude-opus-5"]\n')
        self.assertEqual(
            self.review.reviewers_for("anthropic/claude-opus-5", rules),
            ("claude-opus-5",),
        )

    def test_matching_ignores_case(self):
        rules = self.rules('[[rule]]\npattern = "^GPT"\nreviewers = ["claude-opus-5"]\n')
        self.assertEqual(
            self.review.reviewers_for("gpt-5.6-sol", rules), ("claude-opus-5",)
        )

    def test_the_first_matching_rule_wins(self):
        rules = self.rules(
            '[[rule]]\npattern = "^claude-opus"\nreviewers = ["claude-sonnet-5"]\n\n'
            '[[rule]]\npattern = "."\nreviewers = ["claude-opus-5"]\n'
        )
        self.assertEqual(
            self.review.reviewers_for("claude-opus-5", rules), ("claude-sonnet-5",)
        )
        self.assertEqual(
            self.review.reviewers_for("something-else", rules), ("claude-opus-5",)
        )

    def test_an_author_matching_no_rule_is_an_error_naming_the_model(self):
        rules = self.rules(
            '[[rule]]\npattern = "^claude"\nreviewers = ["claude-opus-5"]\n'
        )
        with self.assertRaises(self.review.ConfigurationError) as raised:
            self.review.reviewers_for("gpt-5.6-sol", rules)

        message = str(raised.exception)
        self.assertIn("gpt-5.6-sol", message)
        self.assertIn("catch-all", message)

    def test_no_matching_rule_is_a_usage_error_not_a_traceback(self):
        self.write('[[rule]]\npattern = "^claude"\nreviewers = ["claude-opus-5"]\n')
        with mock.patch.dict(os.environ, {self.review.CONFIG_ENV_VAR: str(self.path)}):
            code, _, err = run_main(self.review, "gpt-5.6-sol", PROJECT, GOAL, "x")
        self.assertEqual(code, self.review.EXIT_USAGE)
        self.assertIn("no rule", err)

    def test_every_reviewer_named_in_a_rule_is_used(self):
        rules = self.rules(
            '[[rule]]\npattern = "."\n'
            'reviewers = ["claude-opus-5", "claude-sonnet-5"]\n'
        )
        self.assertEqual(
            self.review.reviewers_for("anything", rules),
            ("claude-opus-5", "claude-sonnet-5"),
        )


class ShippedExampleTest(unittest.TestCase):
    """The example the tool prints must be usable as written."""

    def setUp(self):
        self.review = review_module.load()

    def test_the_example_parses_and_routes_every_shipped_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "reviewers.toml"
            path.write_text(self.review.EXAMPLE_CONFIG)
            rules = self.review.load_rules(path)

            for author in list(self.review.HARNESS) + ["some-unknown-model"]:
                with self.subTest(author=author):
                    self.assertTrue(self.review.reviewers_for(author, rules))

    def test_the_example_never_routes_a_model_to_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "reviewers.toml"
            path.write_text(self.review.EXAMPLE_CONFIG)
            rules = self.review.load_rules(path)

            for author in self.review.HARNESS:
                with self.subTest(author=author):
                    self.assertNotIn(author, self.review.reviewers_for(author, rules))


if __name__ == "__main__":
    unittest.main()
