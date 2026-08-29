import contextlib
import io
import os
import pathlib
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

import summary_module


SCHEMA = """
CREATE TABLE reviews (
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    project TEXT,
    author TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    harness TEXT NOT NULL,
    grade TEXT,
    status TEXT NOT NULL,
    duration_s REAL,
    cost_usd REAL
)
"""


class SummaryTest(unittest.TestCase):
    def setUp(self):
        self.summary = summary_module.load()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = pathlib.Path(self.temporary.name) / "reviews.db"
        connection = sqlite3.connect(self.path)
        connection.execute(SCHEMA)
        connection.close()

    def insert(
        self,
        run_id,
        author,
        reviewer,
        grade,
        status="ok",
        project="shop",
        ts="2026-08-01T00:00:00+00:00",
        duration_s=10.0,
        cost_usd=None,
        harness="agent-cli",
    ):
        connection = sqlite3.connect(self.path)
        connection.execute(
            "INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                ts,
                project,
                author,
                reviewer,
                harness,
                grade,
                status,
                duration_s,
                cost_usd,
            ),
        )
        connection.commit()
        connection.close()

    def rows(self, **filters):
        connection = self.summary.open_database(self.path)
        try:
            return self.summary.load_rows(connection, **filters)
        finally:
            connection.close()

    def test_author_mean_excludes_failed_and_na_attempts(self):
        self.insert("one", "agent-a", "reviewer-1", "A")
        self.insert("one", "agent-a", "reviewer-2", "C")
        self.insert("two", "agent-a", "reviewer-1", "NA", "not_found")
        self.insert("three", "agent-a", "reviewer-2", None, "timeout")

        stats = self.summary.group_stats(self.rows(), "author", "reviewer")[0]

        self.assertEqual(stats.mean, 3.0)
        self.assertEqual(stats.graded, 2)
        self.assertEqual(stats.attempts, 4)
        self.assertEqual(stats.invocations, 3)
        self.assertEqual(dict(stats.distribution), {"A": 1, "B": 0, "C": 1, "D": 0, "F": 0})

    def test_only_ok_rows_with_a_known_grade_contribute_to_statistics(self):
        self.insert("one", "agent-a", "reviewer", "A")
        self.insert("two", "agent-a", "reviewer", "A", "timeout")
        self.insert("three", "agent-a", "reviewer", "B", "unparsed")
        self.insert("four", "agent-a", "reviewer", None, "ok")

        stats = self.summary.group_stats(self.rows(), "author", "reviewer")[0]

        self.assertEqual(stats.mean, 4.0)
        self.assertEqual(stats.graded, 1)
        self.assertEqual(stats.attempts, 4)
        self.assertEqual(dict(stats.distribution), {"A": 1, "B": 0, "C": 0, "D": 0, "F": 0})

    def test_report_compares_author_and_reviewer_tendencies(self):
        self.insert("one", "agent-a", "strict", "C", duration_s=20, cost_usd=0.25)
        self.insert("one", "agent-a", "lenient", "A", duration_s=10)
        self.insert("two", "agent-b", "strict", "D", duration_s=40, cost_usd=0.50)

        report = self.summary.build_report(self.rows(), self.path)

        self.assertIn("## Authors", report)
        self.assertIn("agent-a | 3.00/4 | 2/2", report)
        self.assertIn("agent-b | 1.00/4 | 1/1", report)
        self.assertIn(
            "strict via agent-cli | 1.50/4 | 2/2 | 2 | 30.0s | $0.7500 (2/2 rows)",
            report,
        )
        self.assertIn("lenient via agent-cli | 4.00/4", report)

    def test_same_reviewer_through_different_harnesses_stays_separate(self):
        self.insert("one", "agent-a", "reviewer", "A", harness="first")
        self.insert("two", "agent-a", "reviewer", "F", harness="second")

        stats = self.summary.group_stats(
            self.rows(), self.summary.reviewer_identity, "author"
        )

        self.assertEqual(
            [(item.name, item.mean) for item in stats],
            [("reviewer via first", 4.0), ("reviewer via second", 0.0)],
        )

    def test_agreement_uses_only_reviewers_of_the_same_invocation(self):
        self.insert("one", "agent-a", "reviewer-1", "A")
        self.insert("one", "agent-a", "reviewer-2", "C")
        self.insert("two", "agent-a", "reviewer-1", "B")
        self.insert("two", "agent-a", "reviewer-2", "B")
        self.insert("three", "agent-b", "reviewer-1", "F")

        agreement = self.summary.agreement_stats(self.rows())

        self.assertEqual(agreement.comparable, 2)
        self.assertEqual(agreement.exact, 1)
        self.assertEqual(agreement.average_spread, 1.0)
        self.assertEqual(len(agreement.disagreements), 1)

    def test_project_and_date_filters_are_applied_together(self):
        self.insert("one", "agent-a", "reviewer", "A", project="shop", ts="2026-07-01T00:00:00+00:00")
        self.insert("two", "agent-b", "reviewer", "B", project="shop", ts="2026-08-02T00:00:00+00:00")
        self.insert("three", "agent-c", "reviewer", "C", project="other", ts="2026-08-03T00:00:00+00:00")

        rows = self.rows(project="shop", since="2026-08-01")

        self.assertEqual([row["author"] for row in rows], ["agent-b"])

    def test_z_timestamp_is_normalized_before_filtering(self):
        self.insert(
            "one",
            "agent-a",
            "reviewer",
            "A",
            ts="2026-08-01T00:00:00+00:00",
        )

        since = self.summary.parse_since("2026-08-01T00:00:00Z")

        self.assertEqual(since, "2026-08-01T00:00:00+00:00")
        self.assertEqual(len(self.rows(since=since)), 1)

    def test_legacy_schema_can_be_summarized_but_not_filtered_by_project(self):
        legacy_path = pathlib.Path(self.temporary.name) / "legacy.db"
        connection = sqlite3.connect(legacy_path)
        connection.executescript(
            """
            CREATE TABLE reviews (
                run_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                author TEXT NOT NULL,
                reviewer TEXT NOT NULL,
                harness TEXT NOT NULL,
                grade TEXT,
                status TEXT NOT NULL
            );
            INSERT INTO reviews VALUES (
                'run', '2026-08-01T00:00:00+00:00', 'author', 'reviewer', 'cli', 'B', 'ok'
            );
            """
        )
        connection.close()
        readonly = self.summary.open_database(legacy_path)
        self.addCleanup(readonly.close)

        rows = self.summary.load_rows(readonly)

        self.assertIsNone(rows[0]["project"])
        self.assertIsNone(rows[0]["duration_s"])
        with self.assertRaisesRegex(self.summary.DataError, "cannot be filtered"):
            self.summary.load_rows(readonly, project="shop")

    def test_empty_scope_and_markdown_values_are_reported_safely(self):
        report = self.summary.build_report(
            [], self.path, project="shop|admin", since="2026-08-01"
        )

        self.assertIn("project `shop\\|admin`", report)
        self.assertIn("No review attempts matched this scope.", report)

    def test_database_is_opened_read_only(self):
        connection = self.summary.open_database(self.path)
        self.addCleanup(connection.close)

        with self.assertRaises(sqlite3.OperationalError):
            connection.execute(
                "INSERT INTO reviews (run_id, ts, author, reviewer, harness, status) "
                "VALUES ('x', 'x', 'x', 'x', 'x', 'x')"
            )

    def test_default_database_path_matches_the_review_tool(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                self.summary.database_path(),
                pathlib.Path.home() / ".local/share/cross-agent-review/reviews.db",
            )
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/data"}, clear=True):
            self.assertEqual(
                self.summary.database_path(),
                pathlib.Path("/data/cross-agent-review/reviews.db"),
            )
        with mock.patch.dict(os.environ, {"REVIEW_DB": "/tmp/custom.db"}, clear=True):
            self.assertEqual(self.summary.database_path(), pathlib.Path("/tmp/custom.db"))

    def test_missing_database_returns_a_clear_error(self):
        missing = pathlib.Path(self.temporary.name) / "missing.db"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.summary.main(["--db", str(missing)])

        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn(f"no review database at {missing}", err.getvalue())

    def test_bundled_script_is_executable_and_has_help(self):
        self.assertTrue(os.access(summary_module.SCRIPT, os.X_OK))
        result = subprocess.run(
            [str(summary_module.SCRIPT), "--help"], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("Summarize grades", result.stdout)


if __name__ == "__main__":
    unittest.main()
