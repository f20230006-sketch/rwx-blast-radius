"""Collect test execution and fixture dependencies at the pinned revision."""

import json
import os
import sys
import tempfile
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import coverage
import pytest


child_tracer = None


def trace_job(data_file, test, function, args, kwargs):
    """Carry the originating pytest ID into each spawned worker."""
    global child_tracer
    if child_tracer is None:
        child_tracer = coverage.Coverage(
            source=["sqlglot"], config_file=False, data_file=data_file,
            data_suffix=True, auto_data=True,
        )
        child_tracer.start()
    child_tracer.switch_context(test)
    return function(*args, **kwargs)


class Dependencies:
    def __init__(self, root, tracer):
        self.root = root
        self.tracer = tracer
        self.active = None
        self.fixtures = defaultdict(set)
        self.passed = set()

    def opened(self, event, args):
        if event != "open" or self.active is None:
            return
        path = args[0]
        if not isinstance(path, str) or "fixtures" not in path:
            return
        relative = os.path.relpath(path, self.root).replace(os.sep, "/")
        if relative.startswith("tests/fixtures/"):
            self.fixtures[relative].add(self.active)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item):
        self.active = item.nodeid
        self.tracer.switch_context(item.nodeid)
        try:
            return (yield)
        finally:
            self.active = None
            self.tracer.switch_context("")

    def pytest_runtest_logreport(self, report):
        if report.when == "call" and report.passed:
            self.passed.add(report.nodeid)


def main():
    # Spawned workers import this callable by module name.
    from build_map import trace_job

    output = Path(sys.argv[1]).resolve()
    root = Path.cwd()
    sys.path.insert(0, str(root))
    with tempfile.TemporaryDirectory() as directory:
        data_file = str(Path(directory) / ".coverage")
        tracer = coverage.Coverage(source=["sqlglot"], config_file=False, data_file=data_file)
        dependencies = Dependencies(root, tracer)
        sys.addaudithook(dependencies.opened)
        submit = ProcessPoolExecutor.submit
        tracked_jobs = False

        def submit_with_test(executor, function, /, *args, **kwargs):
            nonlocal tracked_jobs
            if dependencies.active is None:
                return submit(executor, function, *args, **kwargs)
            tracked_jobs = True
            return submit(executor, trace_job, data_file, dependencies.active, function, args, kwargs)

        ProcessPoolExecutor.submit = submit_with_test
        try:
            with tracer.collect():
                result = pytest.main(["-q", "-p", "no:cacheprovider", "tests"], plugins=[dependencies])
        finally:
            ProcessPoolExecutor.submit = submit
        if result not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
            raise SystemExit(result)
        tracer.combine(strict=tracked_jobs)
        write_map(output, root, tracer.get_data(), dependencies)


def write_map(output, root, data, dependencies):
    if not dependencies.passed:
        raise SystemExit("No baseline tests passed")

    names = sorted(dependencies.passed)
    numbers = {name: index for index, name in enumerate(names)}

    def mask(contexts):
        bits = 0
        for context in contexts:
            if context in numbers:
                bits |= 1 << numbers[context]
        return bits

    files = {}
    for name in sorted(data.measured_files()):
        relative = Path(name).relative_to(root).as_posix()
        lines = {}
        for line, contexts in data.contexts_by_lineno(name).items():
            bits = mask(contexts)
            if bits or "" in contexts:
                lines[str(line)] = [hex(bits), "" in contexts]
        files[relative] = lines
    output.write_text(json.dumps({
        "tests": names,
        "files": files,
        "fixtures": {name: hex(mask(tests)) for name, tests in dependencies.fixtures.items()},
    }, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()
