"""Load the extensionless bundled summary script as a module."""

import pathlib
import types


SCRIPT = (
    pathlib.Path(__file__).resolve().parent.parent
    / "skills/cross-agent-review-summarize/scripts/cross-agent-review-summarize"
)


def load():
    module = types.ModuleType("summary_tool")
    module.__file__ = str(SCRIPT)
    exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), module.__dict__)
    return module
