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


def diff_files(patch):
    header = None
    content = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            if header is not None:
                yield header, content
            header, content = line, []
        elif header is not None:
            content.append(line)
    if header is not None:
        yield header, content


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
            suite = ast.Module(body=body)
            bodies.append((body[0].lineno, node.end_lineno, ast.dump(suite), suite))
            node.body = []

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Lambda(self, node):
            if node.end_lineno > node.lineno:
                # The declaration line may also be covered when the lambda is created.
                bodies.append((node.lineno + 1, node.end_lineno, ast.dump(node.body), node.body))
                node.body = ast.Constant(value=None)

    Visitor().visit(tree)
    return ast.dump(tree), bodies


def expression_edits(old, new):
    """Return baseline statement ranges for simple expression replacements."""
    anchors = set()
    operators = (ast.operator, ast.unaryop, ast.cmpop)

    def visit(left, right, statement=None):
        if isinstance(left, ast.stmt):
            statement = left
        if isinstance(left, operators) and isinstance(right, operators):
            if type(left) is not type(right):
                anchors.add(statement)
            return True
        if type(left) is not type(right):
            return False
        if isinstance(left, ast.Constant):
            if type(left.value) is not type(right.value):
                return False
            if left.value != right.value:
                anchors.add(statement)
            return True
        if isinstance(left, ast.AST):
            return all(visit(value, getattr(right, name), statement) for name, value in ast.iter_fields(left))
        if isinstance(left, list):
            return len(left) == len(right) and all(visit(a, b, statement) for a, b in zip(left, right))
        return left == right

    if not visit(old, new) or not anchors or None in anchors:
        return []
    ranges = []
    for node in anchors:
        end = node.end_lineno
        if body := getattr(node, "body", None):
            if isinstance(body, list):
                end = max(node.lineno, body[0].lineno - 1)
        ranges.append((node.lineno, end))
    return ranges


def literal_shape(source):
    """Retain the structure and types of a module-level literal edit."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            node.value = type(node.value).__name__
    return ast.dump(tree)


def class_literals(source):
    """Separate static class configuration from the rest of a module."""
    tree = ast.parse(source)
    values = {}

    def visit(node, parent=""):
        for child in node.body:
            if isinstance(child, ast.ClassDef):
                visit(child, f"{parent}.{child.name}" if parent else child.name)
            elif parent and isinstance(child, (ast.Assign, ast.AnnAssign)) and child.value:
                try:
                    ast.literal_eval(child.value)
                except (ValueError, TypeError):
                    continue
                target = child.targets if isinstance(child, ast.Assign) else [child.target]
                if len(target) != 1 or not isinstance(target[0], ast.Name):
                    continue
                key = parent, target[0].id
                if key in values:
                    raise ValueError("reassigned class configuration")
                values[key] = ast.dump(child.value)
                child.value = ast.Constant(value=None)

    visit(tree)
    return ast.dump(tree), values


@lru_cache(maxsize=128)
def identity_class(path, name):
    if not path.is_file():
        return False
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return (
                not node.bases and not node.keywords and not node.decorator_list
                and not any(
                    isinstance(member, FUNCTIONS)
                    and member.name in {"__eq__", "__ne__", "__getattribute__"}
                    for member in node.body
                )
            )
    return False


@lru_cache(maxsize=128)
def named_assignments(root, name, attribute_only):
    """Find explicit writes in baseline code and tests before applying a type rule."""
    pattern = re.compile(rf"\b{re.escape(name)}\b")
    writes = []
    for directory in ("sqlglot", "tests"):
        for path in (root / directory).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if not pattern.search(source):
                continue
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Assign):
                    targets, value = node.targets, node.value
                elif isinstance(node, ast.AnnAssign):
                    targets, value = [node.target], node.value
                else:
                    continue
                if value is not None and any(
                    isinstance(target, ast.Attribute) and target.attr == name
                    or not attribute_only and isinstance(target, ast.Name) and target.id == name
                    for target in targets
                ):
                    writes.append(value)
    return writes


def identity_normal_form(text, root):
    """Normalize None comparisons when the other operand has identity equality."""
    tree = ast.parse(text)
    declarations = list(tree.body)
    for node in tree.body:
        if isinstance(node, ast.If) and (
            isinstance(node.test, ast.Attribute) and node.test.attr == "TYPE_CHECKING"
            or isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"
        ):
            declarations.extend(node.body)
    imports = {
        alias.asname or alias.name: (root.joinpath(*node.module.split(".")).with_suffix(".py"), alias.name)
        for node in declarations
        if isinstance(node, ast.ImportFrom) and not node.level and node.module
        for alias in node.names
    }
    enum_types = {"Enum", "IntEnum", "StrEnum"}
    for name, (path, original) in imports.items():
        if path.is_file() and re.search(
            rf"^class {re.escape(original)}\((?:Enum|IntEnum|StrEnum)\):",
            path.read_text(encoding="utf-8"), re.M,
        ):
            enum_types.add(name)
    for node in tree.body:
        if (
            isinstance(node, ast.ClassDef)
            and node.bases
            and all(isinstance(base, ast.Name) and base.id in enum_types | {"str", "int"}
                    for base in node.bases)
            and any(base.id in enum_types for base in node.bases)
            and not node.decorator_list
        ):
            enum_types.add(node.name)

    def enum_member(node, name):
        return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == name

    def enum_attribute(node, name):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return node.func.id == name
        if enum_member(node, name):
            return True
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            defaults = named_assignments(root, node.attr, False)
            return bool(defaults) and all(enum_member(value, name) for value in defaults)
        return False

    for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        attrs = set()
        for node in ast.walk(cls):
            if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Attribute):
                continue
            target, annotation = node.target, node.annotation
            if not (
                isinstance(target.value, ast.Name) and target.value.id == "self"
                and target.attr.startswith("_")
                and isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr)
                and isinstance(annotation.left, ast.Name)
                and isinstance(annotation.right, ast.Constant) and annotation.right.value is None
            ):
                continue
            if annotation.left.id in imports and identity_class(*imports[annotation.left.id]):
                attrs.add(target.attr)
        for node in ast.walk(cls):
            if not isinstance(node, ast.Compare) or len(node.ops) != 1:
                continue
            value, other = node.left, node.comparators[0]
            if (
                isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name)
                and value.value.id == "self"
                and isinstance(node.ops[0], (ast.Eq, ast.NotEq))
            ):
                same = value.attr in attrs and isinstance(other, ast.Constant) and other.value is None
                if isinstance(other, ast.Attribute) and isinstance(other.value, ast.Name):
                    name = other.value.id
                    if name in enum_types:
                        writes = named_assignments(root, value.attr, True)
                        same = bool(writes) and all(enum_attribute(write, name) for write in writes)
                if same:
                    node.ops[0] = ast.Is() if isinstance(node.ops[0], ast.Eq) else ast.IsNot()
    return ast.dump(tree)


class Selector:
    def __init__(self, tests, dependencies, source, classes):
        self.tests = tests
        self.source = source
        self.all_tests = (1 << len(tests)) - 1
        self.files = dependencies["files"]
        self.fixtures = dependencies["fixtures"]
        mapped = {name: index for index, name in enumerate(dependencies["tests"])}
        self.number_by_bit = {}
        self.by_test_file = {}
        self.classes = classes["classes"]
        self.class_test_bits = {
            name: 1 << index for index, name in enumerate(classes["tests"])
        }
        self.unmapped = 0
        for index, test in enumerate(tests):
            bit = 1 << index
            self.by_test_file.setdefault(test["file"], []).append((bit, test["id"]))
            if test["id"] in mapped:
                self.number_by_bit[1 << mapped[test["id"]]] = bit
            else:
                self.unmapped |= bit
        self.class_catalog = [
            (1 << index, self.class_test_bits[test["id"]])
            for index, test in enumerate(tests)
            if test["id"] in self.class_test_bits
        ]

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

    @lru_cache(maxsize=64)
    def dialect_tests(self, dialect):
        # Include sibling implementations for the same dialect and direct
        # references in tests that use inherited implementations.
        bits = 0
        for area in ("dialects", "parsers", "generators", "typing"):
            for tests, _ in self.files.get(f"sqlglot/{area}/{dialect}.py", {}).values():
                bits |= int(tests, 16)
        selected = self.translate(bits)
        selected |= self.referenced_tests(dialect)
        return (selected | self.unmapped) if selected else self.all_tests

    @lru_cache(maxsize=128)
    def referenced_tests(self, name):
        selected = 0
        reference = re.compile(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", re.I)
        for path, tests in self.by_test_file.items():
            if Path(path).stem == f"test_{name.lower()}":
                selected |= sum(bit for bit, _ in tests)
                continue
            source = (self.source / path).read_text(encoding="utf-8")
            if not reference.search(source):
                continue
            lines = source.splitlines()
            names = {
                node.name
                for node in ast.walk(ast.parse(source))
                if isinstance(node, FUNCTIONS)
                and reference.search("\n".join(lines[node.lineno - 1:node.end_lineno]))
            }
            for bit, name in tests:
                if name.rsplit("::", 1)[-1].split("[", 1)[0] in names:
                    selected |= bit
        return selected

    def class_tests(self, path, original, changed):
        old_shape, old_values = class_literals(original)
        shape, values = class_literals(changed)
        if old_shape != shape or old_values.keys() != values.keys():
            return 0
        module = path.removesuffix(".py").replace("/", ".")
        classes = {name for name, field in values if values[name, field] != old_values[name, field]}
        bits = selected = 0
        for name in classes:
            hits = self.classes.get(f"{module}:{name}")
            if hits is None:
                return 0
            bits |= int(hits, 16)
            selected |= self.referenced_tests(name.split(".")[-1])
        return selected | sum(target for target, bit in self.class_catalog if bits & bit)

    def python_change(self, path, patch):
        if path not in self.files:
            return self.all_tests
        original, (old_structure, old_bodies) = self.baseline(path)
        changed = patched_source(original, patch)
        compile(changed, path, "exec", dont_inherit=True)
        new_structure, new_bodies = structure(ast.parse(changed))
        if old_structure != new_structure or len(old_bodies) != len(new_bodies):
            directory = Path(path).parent.name
            dialect = Path(path).stem
            if directory == "expressions":
                selected = self.class_tests(path, original, changed)
                if selected:
                    return selected | self.unmapped
            if (
                directory in {"dialects", "parsers", "generators", "typing"}
                and dialect not in {"__init__", "dialect", "base"}
                and (self.source / "sqlglot" / "dialects" / f"{dialect}.py").is_file()
                and literal_shape(original) == literal_shape(changed)
            ):
                return self.dialect_tests(dialect)
            return self.all_tests
        if all(old[2] == new[2] for old, new in zip(old_bodies, new_bodies)):
            if "type: ignore" not in changed and "type: ignore" not in original:
                return 0
        if (
            any(line.startswith("-") and " is " in line for line in patch)
            and any(line.startswith("+") and ("==" in line or "!=" in line) for line in patch)
        ) and (
            identity_normal_form(original, self.source) == identity_normal_form(changed, self.source)
        ):
            return 0
            if ast.dump(ast.parse(original, type_comments=True)) == ast.dump(
                ast.parse(changed, type_comments=True)
            ):
                return 0

        bits = 0
        lines = self.files[path]
        for (start, end, old, old_tree), (_, _, new, new_tree) in zip(old_bodies, new_bodies):
            if old == new:
                continue
            anchors = expression_edits(old_tree, new_tree)
            for ranges in (anchors, [(start, end)]) if anchors else ([(start, end)],):
                current = 0
                for first, last in ranges:
                    for line in range(first, last + 1):
                        value = lines.get(str(line))
                        if value:
                            tests, collected = value
                            if collected:
                                return self.all_tests
                            current |= int(tests, 16)
                if current:
                    bits |= current
                    break
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
        # Test file changes: only select the tests from that file
        if path.startswith("tests/") and path.endswith(".py"):
            return self.test_file_change(path, patch)
        # Non-code files: docs, CI, configs, etc. don't affect tests
        if self.is_non_code(path):
            return 0
        return self.all_tests

    def test_file_change(self, path, patch):
        """Handle changes to test files themselves."""
        # tests/helpers.py is imported by virtually all tests
        basename = Path(path).name
        if basename == "helpers.py":
            return self.all_tests
        # tests/__init__.py and tests/dialects/__init__.py are empty or trivial
        if basename == "__init__.py":
            return 0
        # Changes to a specific test file: run only that file's tests
        if path in self.by_test_file:
            bits = sum(bit for bit, _ in self.by_test_file[path])
            return bits | self.unmapped
        # Unknown test file: run all tests
        return self.all_tests

    @staticmethod
    def is_non_code(path):
        """Files that never affect test outcomes."""
        NON_CODE_PATTERNS = (
            ".github/", ".pre-commit", "pdoc/", "docs/",
        )
        NON_CODE_EXACT = {
            "AGENTS.md", "CHANGELOG.md", "CLAUDE.md", "CONTRIBUTING.md",
            "LICENSE", "MANIFEST.in", "Makefile", "README.md",
            "pyproject.toml", "setup.py", "setup.cfg", ".gitignore",
            ".flake8", ".editorconfig", ".gitattributes",
        }
        if path in NON_CODE_EXACT or Path(path).name in NON_CODE_EXACT:
            return True
        return any(path.startswith(prefix) for prefix in NON_CODE_PATTERNS)

    def select(self, patch):
        selected = 0
        files = list(diff_files(patch))
        for header, content in files:
            selected |= self.select_file(header, content)
            if selected == self.all_tests:
                break
        return selected if files else self.all_tests

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
    parser.add_argument("--classes", type=Path, default=HERE / "classes.json")
    args = parser.parse_args()
    tests = json.loads((args.data / "tests.json").read_text(encoding="utf-8"))["tests"]
    dependencies = json.loads(args.map.read_text(encoding="utf-8"))
    classes = json.loads(args.classes.read_text(encoding="utf-8")) if args.classes.exists() else {
        "tests": [], "classes": {},
    }
    selector = Selector(tests, dependencies, args.source, classes)

    @lru_cache(maxsize=128)
    def workers_for(selected):
        return schedule(tests, selected)

    for patch in sorted((args.data / "commits").glob("*.patch")):
        try:
            content = patch.read_text(encoding="utf-8")
            selected = selector.select(content)
        except (OSError, ValueError, SyntaxError, RecursionError):
            selected = selector.all_tests
        workers = workers_for(selected)
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))


if __name__ == "__main__":
    main()
