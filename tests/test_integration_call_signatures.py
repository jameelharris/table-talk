# A static guard on the call sites that the default test run never executes.
#
# WHAT IT CHECKS
#
# Every call in `tests/` to a function or class imported from `table_talk`, where
# that call sits inside a test marked `@pytest.mark.integration` or inside a
# helper reachable from one, supplies every parameter that has no default. That
# is the whole check.
#
# WHY IT EXISTS
#
# `uv run pytest` deselects integration tests (`addopts` in pyproject.toml), so a
# signature change that breaks one of their call sites stays green until someone
# runs `-m integration` against real GCP. Provenance made `prompt_hashes` a
# required keyword-only argument on four orchestrators; five integration tests
# went on calling them without it, and the `TypeError`s surfaced only at the next
# integration run, long after the commit that caused them.
#
# WHAT IT DOES NOT CHECK
#
# - **Types or values.** A stub hash where a real one belongs passes here. This
#   says arguments are *present*, never that they are right.
# - **Calls it cannot resolve statically**: through a variable or an attribute, or
#   with a `**kwargs` splat, whose contents are unknowable without running the
#   test. Those are skipped rather than guessed at.
# - **Names it cannot attribute to a project callable.** Resolution goes through
#   each test file's own imports, so `Path.resolve` and the three per-phase
#   `check_preconditions` cannot collide; anything unresolvable is skipped.
# - **Production call sites**, which the unit tests already exercise, and
#   non-integration test calls, which fail in the default run anyway.
#
# It reads the test files as text and never imports them, so it does not violate
# CLAUDE.md's rule that no test file imports another phase's test files.

import ast
import importlib
import inspect
import pathlib

_TESTS_DIR = pathlib.Path(__file__).resolve().parent


def _required_parameters(obj) -> tuple[list[str], list[str]]:
    """(required positional names, required keyword-only names) for one callable.

    One signature read covers functions and dataclasses alike — a dataclass's
    fields without defaults are its `__init__`'s required parameters. Exception
    classes have no introspectable signature; they are reported as requiring
    nothing, which is the right answer for a guard that only looks for absences.
    """
    try:
        parameters = inspect.signature(obj).parameters.values()
    except (ValueError, TypeError):
        return [], []
    positional, keyword_only = [], []
    for p in parameters:
        if p.default is not inspect.Parameter.empty:
            continue
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
            positional.append(p.name)
        elif p.kind == p.KEYWORD_ONLY:
            keyword_only.append(p.name)
    return positional, keyword_only


def _project_callables(tree: ast.Module) -> dict:
    """Local name -> project callable, from this module's own imports only.

    Per-file resolution is what keeps the guard from confusing same-named things:
    `check_preconditions` means a different function in each of three phases.
    """
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not (node.module or "").startswith("table_talk"):
            continue
        module = importlib.import_module(node.module)
        for alias in node.names:
            obj = getattr(module, alias.name, None)
            if inspect.isfunction(obj) or inspect.isclass(obj):
                found[alias.asname or alias.name] = obj
    return found


def _integration_scope(tree: ast.Module) -> set[str]:
    """Names of integration-marked tests, plus helpers reachable from them.

    The transitive half is load-bearing: Phase 3's integration test delegates its
    whole body to `_integration_body`, so the call sites that matter are one hop
    from the marker.
    """
    marked: set[str] = set()
    callers: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any("integration" in ast.unparse(d) for d in node.decorator_list):
            marked.add(node.name)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                callers.setdefault(sub.func.id, set()).add(node.name)

    grew = True
    while grew:
        grew = False
        for callee, its_callers in callers.items():
            if callee not in marked and its_callers & marked:
                marked.add(callee)
                grew = True
    return marked


def missing_arguments(source: str) -> list[str]:
    """One finding per integration call site that omits a required argument."""
    tree = ast.parse(source)
    callables = _project_callables(tree)
    scope = _integration_scope(tree)

    # The outermost enclosing function, since ast.walk reaches it first — which is
    # the one carrying the integration marker when the call is nested in a
    # closure.
    enclosing: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                enclosing.setdefault(id(sub), node.name)

    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        obj = callables.get(node.func.id)
        if obj is None or enclosing.get(id(node)) not in scope:
            continue
        if any(keyword.arg is None for keyword in node.keywords):
            continue  # **kwargs splat: what it supplies is not knowable here
        supplied = {keyword.arg for keyword in node.keywords}
        positional, keyword_only = _required_parameters(obj)
        missing = [p for p in positional[len(node.args):] if p not in supplied]
        missing += [k for k in keyword_only if k not in supplied]
        if missing:
            findings.append(
                f"line {node.lineno}: {node.func.id}() in "
                f"{enclosing[id(node)]} is missing {', '.join(missing)}"
            )
    return findings


def test_every_integration_call_site_supplies_its_required_arguments():
    findings = {}
    for path in sorted(_TESTS_DIR.rglob("test_*.py")):
        stale = missing_arguments(path.read_text())
        if stale:
            findings[path.name] = stale
    assert findings == {}, (
        "integration call sites are stale against the current signatures — the "
        "default test run cannot see these:\n"
        + "\n".join(f"  {name}: {s}" for name, lines in findings.items() for s in lines)
    )


# --- the guard's own behaviour ----------------------------------------------
#
# process_video is the subject because it has both required positional parameters
# and a required keyword-only one (prompt_hashes).

_CALL = """
import pytest
from table_talk.payout_processing import process_video

{decorator}
def test_something():
    process_video("v", "/tmp/x.mp4", "proj", "ds", "bucket", "prompt"{extra})
"""


def test_the_guard_catches_a_missing_required_argument():
    findings = missing_arguments(
        _CALL.format(decorator="@pytest.mark.integration", extra="")
    )
    assert len(findings) == 1
    assert "process_video() in test_something is missing prompt_hashes" in findings[0]


def test_the_guard_passes_the_same_call_once_the_argument_is_supplied():
    assert missing_arguments(
        _CALL.format(decorator="@pytest.mark.integration", extra=", prompt_hashes={}")
    ) == []


def test_the_guard_ignores_a_call_outside_an_integration_test():
    """A unit test's stale call fails in the default run, which is the whole
    reason this guard is scoped to the ones that do not."""
    assert missing_arguments(_CALL.format(decorator="", extra="")) == []


def test_the_guard_follows_a_helper_called_from_an_integration_test():
    source = """
import pytest
from table_talk.payout_processing import process_video


async def _body():
    process_video("v", "/tmp/x.mp4", "proj", "ds", "bucket", "prompt")


@pytest.mark.integration
def test_something():
    _body()
"""
    findings = missing_arguments(source)
    assert len(findings) == 1
    assert "process_video() in _body is missing prompt_hashes" in findings[0]
