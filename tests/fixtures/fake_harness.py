#!/usr/bin/env python3
"""Stand-in for a real coding harness, driven by environment variables.

FAKE_HARNESS_MODE selects the behavior:
  echo      write FAKE_HARNESS_OUTPUT to stdout and exit 0 (the default)
  echo_both write FAKE_HARNESS_OUTPUT to stdout and FAKE_HARNESS_STDERR to stderr
  empty     write nothing and exit 0
  nonzero   write to stderr and exit 3
  env       write the inherited REVIEW_ACTIVE value to stdout
  hang      spawn a long-lived grandchild, record its pid in
            FAKE_HARNESS_PIDFILE, then sleep past any sane timeout
  detached  like hang, but the grandchild starts its own session, so it
            survives a process-group kill and keeps the output pipes open
"""

import os
import subprocess
import sys
import time

MODE = os.environ.get("FAKE_HARNESS_MODE", "echo")

DEFAULT_OUTPUT = "a review body long enough to classify as real"


def sleep_with_grandchild(detached):
    grandchild = subprocess.Popen(["sleep", "300"], start_new_session=detached)
    with open(os.environ["FAKE_HARNESS_PIDFILE"], "w") as pidfile:
        pidfile.write(str(grandchild.pid))
        pidfile.flush()
        os.fsync(pidfile.fileno())
    time.sleep(300)


def main():
    if MODE == "echo":
        sys.stdout.write(os.environ.get("FAKE_HARNESS_OUTPUT", DEFAULT_OUTPUT))
    elif MODE == "echo_both":
        sys.stdout.write(os.environ.get("FAKE_HARNESS_OUTPUT", DEFAULT_OUTPUT))
        sys.stderr.write(os.environ.get("FAKE_HARNESS_STDERR", ""))
    elif MODE == "empty":
        pass
    elif MODE == "nonzero":
        sys.stderr.write("harness failed")
        return 3
    elif MODE == "env":
        inherited = os.environ.get("REVIEW_ACTIVE", "<unset>")
        sys.stdout.write(f"REVIEW_ACTIVE={inherited} as seen by the fake harness")
    elif MODE == "hang":
        sleep_with_grandchild(detached=False)
    elif MODE == "detached":
        sleep_with_grandchild(detached=True)
    else:
        raise SystemExit(f"unknown FAKE_HARNESS_MODE {MODE!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
