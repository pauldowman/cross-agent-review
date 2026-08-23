"""Load the extensionless `bin/review` script as an importable module.

Compiles from source on every load rather than going through the import
system, so a cached `.pyc` can never mask an edit to the script.
"""

import pathlib
import types

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "bin" / "review"


def load():
    module = types.ModuleType("review_tool")
    module.__file__ = str(SCRIPT)
    exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), module.__dict__)
    return module
