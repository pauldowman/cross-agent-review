import shlex
import signal
import sqlite3
import subprocess
import time
import unittest

from test_grade import reply
from test_spawn import GOAL, PROJECT, SpawnTestCase, run_main

FIRST = "The first reviewer found an off-by-one in the retry loop."
SECOND = "The second reviewer found a missing index on the lookup table."


def rows(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM reviews")]
    finally:
        connection.close()


class ParallelTestCase(SpawnTestCase):
    def static_harness(self, name, text, delay=0.0):
        """Install a reviewer whose reply is fixed, independent of the others.

        The placeholder is passed as the argument after the script, where sh
        puts it in $0 and the script ignores it -- satisfying the one-
        placeholder rule without the prompt reaching the reply.
        """
        script = f"printf %s {shlex.quote(text)}"
        if delay:
            script = f"sleep {delay}; {script}"
        self.install_harness(
            name,
            "plain",
            ("/bin/sh", "-c", script, self.review.PROMPT_PLACEHOLDER),
        )

    def broken_harness(self, name):
        self.install_harness(
            name,
            "plain",
            ("/nonexistent/harness", self.review.PROMPT_PLACEHOLDER),
        )

    def use_reviewers(self, *names):
        self.route_to(*(self.review.Reviewer("plain", name) for name in names))


class TwoReviewersTest(ParallelTestCase):
    def test_both_reviews_are_returned_in_their_own_sections(self):
        self.static_harness("alpha", reply("A", FIRST))
        self.static_harness("beta", reply("C", SECOND))
        self.use_reviewers("alpha", "beta")

        code, out, _ = run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")

        self.assertEqual(code, self.review.EXIT_OK)
        self.assertIn(FIRST, out)
        self.assertIn(SECOND, out)
        self.assertIn("review from alpha", out)
        self.assertIn("review from beta", out)
        self.assertEqual(out.count("end of review"), 2)

    def test_both_runs_are_recorded_under_one_run_id(self):
        self.static_harness("alpha", reply("A", FIRST))
        self.static_harness("beta", reply("C", SECOND))
        self.use_reviewers("alpha", "beta")

        run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")

        recorded = rows(self.db_path)
        self.assertEqual(len(recorded), 2)
        self.assertEqual(len({row["run_id"] for row in recorded}), 1)
        self.assertEqual(
            sorted((row["reviewer"], row["grade"]) for row in recorded),
            [("alpha", "A"), ("beta", "C")],
        )

    def test_reviewers_run_concurrently_rather_than_one_after_another(self):
        delay = 1.5
        self.static_harness("alpha", reply("A", FIRST), delay=delay)
        self.static_harness("beta", reply("B", SECOND), delay=delay)
        self.use_reviewers("alpha", "beta")

        started = time.monotonic()
        code, out, _ = run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")
        elapsed = time.monotonic() - started

        self.assertEqual(code, self.review.EXIT_OK)
        self.assertIn(FIRST, out)
        self.assertIn(SECOND, out)
        # Concurrent runs finish in about `delay`; sequential ones take 2x.
        # Half-way between the two keeps the assertion meaningful without
        # making it sensitive to load.
        self.assertLess(
            elapsed,
            delay * 1.5,
            "two reviewers took as long as running them in sequence",
        )


class PartialFailureTest(ParallelTestCase):
    def test_one_failure_does_not_withhold_the_other_review(self):
        self.static_harness("alpha", reply("A", FIRST))
        self.broken_harness("beta")
        self.use_reviewers("alpha", "beta")

        code, out, err = run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")

        self.assertEqual(code, self.review.EXIT_OK)
        self.assertIn(FIRST, out)
        self.assertIn("beta via plain failed", err)

    def test_a_failed_reviewer_is_still_recorded(self):
        self.static_harness("alpha", reply("A", FIRST))
        self.broken_harness("beta")
        self.use_reviewers("alpha", "beta")

        run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")

        recorded = {row["reviewer"]: row for row in rows(self.db_path)}
        self.assertEqual(len(recorded), 2)
        self.assertEqual(recorded["alpha"]["status"], self.review.STATUS_OK)
        self.assertEqual(
            recorded["beta"]["status"], self.review.STATUS_HARNESS_MISSING
        )
        self.assertIsNone(recorded["beta"]["grade"])

    def test_every_reviewer_failing_exits_all_failed(self):
        self.broken_harness("alpha")
        self.broken_harness("beta")
        self.use_reviewers("alpha", "beta")

        code, out, err = run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")

        self.assertEqual(code, self.review.EXIT_ALL_FAILED)
        self.assertEqual(out, "")
        self.assertIn("no reviews were produced", err)

    def test_a_reviewer_that_raises_does_not_lose_the_others(self):
        self.static_harness("alpha", reply("A", FIRST))
        self.install_harness(
            "beta", "plain", ("/bin/sh", "-c", "true")
        )
        self.use_reviewers("alpha", "beta")

        code, out, err = run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")

        self.assertEqual(code, self.review.EXIT_OK)
        self.assertIn(FIRST, out)
        self.assertIn("beta via plain could not be run", err)


class InterruptTest(ParallelTestCase):
    """An interrupted run must not leave paid-for reviewers running."""

    def test_live_reviewers_are_killed_on_demand(self):
        process = subprocess.Popen(
            ["sleep", "300"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.addCleanup(process.kill)
        self.review.track(process)

        self.assertEqual(self.review.kill_live_processes(), 1)
        # Reap it here: os.kill(pid, 0) would still succeed on the zombie.
        self.assertEqual(process.wait(timeout=5), -signal.SIGKILL)

    def test_a_finished_reviewer_is_no_longer_tracked(self):
        self.static_harness("alpha", reply("A", FIRST))
        self.use_reviewers("alpha")
        run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")

        self.assertEqual(self.review.kill_live_processes(), 0)

    def test_both_interrupt_signals_are_handled(self):
        original = {
            received: signal.getsignal(received)
            for received in (signal.SIGINT, signal.SIGTERM)
        }
        self.addCleanup(
            lambda: [signal.signal(sig, handler) for sig, handler in original.items()]
        )

        self.review.install_signal_handlers()

        for received in (signal.SIGINT, signal.SIGTERM):
            self.assertIs(signal.getsignal(received), self.review.terminate_everything)


class ThreeReviewersTest(ParallelTestCase):
    def test_every_configured_reviewer_is_run(self):
        for name in ("alpha", "beta", "gamma"):
            self.static_harness(name, reply("B", f"{name} says the code is fine."))
        self.use_reviewers("alpha", "beta", "gamma")

        code, out, _ = run_main(self.review, "gpt-5.6", PROJECT, GOAL, "the branch")

        self.assertEqual(code, self.review.EXIT_OK)
        self.assertEqual(out.count("end of review"), 3)
        self.assertEqual(len(rows(self.db_path)), 3)


if __name__ == "__main__":
    unittest.main()
