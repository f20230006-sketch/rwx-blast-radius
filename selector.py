"""Select tests from baseline dependencies and schedule them by recorded cost."""

import argparse
import ast
import json
import re
from functools import lru_cache
from pathlib import Path


HERE = Path(__file__).resolve().parent
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
DIFF = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def patched_source(original, patch):
    """Apply a unified diff for an existing file, checking its old-side context."""
    source = original.splitlines()
    result = []
    cursor = 0
    remaining = None
    saw_hunk = False
    for line in patch:
        match = HUNK.match(line)
        if match:
            if remaining:
                raise ValueError("Incomplete hunk")
            start, count = match.groups()
            remaining = int(count) if count is not None else 1
            start = int(start) - bool(remaining)
            if not cursor <= start <= len(source):
                raise ValueError("Invalid hunk position")
            result.extend(source[cursor:start])
            cursor = start
            saw_hunk = True
        elif saw_hunk and line and line[0] in " +-":
            if line[0] != "+":
                if not remaining or cursor >= len(source) or source[cursor] != line[1:]:
                    raise ValueError("Patch does not match baseline")
                cursor += 1
                remaining -= 1
            if line[0] != "-":
                result.append(line[1:])
        elif saw_hunk and line != "\\ No newline at end of file":
            raise ValueError("Unexpected hunk content")
    if not saw_hunk or remaining:
        raise ValueError("Missing or incomplete hunk")
    return "\n".join(result + source[cursor:]) + "\n"


def structure(tree):
    """Extract function bodies while retaining import-time code in the tree."""
    bodies = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            body = node.body
            bodies.append((body[0].lineno, node.end_lineno, ast.dump(ast.Module(body=body))))
            node.body = []

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Lambda(self, node):
            if node.end_lineno > node.lineno:
                # The declaration line may also be covered when the lambda is created.
                bodies.append((node.lineno + 1, node.end_lineno, ast.dump(node.body)))
                node.body = ast.Constant(value=None)

    Visitor().visit(tree)
    return ast.dump(tree), bodies


class Selector:
    def __init__(self, tests, dependencies, source):
        self.tests = tests
        self.source = source
        self.all_tests = (1 << len(tests)) - 1
        self.files = dependencies["files"]
        self.fixtures = dependencies["fixtures"]
        mapped = {name: index for index, name in enumerate(dependencies["tests"])}
        self.number_by_bit = {}
        self.unmapped = 0
        for index, test in enumerate(tests):
            bit = 1 << index
            if test["id"] in mapped:
                self.number_by_bit[1 << mapped[test["id"]]] = bit
            else:
                self.unmapped |= bit

    @lru_cache(maxsize=4096)
    def translate(self, bits):
        selected = 0
        while bits:
            low = bits & -bits
            selected |= self.number_by_bit.get(low, 0)
            bits ^= low
        return selected

    @lru_cache(maxsize=64)
    def baseline(self, path):
        content = (self.source / path).read_text(encoding="utf-8")
        return content, structure(ast.parse(content))

    def python_change(self, path, patch):
        if path not in self.files:
            return self.all_tests
        original, (old_structure, old_bodies) = self.baseline(path)
        changed = patched_source(original, patch)
        compile(changed, path, "exec", dont_inherit=True)
        new_structure, new_bodies = structure(ast.parse(changed))
        if old_structure != new_structure or len(old_bodies) != len(new_bodies):
            return self.all_tests

        bits = 0
        lines = self.files[path]
        for (start, end, old), (_, _, new) in zip(old_bodies, new_bodies):
            if old == new:
                continue
            for line in range(start, end + 1):
                value = lines.get(str(line))
                if value:
                    tests, collected = value
                    if collected:
                        return self.all_tests
                    bits |= int(tests, 16)
        # A source-only change may still affect source inspection or line-sensitive behavior.
        if not bits:
            for tests, _ in lines.values():
                bits |= int(tests, 16)
        return (self.translate(bits) | self.unmapped) if bits else self.all_tests

    def select_file(self, header, patch):
        match = DIFF.fullmatch(header)
        if not match or match[1] != match[2]:
            return self.all_tests
        path = match[1]
        if ".." in Path(path).parts:
            return self.all_tests
        if path in self.fixtures:
            return self.translate(int(self.fixtures[path], 16)) | self.unmapped
        if path.startswith("sqlglot/") and path.endswith(".py"):
            return self.python_change(path, patch)
        return self.all_tests

    def select(self, patch):
        selected = 0
        header = None
        content = []
        for line in patch.splitlines():
            if line.startswith("diff --git "):
                if header is not None:
                    selected |= self.select_file(header, content)
                    if selected == self.all_tests:
                        return selected
                header, content = line, []
            elif header is not None:
                content.append(line)
        if header is None:
            return self.all_tests
        return selected | self.select_file(header, content)


def schedule(tests, selected):
    if not selected:
        return []
    jobs = []
    while selected:
        low = selected & -selected
        test = tests[low.bit_length() - 1]
        jobs.append((round(test["seconds"] * 1000), test["number"]))
        selected ^= low
    jobs.sort(reverse=True)
    best_cost, best_workers = float("inf"), []
    for count in range(1, min(8, len(jobs)) + 1):
        if jobs[0][0] + 2000 * count >= best_cost:
            break
        workers = [[] for _ in range(count)]
        loads = [0] * count
        for duration, number in jobs:
            worker = loads.index(min(loads))
            workers[worker].append(number)
            loads[worker] += duration
        workers = [worker for worker in workers if worker]
        cost = max(loads) + 2000 * len(workers)
        if cost < best_cost:
            best_cost, best_workers = cost, workers
    return best_workers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--map", type=Path, default=HERE / "dependencies.json")
    parser.add_argument("--source", type=Path, default=HERE / "source")
    args = parser.parse_args()
    tests = json.loads((args.data / "tests.json").read_text(encoding="utf-8"))["tests"]
    dependencies = json.loads(args.map.read_text(encoding="utf-8"))
    selector = Selector(tests, dependencies, args.source)

    @lru_cache(maxsize=128)
    def workers_for(selected):
        return schedule(tests, selected)

    for patch in sorted((args.data / "commits").glob("*.patch")):
        try:
            selected = selector.select(patch.read_text(encoding="utf-8"))
        except (OSError, ValueError, SyntaxError, RecursionError):
            selected = selector.all_tests
        workers = workers_for(selected)
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))


if __name__ == "__main__":
    main()
