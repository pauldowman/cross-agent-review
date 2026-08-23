"""Load the extensionless `bin/review` script as an importable module.

Compiles from source on every load rather than going through the import
system, so a cached `.pyc` can never mask an edit to the script.
"""

import os
import pathlib
import tempfile
import types

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "bin" / "review"

# Every test file imports this module, so this is the one place that can
# guarantee no test ever writes to the user's real review database.
os.environ.setdefault(
    "REVIEW_DB", str(pathlib.Path(tempfile.gettempdir()) / "review-tests.db")
)


def load():
    module = types.ModuleType("review_tool")
    module.__file__ = str(SCRIPT)
    exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), module.__dict__)
    return module
