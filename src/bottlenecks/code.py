"""Static (AST based) bottleneck detection for Python source code.

Rules
-----
* ``nested-loops``      loops nested >= 2 deep (polynomial work)
* ``io-in-loop``        file / network / subprocess / DB / sleep calls inside a loop
* ``str-concat-in-loop`` ``s += "..."`` string building inside a loop
* ``regex-in-loop``     ``re.*`` compilation/matching with a literal pattern inside a loop
* ``sort-in-loop``      ``sorted()`` / ``.sort()`` inside a loop
* ``high-complexity``   cyclomatic complexity above threshold
* ``long-function``     function body longer than threshold lines
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Sequence, Union

from .models import Bottleneck, Report

IO_CALLS = {
    "open", "input", "print",
    "time.sleep", "sleep",
    "subprocess.run", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output", "subprocess.Popen", "os.system", "os.popen",
    "urllib.request.urlopen", "urlopen",
    "socket.create_connection",
}
IO_PREFIXES = ("requests.", "httpx.", "aiohttp.", "urllib.", "boto3.", "sqlite3.", "psycopg", "pymysql.")
IO_METHODS = {"execute", "executemany", "fetchall", "fetchone", "commit", "query",
              "read_text", "write_text", "read_bytes", "write_bytes", "urlopen",
              "get", "post", "put", "delete", "patch", "request", "send", "recv"}
IO_METHOD_RECEIVERS = {"requests", "httpx", "session", "client", "conn", "connection",
                       "cursor", "cur", "db", "sock", "socket", "s3", "http"}
REGEX_FUNCS = {"compile", "match", "search", "findall", "finditer", "sub", "split", "fullmatch"}
DEFAULT_EXCLUDES = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", "build",
                    "dist", ".tox", ".mypy_cache", ".pytest_cache", ".worktrees", "site-packages"}

LOOP_TYPES = (ast.For, ast.AsyncFor, ast.While, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _dotted(node: ast.AST) -> str:
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif isinstance(node, ast.Call):
        parts.append(_dotted(node.func) + "()")
    return ".".join(reversed(parts))


def _is_io_call(call: ast.Call) -> Optional[str]:
    name = _dotted(call.func)
    if not name:
        return None
    if name in IO_CALLS or name.startswith(IO_PREFIXES):
        return name
    if isinstance(call.func, ast.Attribute) and call.func.attr in IO_METHODS:
        receiver = _dotted(call.func.value).split(".")[-1].lower().rstrip("()")
        if receiver in IO_METHOD_RECEIVERS or receiver.endswith(("session", "client", "cursor", "conn")):
            return name
    return None


def cyclomatic_complexity(func: ast.AST) -> int:
    """McCabe-style complexity of a single function (nested defs excluded)."""
    complexity = 1
    for node in _walk_own(func):
        if isinstance(node, (ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While,
                             ast.ExceptHandler, ast.Assert)):
            complexity += 1
        elif isinstance(node, ast.BoolOp):
            complexity += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            complexity += 1 + len(node.ifs)
        elif hasattr(ast, "match_case") and isinstance(node, getattr(ast, "match_case")):
            complexity += 1
    return complexity


def _walk_own(func: ast.AST) -> Iterator[ast.AST]:
    """Walk a function body without descending into nested functions/classes."""
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, FUNC_TYPES + (ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str, complexity_threshold: int, length_threshold: int) -> None:
        self.path = path
        self.complexity_threshold = complexity_threshold
        self.length_threshold = length_threshold
        self.loop_depth = 0
        self.func_stack: List[str] = []
        self.items: List[Bottleneck] = []
        self._reported_nesting: set = set()

    # helpers -----------------------------------------------------------
    def _where(self, node: ast.AST) -> str:
        func = self.func_stack[-1] if self.func_stack else "<module>"
        return f"{self.path}:{getattr(node, 'lineno', 0)} ({func})"

    def _add(self, node: ast.AST, kind: str, message: str, score: float, suggestion: str, **metrics) -> None:
        self.items.append(Bottleneck("code", kind, self._where(node), message, score, suggestion=suggestion,
                                     metrics=dict(metrics, loop_depth=self.loop_depth)))

    # functions ---------------------------------------------------------
    def _visit_func(self, node: ast.AST) -> None:
        name = getattr(node, "name", "<lambda>")
        cc = cyclomatic_complexity(node)
        if cc > self.complexity_threshold:
            score = min(1.0, 0.3 + (cc - self.complexity_threshold) / (self.complexity_threshold * 3))
            self.items.append(Bottleneck(
                "code", "high-complexity", f"{self.path}:{node.lineno} ({name})",
                f"cyclomatic complexity {cc} > {self.complexity_threshold}", score,
                suggestion="split into smaller functions, use lookup tables / early returns",
                metrics={"complexity": cc}))
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        length = end - node.lineno + 1
        if length > self.length_threshold:
            score = min(0.7, 0.15 + (length - self.length_threshold) / (self.length_threshold * 4))
            self.items.append(Bottleneck(
                "code", "long-function", f"{self.path}:{node.lineno} ({name})",
                f"function is {length} lines long (> {self.length_threshold})", score,
                suggestion="extract cohesive blocks into helpers", metrics={"lines": length}))
        saved_depth, self.loop_depth = self.loop_depth, 0
        self.func_stack.append(name)
        self.generic_visit(node)
        self.func_stack.pop()
        self.loop_depth = saved_depth

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    # loops -------------------------------------------------------------
    def _visit_loop(self, node: ast.AST) -> None:
        self.loop_depth += 1
        if self.loop_depth >= 2:
            key = (self.func_stack[-1] if self.func_stack else "<module>", getattr(node, "lineno", 0))
            if key not in self._reported_nesting:
                self._reported_nesting.add(key)
                score = min(1.0, 0.25 + 0.2 * (self.loop_depth - 1))
                self._add(node, "nested-loops", f"loop nested {self.loop_depth} levels deep (O(n^{self.loop_depth}))",
                          score, "index with dict/set, pre-group data, vectorise or use itertools/joins")
        self.generic_visit(node)
        self.loop_depth -= 1

    visit_For = _visit_loop
    visit_AsyncFor = _visit_loop
    visit_While = _visit_loop
    visit_ListComp = _visit_loop
    visit_SetComp = _visit_loop
    visit_DictComp = _visit_loop
    visit_GeneratorExp = _visit_loop

    # statements inside loops ------------------------------------------
    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        is_str = isinstance(node.value, ast.JoinedStr) or (
            isinstance(node.value, ast.Constant) and isinstance(node.value.value, str))
        if self.loop_depth and isinstance(node.op, ast.Add) and is_str:
            self._add(node, "str-concat-in-loop", "string concatenation with += inside a loop", 0.3,
                      "collect parts in a list and ''.join() them once")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self.loop_depth:
            io_name = _is_io_call(node)
            if io_name and io_name != "print":
                score = min(1.0, 0.45 + 0.15 * (self.loop_depth - 1))
                self._add(node, "io-in-loop", f"blocking I/O call '{io_name}' inside a loop", score,
                          "batch the operation, cache results, or run concurrently (asyncio/threads)",
                          call=io_name)
            name = _dotted(node.func)
            if name.startswith("re.") and name.split(".")[-1] in REGEX_FUNCS and node.args \
                    and isinstance(node.args[0], ast.Constant):
                self._add(node, "regex-in-loop", f"'{name}' with a literal pattern inside a loop", 0.2,
                          "pre-compile the pattern once outside the loop", call=name)
            if name == "sorted" or (isinstance(node.func, ast.Attribute) and node.func.attr == "sort"):
                self._add(node, "sort-in-loop", "sorting inside a loop", 0.35,
                          "sort once outside the loop or use heapq/bisect", call=name)
        self.generic_visit(node)


def analyze_source(source: str, path: str = "<string>", complexity_threshold: int = 10,
                   length_threshold: int = 80) -> List[Bottleneck]:
    """Analyse a single Python source string."""
    tree = ast.parse(source, filename=path)
    visitor = _Visitor(path, complexity_threshold, length_threshold)
    visitor.visit(tree)
    return visitor.items


def iter_python_files(paths: Sequence[Union[str, os.PathLike]], excludes: Iterable[str] = DEFAULT_EXCLUDES) -> Iterator[Path]:
    excludes = set(excludes)
    for raw in paths:
        p = Path(raw)
        if p.is_file() and p.suffix == ".py":
            yield p
        elif p.is_dir():
            for root, dirs, files in os.walk(p):
                dirs[:] = sorted(d for d in dirs if d not in excludes and not d.endswith(".egg-info"))
                for f in sorted(files):
                    if f.endswith(".py"):
                        yield Path(root) / f


def analyze_code(paths: Union[str, os.PathLike, Sequence[Union[str, os.PathLike]]],
                 complexity_threshold: int = 10, length_threshold: int = 80) -> Report:
    """Scan files/directories and return a :class:`Report` of code bottlenecks."""
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    report = Report()
    files = errors = 0
    for file in iter_python_files(paths):
        files += 1
        try:
            source = file.read_text(encoding="utf-8", errors="replace")
            report.extend(analyze_source(source, str(file), complexity_threshold, length_threshold))
        except (SyntaxError, ValueError):
            errors += 1
    report.summary["code"] = {"files": files, "parse_errors": errors, "findings": len(report.items)}
    return report
