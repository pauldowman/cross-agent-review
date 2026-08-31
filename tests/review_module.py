"""Load the extensionless bundled skill script as a module.

Compiles from source on every load rather than going through the import
system, so a cached `.pyc` can never mask an edit to the script.
"""

import os
import pathlib
import tempfile
import types

SCRIPT = (
    pathlib.Path(__file__).resolve().parent.parent
    / "skills/cross-agent-review/scripts/cross-agent-review"
)

# Every test file imports this module, so this is the one place that can
# guarantee no test ever writes to the user's real review database.
os.environ.setdefault(
    "REVIEW_DB", str(pathlib.Path(tempfile.gettempdir()) / "review-tests.db")
)

# A reviewer runs with REVIEW_ACTIVE set, so a reviewer asked to check this
# repository inherits it and every test that calls main() hits the recursion
# guard. The tests that care about the guard set it themselves.
os.environ.pop("REVIEW_ACTIVE", None)

# Routing now comes from a config file; tests get a fixture, never the
# user's own, so a missing or edited personal config cannot break the suite.
os.environ.setdefault(
    "CROSS_AGENT_REVIEW_CONFIG",
    str(pathlib.Path(__file__).resolve().parent / "fixtures" / "reviewers.toml"),
)


def load():
    module = types.ModuleType("review_tool")
    module.__file__ = str(SCRIPT)
    exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), module.__dict__)
    return module
