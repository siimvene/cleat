"""python_imports — every module a Python file imports, read through its parser.

The file is parsed from its bytes, so a BOM and a PEP 263 `coding:` cookie are
honoured exactly as Python honours them: a `# coding: utf-7` file whose comment
decodes into a real import line is read as that import. Read are:

- `import a.b, c as d` — every alias; `from pkg import name` — the submodule
  `pkg.name` first, then `pkg`; relative `from ..x import y` by its level;
- calls of `importlib.import_module` (with `package`, positional or keyword, for
  a relative name), `__import__` (with `fromlist` and `level`) and
  `runpy.run_module`, positional or by keyword, by any name the file binds them
  to (`import importlib as il`, `from importlib import import_module as im`,
  `im = importlib.import_module`, `getattr(importlib, "import_module")`), whose
  module name is a constant string or a concatenation of constant strings. Only
  the real function counts: `<name bound to importlib>.import_module(...)`, a
  bare name the file imported from importlib/runpy, the builtin `__import__` —
  a method of any other object that shares the name is not an import;
- `exec` / `eval` of a constant string, by parsing that string.

Out of scope, by design: a module name computed at run time (an f-string with a
variable, a name read from config), `getattr` with a computed attribute,
`exec`/`eval` of a computed string, `runpy.run_path`, and import hooks. A reader
that chased those would be an interpreter.

A file the parser refuses (a syntax error, a null byte, nesting past the
recursion limit) is read by a line reader instead — each `;`-separated statement
of each logical line — and reported as unparsed, so the caller can say so.
"""

import ast
import importlib.util
import io
import re
import tokenize
import warnings

# Callee name -> what it does with its first argument.
DYNAMIC = {"import_module": "import_module", "__import__": "__import__", "run_module": "run_module",
           "exec": "exec", "eval": "exec"}
# The modules a dynamic callee belongs to, and which of their names are one.
ALIASABLE = {"importlib": ("import_module",), "builtins": ("__import__", "exec", "eval"), "runpy": ("run_module",)}
# Bare names that are dynamic without being imported: the builtins.
BUILTINS = {name: DYNAMIC[name] for name in ALIASABLE["builtins"]}
NAME_KEYWORDS = ("name", "mod_name", "source")
EXEC_DEPTH = 3


def read(path):
    """([(line, candidate specs)], parsed): every import in the file at `path`, and
    whether the parser read it (False: the line reader did). Raises OSError."""
    with open(path, "rb") as handle:
        data = handle.read()
    found = parse(data)
    if found is not None:
        return found, True
    return line_imports(decode(data)), False


def parse(source, depth=0):
    """[(line, candidate specs)] sorted by line, or None when `source` (bytes or str)
    does not parse."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(source)
        bindings = _bindings(tree)
        found = [item for node in ast.walk(tree) for item in _node_imports(node, bindings, depth)]
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    return sorted(found, key=lambda item: item[0])


def decode(data):
    """The text of `data` as Python would decode it, replacing what it cannot."""
    try:
        encoding = tokenize.detect_encoding(io.BytesIO(data).readline)[0]
        return data.decode(encoding, errors="replace")
    except (SyntaxError, LookupError, UnicodeError):
        return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- statements

def _from_import(node):
    """[(line, candidate specs)] per name of one `from ... import ...`: the submodule
    `pkg.name` first (not for `*`), then the package itself."""
    base = "." * node.level + (node.module or "")
    joiner = "." if node.module else ""
    return [(node.lineno, ([] if alias.name == "*" else [base + joiner + alias.name]) + [base]) for alias in node.names]


def _node_imports(node, bindings, depth):
    """[(line, candidate specs)] one parsed node contributes."""
    if isinstance(node, ast.Import):
        return [(node.lineno, [alias.name]) for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return _from_import(node)
    if isinstance(node, ast.Call):
        return _call_imports(node, bindings, depth)
    return []


# ---------------------------------------------------------------- dynamic imports

class Bindings:
    """What a file binds the dynamic-import machinery to: `modules` maps a local name to
    the module it is (`import importlib as il` -> il: importlib), `functions` a local
    name to the dynamic kind it calls (`from importlib import import_module as im`)."""

    def __init__(self):
        self.modules, self.functions = {}, {}


    def bind_import(self, node):
        """`import importlib` / `import importlib.util` bind importlib; `import x.y as z` binds x.y."""
        for alias in node.names:
            head = alias.name.split(".")[0]
            if head in ALIASABLE:
                self.modules[alias.asname or head] = alias.name if alias.asname else head

    def bind_from(self, node):
        """`from importlib import import_module [as im]` binds the function."""
        if node.module in ALIASABLE and not node.level:
            self.functions.update((a.asname or a.name, DYNAMIC[a.name]) for a in node.names if a.name in ALIASABLE[node.module])

    def bind_assign(self, node):
        """`load = importlib.import_module` binds `load` to what it names."""
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            kind = callee_kind(node.value, self)
            if kind:
                self.functions[node.targets[0].id] = kind


def _bindings(tree):
    bindings = Bindings()
    statements = {ast.Import: bindings.bind_import, ast.ImportFrom: bindings.bind_from}
    for node in ast.walk(tree):
        statements.get(type(node), lambda _node: None)(node)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            bindings.bind_assign(node)
    return bindings


def _member(module_node, attr, bindings):
    """The dynamic kind of `attr` on `module_node` when that is a name bound to the
    module defining it (importlib.import_module, builtins.__import__), else None."""
    module = bindings.modules.get(module_node.id) if isinstance(module_node, ast.Name) else None
    return DYNAMIC[attr] if module in ALIASABLE and attr in ALIASABLE[module] else None


def callee_kind(func, bindings):
    """The dynamic kind `func` names — a name the file bound to one, a builtin, a
    bound module's member, or `getattr(<bound module>, "<member>")` — else None."""
    if isinstance(func, ast.Name):
        return bindings.functions.get(func.id) or BUILTINS.get(func.id)
    if isinstance(func, ast.Attribute):
        return _member(func.value, func.attr, bindings)
    if isinstance(func, ast.Call) and isinstance(func.func, ast.Name) and func.func.id == "getattr" and len(func.args) >= 2:
        return _member(func.args[0], const_str(func.args[1]), bindings)
    return None


def _joined(parts):
    return None if None in parts else "".join(parts)


def const_str(node):
    """The string `node` always evaluates to — a constant, a `+` of constants, an
    f-string without placeholders — else None."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _joined([const_str(node.left), const_str(node.right)])
    if isinstance(node, ast.JoinedStr):
        return _joined([const_str(value) for value in node.values])
    return None


def _arg(call, index, keywords):
    """The call's positional argument `index`, else its keyword argument by one of `keywords`."""
    positional = [a for a in call.args if not isinstance(a, ast.Starred)]
    if len(positional) > index:
        return positional[index]
    return next((k.value for k in call.keywords if k.arg in keywords), None)


def _call_imports(call, bindings, depth):
    kind = callee_kind(call.func, bindings)
    name = const_str(_arg(call, 0, NAME_KEYWORDS)) if kind else None
    if not name:
        return []
    if kind == "exec":
        return _exec_imports(call.lineno, name, depth)
    if kind == "__import__":
        return [(call.lineno, specs) for specs in _dunder_specs(call, name)]
    return [(call.lineno, [_absolute(name, const_str(_arg(call, 1, ("package",))))])]


def _absolute(name, package):
    """`name` made absolute against `package` the way import_module does, when it is
    relative and the package is known; else as written."""
    if not name.startswith(".") or not package:
        return name
    try:
        return importlib.util.resolve_name(name, package)
    except (ImportError, ValueError):
        return name


def _dunder_specs(call, name):
    """Candidate specs per `__import__(name, globals, locals, fromlist, level)`: each
    fromlist entry as a submodule of `name` first, else `name` itself."""
    base = "." * _const_int(_arg(call, 4, ("level",))) + name
    specs = [[base + "." + entry, base] for entry in _const_strs(_arg(call, 3, ("fromlist",))) if entry and entry != "*"]
    return specs or [[base]]


def _const_int(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, int) and node.value > 0 else 0


def _const_strs(node):
    return [const_str(e) for e in node.elts] if isinstance(node, (ast.List, ast.Tuple)) else []


def _exec_imports(line, code, depth):
    """The imports in a constant string handed to exec/eval, reported at the call's line."""
    found = parse(code, depth + 1) if depth < EXEC_DEPTH else None
    return [(line, specs) for _inner, specs in found or []]


# ---------------------------------------------------------------- line reader

FROM_RE = re.compile(r"^from\s+(\.*[\w.]*)\s+import\b(.*)$")
IMPORT_RE = re.compile(r"^import\s+(.+)$")
NAME_RE = re.compile(r"(\w+|\*)(?:\s+as\s+\w+)?")


def _logical_lines(text):
    """(first line number, text) per logical line, backslash continuations joined."""
    start, parts = None, []
    for number, line in enumerate(text.split("\n"), 1):
        start = number if start is None else start
        if line.endswith("\\"):
            parts.append(line[:-1])
            continue
        parts.append(line)
        yield start, " ".join(parts)
        start, parts = None, []


def _from_statement(match):
    base = match.group(1)
    joiner = "" if base.endswith(".") else "."
    names = [n for n in NAME_RE.findall(match.group(2).strip().strip("()")) if n != "*"]
    return [[base + joiner + n, base] for n in names] or [[base]]


def _import_statement(match):
    return [[part.split()[0]] for part in match.group(1).split(",") if part.split()]


def _statement_imports(statement):
    """[candidate specs] per module one statement imports."""
    statement = statement.split("#")[0].strip()
    match = FROM_RE.match(statement)
    if match:
        return _from_statement(match)
    match = IMPORT_RE.match(statement)
    return _import_statement(match) if match else []


def line_imports(text):
    """[(line, candidate specs)] read statement by statement, for a file that does not parse."""
    return [(number, specs) for number, line in _logical_lines(text)
            for statement in line.split(";") for specs in _statement_imports(statement)]
