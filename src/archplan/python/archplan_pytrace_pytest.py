"""pytest plugin for archplan_pytrace: records each test function's call phase.
Load it with `-p archplan_pytrace_pytest`; pytest-xdist workers load it too."""

import unittest

import pytest

import archplan_pytrace as trace


def pytest_configure(config):
    trace.install()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    cls = getattr(item, "cls", None)
    if (cls is not None and issubclass(cls, unittest.TestCase)) or trace.current is not None:
        yield  # unittest classes are recorded by the unittest patch
        return
    path = str(getattr(item, "path", None) or item.fspath)
    line = item.location[1] + 1 if item.location[1] is not None else None
    trace.start(item.name, item.nodeid, path, line)
    state = "failed"
    try:
        result = yield
        exc = result.excinfo
        if exc is None:
            state = "passed"
        elif issubclass(exc[0], (pytest.skip.Exception, pytest.xfail.Exception)):
            state = None
    finally:
        trace.stop(state)
