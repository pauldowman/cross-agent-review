"""Load the advice script while keeping tests off the user's ledger and config."""

import pathlib
import types

import review_module

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "skills/cross-agent-advice/scripts/cross-agent-advice"


def load():
    module = types.ModuleType("advice_tool")
    module.__file__ = str(SCRIPT)
    exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), module.__dict__)
    return module
