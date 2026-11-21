"""Record which baseline tests instantiate each SQLGlot class."""

import atexit
import json
import os
import sys
import tempfile
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest


observed = defaultdict(set)
active = None
installed = False


def install():
    global installed
    if installed:
        return
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.expressions import Expr

    for base in (Dialect, Expr):
        original = base.__init__

        def constructor(self, *args, initialize=original, **kwargs):
            if active is not None:
                active.add(type(self))
            initialize(self, *args, **kwargs)

        base.__init__ = constructor
    installed = True


def observations():
    by_class = defaultdict(set)
    for test, classes in observed.items():
        for cls in classes:
            for base in cls.__mro__:
                if base.__module__.startswith("sqlglot."):
                    by_class[f"{base.__module__}:{base.__qualname__}"].add(test)
    return {cls: sorted(tests) for cls, tests in by_class.items()}


def run_job(directory, test, function, args, kwargs):
    global active
    if not installed:
        install()
        target = Path(directory) / f"{os.getpid()}.json"
        atexit.register(lambda: target.write_text(json.dumps(observations()), encoding="utf-8"))
    active = observed[test]
    try:
        return function(*args, **kwargs)
    finally:
        active = None


class Observe:
    current = None

    def __init__(self):
        self.failed = set()

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failed.add(report.nodeid)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item):
        global active
        self.current = item.nodeid
        active = observed[item.nodeid]
        try:
            return (yield)
        finally:
            self.current = None
            active = None


def main():
    from observe import run_job

    sys.path.insert(0, str(Path.cwd()))
    install()
    observer = Observe()
    submit = ProcessPoolExecutor.submit
    with tempfile.TemporaryDirectory() as directory:
        def submit_job(executor, function, /, *args, **kwargs):
            if observer.current is None:
                return submit(executor, function, *args, **kwargs)
            return submit(executor, run_job, directory, observer.current, function, args, kwargs)

        ProcessPoolExecutor.submit = submit_job
        try:
            code = pytest.main(["-q", "-p", "no:cacheprovider", "tests"], plugins=[observer])
        finally:
            ProcessPoolExecutor.submit = submit
        if code not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
            return code
        merged = {cls: set(tests) for cls, tests in observations().items()}
        for path in Path(directory).glob("*.json"):
            for cls, tests in json.loads(path.read_text(encoding="utf-8")).items():
                merged.setdefault(cls, set()).update(tests)
    names = sorted({test for tests in merged.values() for test in tests} | observer.failed)
    numbers = {test: index for index, test in enumerate(names)}
    masks = {
        cls: hex(sum(1 << numbers[test] for test in tests | observer.failed))
        for cls, tests in merged.items()
    }
    Path(sys.argv[1]).write_text(
        json.dumps({"tests": names, "classes": masks}, separators=(",", ":")), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
