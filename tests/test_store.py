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
from test_spawn import GOAL, PROJECT, LONG_ENOUGH_REVIEW, SpawnTestCase, read_reviews, run_main


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
            expected = pathlib.Path.home() / ".local/share/cross-agent-review/reviews.db"
            self.assertEqual(self.review.database_path(), expected)

    def test_xdg_data_home_is_honored(self):
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/somewhere/data"}):
            os.environ.pop(self.review.DB_ENV_VAR, None)
            self.assertEqual(
                self.review.database_path(),
                pathlib.Path("/somewhere/data/cross-agent-review/reviews.db"),
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
                "INSERT INTO reviews (run_id, ts, project, author, reviewer,"
                " harness, goal, description, cwd, status)"
                " VALUES ('r','t','p','a','v','h','g','d','c','ok')"
            )
            connection.commit()
            connection.close()

            self.review.open_database(path).close()
            self.assertEqual(len(rows(path)), 1)


class AdviceSchemaTest(SpawnTestCase):
    def assert_advice_tables(self, connection):
        self.assertEqual(
            [row[1] for row in connection.execute("PRAGMA table_info(advice)")],
            ["id", "run_id", "ts", "project", "author", "advisor", "harness",
             "effort", "question", "context", "cwd", "branch", "git_sha",
             "recommendation", "answer_text", "duration_s", "status", "cost_usd"],
        )
        self.assertEqual(
            [row[1] for row in connection.execute("PRAGMA table_info(advice_decisions)")],
            ["run_id", "ts", "decision"],
        )
        for table in ("advice", "advice_decisions"):
            self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)

    def test_fresh_database_has_all_three_tables(self):
        connection = self.review.open_database(self.db_path)
        self.addCleanup(connection.close)
        self.assert_advice_tables(connection)
        self.assertTrue(connection.execute("PRAGMA table_info(reviews)").fetchall())

    def test_v4_review_rows_survive_migration(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute(self.review.REVIEWS_TABLE)
        connection.execute(
            "INSERT INTO reviews (run_id, ts, project, author, reviewer, harness, goal, description, cwd, status, effort) "
            "VALUES ('old', 't', 'p', 'a', 'v', 'h', 'g', 'd', 'c', 'ok', 'high')"
        )
        connection.execute("PRAGMA user_version=4")
        connection.commit()
        connection.close()
        connection = self.review.open_database(self.db_path)
        self.addCleanup(connection.close)
        self.assert_advice_tables(connection)
        self.assertEqual(connection.execute("SELECT run_id, effort FROM reviews").fetchall(), [("old", "high")])

    def test_v3_migrates_through_both_versions(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute(V1_SCHEMA)
        connection.execute("ALTER TABLE reviews ADD COLUMN project TEXT")
        connection.execute("ALTER TABLE reviews ADD COLUMN goal TEXT")
        connection.execute("PRAGMA user_version=3")
        connection.close()
        connection = self.review.open_database(self.db_path)
        self.addCleanup(connection.close)
        self.assert_advice_tables(connection)
        self.assertIn("effort", [row[1] for row in connection.execute("PRAGMA table_info(reviews)")])

    def test_v6_is_refused_without_changing_its_stamp(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute("PRAGMA user_version=6")
        connection.close()
        with contextlib.redirect_stderr(io.StringIO()) as warning:
            self.assertIsNone(self.review.open_database(self.db_path))
        self.assertIn("schema version 6", warning.getvalue())
        connection = sqlite3.connect(self.db_path)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)


V1_SCHEMA = """
CREATE TABLE reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    author TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    harness TEXT NOT NULL,
    description TEXT NOT NULL,
    cwd TEXT NOT NULL,
    branch TEXT,
    git_sha TEXT,
    grade TEXT,
    review_text TEXT,
    duration_s REAL,
    status TEXT NOT NULL,
    cost_usd REAL
)
"""


class MigrationTest(SpawnTestCase):
    """A ledger written before `project` existed must survive the upgrade."""

    def write_v1_database(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute(V1_SCHEMA)
        connection.execute(
            "INSERT INTO reviews (run_id, ts, author, reviewer, harness,"
            " description, cwd, status, grade)"
            " VALUES ('old','2026-08-23T00:00:00+00:00','gpt-5.6','claude-opus-5',"
            "'claude','the branch','/somewhere','ok','B')"
        )
        connection.execute("PRAGMA user_version=1")
        connection.commit()
        connection.close()

    def test_an_older_database_is_migrated_not_refused(self):
        self.write_v1_database()

        connection = self.review.open_database(self.db_path)
        self.assertIsNotNone(connection)
        connection.close()

        (row,) = rows(self.db_path)
        self.assertEqual(row["grade"], "B")
        self.assertIsNone(row["project"], "rows predating the column keep a NULL")

    def test_the_migrated_database_is_stamped_with_the_new_version(self):
        self.write_v1_database()
        self.review.open_database(self.db_path).close()

        connection = sqlite3.connect(self.db_path)
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        connection.close()
        self.assertEqual(version, self.review.SCHEMA_VERSION)

    def test_a_migrated_database_accepts_new_rows(self):
        self.write_v1_database()
        self.set_env(FAKE_HARNESS_MODE="echo", FAKE_HARNESS_OUTPUT=reply("A"))

        code, _, _ = run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")

        self.assertEqual(code, self.review.EXIT_OK)
        recorded = rows(self.db_path)
        self.assertEqual(len(recorded), 2)
        self.assertEqual(recorded[-1]["project"], PROJECT)

    def test_migrating_is_idempotent(self):
        self.write_v1_database()
        for _ in range(3):
            self.review.open_database(self.db_path).close()
        self.assertEqual(len(rows(self.db_path)), 1)


class EffortMigrationTest(SpawnTestCase):
    def test_v3_rows_keep_null_effort_and_new_rows_record_effort(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute(V1_SCHEMA)
        connection.execute("ALTER TABLE reviews ADD COLUMN project TEXT")
        connection.execute("ALTER TABLE reviews ADD COLUMN goal TEXT")
        connection.execute(
            "INSERT INTO reviews (run_id, ts, author, reviewer, harness,"
            " description, cwd, status, grade, project, goal)"
            " VALUES ('old', '2026-08-23T00:00:00+00:00', 'author', 'reviewer',"
            " 'plain', 'the branch', '/somewhere', 'ok', 'B', 'project', 'goal')"
        )
        connection.execute("PRAGMA user_version=3")
        connection.commit()
        connection.close()
        self.route_to(self.fake_reviewer._replace(effort="high"))
        self.set_env(FAKE_HARNESS_MODE="echo", FAKE_HARNESS_OUTPUT=reply("A"))

        code, _, _ = run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")

        self.assertEqual(code, self.review.EXIT_OK)
        old, new = rows(self.db_path)
        self.assertIsNone(old["effort"])
        self.assertEqual(old["grade"], "B")
        self.assertEqual(old["project"], "project")
        self.assertEqual(old["goal"], "goal")
        self.assertEqual(new["effort"], "high")
        self.assertEqual(new["grade"], "A")
        connection = sqlite3.connect(self.db_path)
        self.addCleanup(connection.close)
        self.assertEqual(
            connection.execute("PRAGMA user_version").fetchone()[0],
            self.review.SCHEMA_VERSION,
        )


class ProjectTest(SpawnTestCase):
    def test_the_project_is_recorded(self):
        self.set_env(FAKE_HARNESS_MODE="echo", FAKE_HARNESS_OUTPUT=reply("B"))
        run_main(self.review, "gpt-5.6", "some-other-repo", GOAL, "the branch")

        (row,) = rows(self.db_path)
        self.assertEqual(row["project"], "some-other-repo")

    def test_the_project_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as raised:
                self.review.main(["gpt-5.6", "the branch"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("project", err.getvalue())

    def test_every_reviewer_of_one_invocation_shares_the_project(self):
        self.set_env(FAKE_HARNESS_MODE="echo", FAKE_HARNESS_OUTPUT=reply("B"))
        connection = self.review.open_database(self.db_path)
        invocation = self.review.describe_invocation(PROJECT, "gpt-5.6", GOAL, "x")
        for reviewer in ("first", "second"):
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

        self.assertEqual({row["project"] for row in rows(self.db_path)}, {PROJECT})


class RunIdTest(SpawnTestCase):
    def test_every_reviewer_of_one_invocation_shares_a_run_id(self):
        connection = self.review.open_database(self.db_path)
        invocation = self.review.describe_invocation(PROJECT, "gpt-5.6", GOAL, "the branch")
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
        code, out, err = run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")
        self.assertEqual(code, self.review.EXIT_OK)
        self.assertIn("retry loop", read_reviews(out))
        self.assertIn("not recording", err)

    def test_a_corrupt_database_still_delivers_the_review(self):
        self.db_path.write_text("this is not a sqlite database at all")
        self.set_env(FAKE_HARNESS_MODE="echo", FAKE_HARNESS_OUTPUT=reply("B"))

        code, out, _ = run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")
        self.assertEqual(code, self.review.EXIT_OK)
        self.assertIn("retry loop", read_reviews(out))

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
        invocation = self.review.describe_invocation(PROJECT, "gpt-5.6", GOAL, "the branch")
        self.assertFalse(self.review.record_run(None, invocation, run))


class RecordedRunTest(SpawnTestCase):
    def echo(self, output):
        self.set_env(FAKE_HARNESS_MODE="echo", FAKE_HARNESS_OUTPUT=output)

    def test_a_successful_review_is_recorded_in_full(self):
        self.echo(reply("B"))
        run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the uncommitted changes")

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

    def test_a_fresh_database_records_the_configured_effort(self):
        self.route_to(self.fake_reviewer._replace(effort="low"))
        self.echo(reply("A"))

        code, _, _ = run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")

        self.assertEqual(code, self.review.EXIT_OK)
        (row,) = rows(self.db_path)
        self.assertEqual(row["effort"], "low")

    def test_a_reviewer_that_crashes_in_collect_still_records_its_effort(self):
        reviewer = self.install_harness("broken", "plain", ("/bin/sh", "-c", "true"))
        self.route_to(reviewer._replace(effort="high"))

        code, _, err = run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")

        self.assertEqual(code, self.review.EXIT_ALL_FAILED)
        self.assertIn("broken (high) via plain could not be run", err)
        (row,) = rows(self.db_path)
        self.assertEqual(row["effort"], "high")
        self.assertEqual(row["status"], self.review.STATUS_HARNESS_ERROR)

    def test_the_repository_position_is_recorded(self):
        self.echo(reply("A"))
        run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")

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
            run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")

            (row,) = rows(self.db_path)
            self.assertIsNone(row["branch"])
            self.assertIsNone(row["git_sha"])
            self.assertEqual(row["cwd"], os.getcwd())

    def test_the_grade_is_recorded_even_though_it_is_never_printed(self):
        self.echo(reply("D"))
        _, out, _ = run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")

        (row,) = rows(self.db_path)
        self.assertEqual(row["grade"], "D")
        self.assertNotIn("D</grade>", out + read_reviews(out))

    def test_the_not_found_sentinel_is_recorded_as_its_grade(self):
        self.echo(reply("NA", "I could not find the branch you named anywhere."))
        run_main(self.review, "gpt-5.6", PROJECT, GOAL, "a branch that does not exist")

        (row,) = rows(self.db_path)
        self.assertEqual(row["grade"], self.review.NOT_FOUND_GRADE)
        self.assertEqual(row["status"], self.review.STATUS_NOT_FOUND)

    def test_an_unparsable_reply_is_recorded_with_no_grade(self):
        self.echo(LONG_ENOUGH_REVIEW)
        run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")

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
                run_main(self.review, "gpt-5.6", PROJECT, GOAL, f"the branch via {mode}")

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
        run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")

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
            run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")

        (row,) = rows(self.db_path)
        self.assertEqual(row["status"], self.review.STATUS_TIMEOUT)
        self.assertIsNone(row["grade"])
        self.assertIsNone(row["review_text"])

    def test_a_self_reported_harness_error_is_recorded(self):
        self.use_fake_harness(family="claude")
        self.echo(json.dumps({"result": "out of credit", "is_error": True}))
        run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")

        (row,) = rows(self.db_path)
        self.assertEqual(row["status"], self.review.STATUS_HARNESS_ERROR)
        self.assertIsNone(row["grade"])

    def test_cost_is_recorded_when_the_harness_reports_it(self):
        self.use_fake_harness(family="claude")
        self.echo(json.dumps({"result": reply("A"), "total_cost_usd": 0.0412}))
        run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")

        (row,) = rows(self.db_path)
        self.assertEqual(row["cost_usd"], 0.0412)

    def test_a_harness_without_cost_reporting_records_null(self):
        self.echo(reply("A"))
        run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")

        (row,) = rows(self.db_path)
        self.assertIsNone(row["cost_usd"])

    def test_successive_invocations_append_rather_than_replace(self):
        self.echo(reply("A"))
        run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the first review")
        self.echo(reply("C"))
        run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the second review")

        recorded = rows(self.db_path)
        self.assertEqual(len(recorded), 2)
        self.assertEqual(
            [row["description"] for row in recorded],
            ["the first review", "the second review"],
        )
        self.assertNotEqual(recorded[0]["run_id"], recorded[1]["run_id"])


if __name__ == "__main__":
    unittest.main()
