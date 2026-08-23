import contextlib
import io
import json
import os
import pathlib
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

import review_module
from test_grade import reply
from test_spawn import LONG_ENOUGH_REVIEW, SpawnTestCase, run_main


def rows(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM reviews")]
    finally:
        connection.close()


class DatabasePathTest(unittest.TestCase):
    def setUp(self):
        self.review = review_module.load()

    def test_defaults_to_the_xdg_data_directory(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(self.review.DB_ENV_VAR, None)
            os.environ.pop("XDG_DATA_HOME", None)
            expected = pathlib.Path.home() / ".local/share/review/reviews.db"
            self.assertEqual(self.review.database_path(), expected)

    def test_xdg_data_home_is_honored(self):
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/somewhere/data"}):
            os.environ.pop(self.review.DB_ENV_VAR, None)
            self.assertEqual(
                self.review.database_path(),
                pathlib.Path("/somewhere/data/review/reviews.db"),
            )

    def test_the_explicit_override_wins(self):
        with mock.patch.dict(
            os.environ,
            {"XDG_DATA_HOME": "/somewhere/data", self.review.DB_ENV_VAR: "/tmp/x.db"},
        ):
            self.assertEqual(self.review.database_path(), pathlib.Path("/tmp/x.db"))


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.review = review_module.load()

    def test_the_database_and_its_parent_directory_are_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "nested" / "deeper" / "reviews.db"
            self.review.open_database(path).close()
            self.assertTrue(path.exists())

    def test_the_schema_version_is_stamped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "reviews.db"
            connection = self.review.open_database(path)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            connection.close()
            self.assertEqual(version, self.review.SCHEMA_VERSION)

    def test_opening_an_existing_database_does_not_destroy_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "reviews.db"
            connection = self.review.open_database(path)
            connection.execute(
                "INSERT INTO reviews (run_id, ts, author, reviewer, harness,"
                " description, cwd, status) VALUES ('r','t','a','v','h','d','c','ok')"
            )
            connection.commit()
            connection.close()

            self.review.open_database(path).close()
            self.assertEqual(len(rows(path)), 1)


class RunIdTest(SpawnTestCase):
    def test_every_reviewer_of_one_invocation_shares_a_run_id(self):
        connection = self.review.open_database(self.db_path)
        invocation = self.review.describe_invocation("gpt-5.6", "the branch")
        for reviewer in ("first", "second", "third"):
            self.review.record_run(
                connection,
                invocation,
                self.review.ReviewerRun(
                    reviewer=reviewer,
                    family="plain",
                    status=self.review.STATUS_OK,
                    text="a review",
                    notice="",
                    stderr="",
                    duration_s=1.0,
                ),
            )
        connection.close()

        recorded = rows(self.db_path)
        self.assertEqual(len(recorded), 3)
        self.assertEqual(len({row["run_id"] for row in recorded}), 1)
        self.assertEqual(
            sorted(row["reviewer"] for row in recorded), ["first", "second", "third"]
        )


class DatabaseFailureTest(SpawnTestCase):
    """A bookkeeping failure must never cost the author a paid-for review."""

    def test_an_unwritable_database_still_delivers_the_review(self):
        self.set_env(
            REVIEW_DB="/proc/nonexistent/reviews.db",
            FAKE_HARNESS_MODE="echo",
            FAKE_HARNESS_OUTPUT=reply("B"),
        )
        code, out, err = run_main(self.review, "gpt-5.6", "the branch")
        self.assertEqual(code, self.review.EXIT_OK)
        self.assertIn("retry loop", out)
        self.assertIn("not recording", err)

    def test_a_corrupt_database_still_delivers_the_review(self):
        self.db_path.write_text("this is not a sqlite database at all")
        self.set_env(FAKE_HARNESS_MODE="echo", FAKE_HARNESS_OUTPUT=reply("B"))

        code, out, _ = run_main(self.review, "gpt-5.6", "the branch")
        self.assertEqual(code, self.review.EXIT_OK)
        self.assertIn("retry loop", out)

    def test_a_database_from_a_future_schema_is_refused_not_rewritten(self):
        connection = self.review.open_database(self.db_path)
        connection.execute("PRAGMA user_version=99")
        connection.close()

        with contextlib.redirect_stderr(io.StringIO()) as warning:
            self.assertIsNone(self.review.open_database(self.db_path))
        self.assertIn("schema version 99", warning.getvalue())

        connection = sqlite3.connect(self.db_path)
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        connection.close()
        self.assertEqual(version, 99)

    def test_record_run_without_a_database_is_a_no_op(self):
        run = self.review.ReviewerRun(
            reviewer="fake",
            family="plain",
            status=self.review.STATUS_OK,
            text="a review",
            notice="",
            stderr="",
            duration_s=1.0,
        )
        invocation = self.review.describe_invocation("gpt-5.6", "the branch")
        self.assertFalse(self.review.record_run(None, invocation, run))


class RecordedRunTest(SpawnTestCase):
    def echo(self, output):
        self.set_env(FAKE_HARNESS_MODE="echo", FAKE_HARNESS_OUTPUT=output)

    def test_a_successful_review_is_recorded_in_full(self):
        self.echo(reply("B"))
        run_main(self.review, "gpt-5.6", "the uncommitted changes")

        (row,) = rows(self.db_path)
        self.assertEqual(row["author"], "gpt-5.6")
        self.assertEqual(row["reviewer"], "fake")
        self.assertEqual(row["harness"], "plain")
        self.assertEqual(row["description"], "the uncommitted changes")
        self.assertEqual(row["status"], self.review.STATUS_OK)
        self.assertEqual(row["grade"], "B")
        self.assertIn("retry loop", row["review_text"])
        self.assertEqual(row["cwd"], os.getcwd())
        self.assertTrue(row["run_id"])
        self.assertTrue(row["ts"])
        self.assertIsNotNone(row["duration_s"])

    def test_the_repository_position_is_recorded(self):
        self.echo(reply("A"))
        run_main(self.review, "gpt-5.6", "the branch")

        (row,) = rows(self.db_path)
        expected = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(row["branch"], expected)
        self.assertEqual(len(row["git_sha"]), 40)

    def test_a_run_outside_a_repository_records_no_branch_or_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = os.getcwd()
            os.chdir(tmp)
            self.addCleanup(os.chdir, original)

            self.echo(reply("A"))
            run_main(self.review, "gpt-5.6", "the branch")

            (row,) = rows(self.db_path)
            self.assertIsNone(row["branch"])
            self.assertIsNone(row["git_sha"])
            self.assertEqual(row["cwd"], os.getcwd())

    def test_the_grade_is_recorded_even_though_it_is_never_printed(self):
        self.echo(reply("D"))
        _, out, _ = run_main(self.review, "gpt-5.6", "the branch")

        (row,) = rows(self.db_path)
        self.assertEqual(row["grade"], "D")
        self.assertNotIn("D</grade>", out)

    def test_the_not_found_sentinel_is_recorded_as_its_grade(self):
        self.echo(reply("NA", "I could not find the branch you named anywhere."))
        run_main(self.review, "gpt-5.6", "a branch that does not exist")

        (row,) = rows(self.db_path)
        self.assertEqual(row["grade"], self.review.NOT_FOUND_GRADE)
        self.assertEqual(row["status"], self.review.STATUS_NOT_FOUND)

    def test_an_unparsable_reply_is_recorded_with_no_grade(self):
        self.echo(LONG_ENOUGH_REVIEW)
        run_main(self.review, "gpt-5.6", "the branch")

        (row,) = rows(self.db_path)
        self.assertEqual(row["status"], self.review.STATUS_UNPARSED)
        self.assertIsNone(row["grade"])
        self.assertEqual(row["review_text"], LONG_ENOUGH_REVIEW)

    def test_every_failure_status_is_recorded_with_no_grade_and_no_text(self):
        for mode, status in (
            ("empty", self.review.STATUS_EMPTY_OUTPUT),
            ("nonzero", self.review.STATUS_NONZERO_EXIT),
        ):
            with self.subTest(mode=mode):
                self.set_env(FAKE_HARNESS_MODE=mode)
                run_main(self.review, "gpt-5.6", f"the branch via {mode}")

                (row,) = [
                    r for r in rows(self.db_path) if r["description"].endswith(mode)
                ]
                self.assertEqual(row["status"], status)
                self.assertIsNone(row["grade"])
                self.assertIsNone(row["review_text"])

    def test_a_missing_harness_is_recorded_with_no_grade(self):
        self.use_fake_harness(
            argv=("/nonexistent/harness", self.review.PROMPT_PLACEHOLDER)
        )
        run_main(self.review, "gpt-5.6", "the branch")

        (row,) = rows(self.db_path)
        self.assertEqual(row["status"], self.review.STATUS_HARNESS_MISSING)
        self.assertIsNone(row["grade"])

    def test_a_timed_out_reviewer_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.set_env(
                FAKE_HARNESS_MODE="hang",
                FAKE_HARNESS_PIDFILE=str(pathlib.Path(tmp) / "pid"),
                REVIEW_TIMEOUT="1",
            )
            run_main(self.review, "gpt-5.6", "the branch")

        (row,) = rows(self.db_path)
        self.assertEqual(row["status"], self.review.STATUS_TIMEOUT)
        self.assertIsNone(row["grade"])
        self.assertIsNone(row["review_text"])

    def test_a_self_reported_harness_error_is_recorded(self):
        self.use_fake_harness(family="claude")
        self.echo(json.dumps({"result": "out of credit", "is_error": True}))
        run_main(self.review, "gpt-5.6", "the branch")

        (row,) = rows(self.db_path)
        self.assertEqual(row["status"], self.review.STATUS_HARNESS_ERROR)
        self.assertIsNone(row["grade"])

    def test_cost_is_recorded_when_the_harness_reports_it(self):
        self.use_fake_harness(family="claude")
        self.echo(json.dumps({"result": reply("A"), "total_cost_usd": 0.0412}))
        run_main(self.review, "gpt-5.6", "the branch")

        (row,) = rows(self.db_path)
        self.assertEqual(row["cost_usd"], 0.0412)

    def test_a_harness_without_cost_reporting_records_null(self):
        self.echo(reply("A"))
        run_main(self.review, "gpt-5.6", "the branch")

        (row,) = rows(self.db_path)
        self.assertIsNone(row["cost_usd"])

    def test_successive_invocations_append_rather_than_replace(self):
        self.echo(reply("A"))
        run_main(self.review, "gpt-5.6", "the first review")
        self.echo(reply("C"))
        run_main(self.review, "gpt-5.6", "the second review")

        recorded = rows(self.db_path)
        self.assertEqual(len(recorded), 2)
        self.assertEqual(
            [row["description"] for row in recorded],
            ["the first review", "the second review"],
        )
        self.assertNotEqual(recorded[0]["run_id"], recorded[1]["run_id"])


if __name__ == "__main__":
    unittest.main()
