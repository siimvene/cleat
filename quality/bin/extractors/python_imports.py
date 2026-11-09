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
  a method of any other object that shares the name is not an import. Bindings
  are per scope (module, function, lambda, class, comprehension), aliases come
  from plain, chained and annotated assignments, and a parameter, `del` or other
  binding of the name in a scope shadows it there;
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
        calls = collect(tree)
        found = [item for node in ast.walk(tree) for item in _static_imports(node)]
        found += [item for call, scope in calls for item in _call_imports(call, scope, depth)]
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


def _static_imports(node):
    """[(line, candidate specs)] one import statement contributes."""
    if isinstance(node, ast.Import):
        return [(node.lineno, [alias.name]) for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return _from_import(node)
    return []


# ---------------------------------------------------------------- dynamic imports

SHADOW = ("shadow", None)
MAX_ALIAS_DEPTH = 20


class Scope:
    """One Python scope (module, function, lambda, class, comprehension) and what each
    name is bound to in it: ("module", name), ("function", dynamic kind), ("assign",
    value expression) or SHADOW — a parameter, a `del`, any other binding. Reading is
    flow-insensitive: a name with any binding that is not the dynamic machinery is
    shadowed in its whole scope, as Python makes it local to the whole scope."""

    def __init__(self, parent=None, is_class=False):
        self.parent, self.is_class = parent, is_class
        self.entries, self.outer = {}, set()

    def bind(self, name, entry):
        self.entries.setdefault(name, []).append(entry)

    def owner_of(self, name):
        """The scope whose binding a reference to `name` here sees, else None. A class
        body's names are not visible in the functions nested in it."""
        scope, here = self, True
        while scope is not None:
            if (here or not scope.is_class) and name in scope.entries and name not in scope.outer:
                return scope
            scope, here = scope.parent, False
        return None


def _children(node, scope, calls):
    return [(child, scope) for child in ast.iter_child_nodes(node)]


def _visit_call(node, scope, calls):
    calls.append((node, scope))
    return _children(node, scope, calls)


def _visit_name(node, scope, calls):
    if not isinstance(node.ctx, ast.Load):
        scope.bind(node.id, SHADOW)
    return []


def _visit_import(node, scope, calls):
    for alias in node.names:
        head = alias.name.split(".")[0]
        module = alias.name if alias.asname else head
        scope.bind(alias.asname or head, ("module", module) if module in ALIASABLE else SHADOW)
    return []


def _visit_import_from(node, scope, calls):
    dynamic = () if node.level else ALIASABLE.get(node.module, ())
    for alias in node.names:
        if alias.name != "*":
            scope.bind(alias.asname or alias.name, ("function", DYNAMIC[alias.name]) if alias.name in dynamic else SHADOW)
    return []


def _assign_target(target, value, scope):
    """Bind a plain-name target to its value expression; any other target is visited."""
    if isinstance(target, ast.Name):
        scope.bind(target.id, ("assign", value))
        return []
    return [(target, scope)]


def _visit_assign(node, scope, calls):
    """`a = b = importlib.import_module`: every plain-name target is an alias candidate."""
    pending = [(node.value, scope)]
    for target in node.targets:
        pending += _assign_target(target, node.value, scope)
    return pending


def _visit_ann_assign(node, scope, calls):
    """`load: Callable = importlib.import_module` binds like an assignment; without a value
    the name is still local to the scope."""
    pending = [(node.annotation, scope)] + ([(node.value, scope)] if node.value is not None else [])
    if node.value is None:
        return pending + [(node.target, scope)]
    return pending + _assign_target(node.target, node.value, scope)


def _visit_outer(node, scope, calls):
    scope.outer.update(node.names)
    return []


def _visit_bound_name(node, scope, calls):
    """An except handler's or a match pattern's captured name shadows."""
    for name in (getattr(node, "name", None), getattr(node, "rest", None)):
        if name:
            scope.bind(name, SHADOW)
    return _children(node, scope, calls)


def _parameters(args):
    every = args.posonlyargs + args.args + args.kwonlyargs + [a for a in (args.vararg, args.kwarg) if a]
    return [a.arg for a in every], [a.annotation for a in every if a.annotation]


def _outer_parts(node, annotations):
    """What a def or lambda evaluates in its enclosing scope: decorators, defaults, annotations."""
    returns = [node.returns] if getattr(node, "returns", None) else []
    defaults = node.args.defaults + [d for d in node.args.kw_defaults if d]
    return getattr(node, "decorator_list", []) + defaults + annotations + returns


def _visit_function(node, scope, calls):
    """Decorators, defaults and annotations run in the enclosing scope; parameters and
    the body are the function's own."""
    if not isinstance(node, ast.Lambda):
        scope.bind(node.name, SHADOW)
    names, annotations = _parameters(node.args)
    outer = _outer_parts(node, annotations)
    inner = Scope(scope)
    for name in names:
        inner.bind(name, SHADOW)
    body = node.body if isinstance(node.body, list) else [node.body]
    return [(n, scope) for n in outer] + [(n, inner) for n in body]


def _visit_class(node, scope, calls):
    scope.bind(node.name, SHADOW)
    inner = Scope(scope, is_class=True)
    outer = node.decorator_list + node.bases + [k.value for k in node.keywords]
    return [(n, scope) for n in outer] + [(n, inner) for n in node.body]


def _visit_comprehension(node, scope, calls):
    return _children(node, Scope(scope), calls)


VISITORS = {
    ast.Call: _visit_call, ast.Name: _visit_name, ast.Import: _visit_import, ast.ImportFrom: _visit_import_from,
    ast.Assign: _visit_assign, ast.AnnAssign: _visit_ann_assign, ast.Global: _visit_outer, ast.Nonlocal: _visit_outer,
    ast.ExceptHandler: _visit_bound_name, ast.MatchAs: _visit_bound_name, ast.MatchStar: _visit_bound_name,
    ast.MatchMapping: _visit_bound_name, ast.FunctionDef: _visit_function, ast.AsyncFunctionDef: _visit_function,
    ast.Lambda: _visit_function, ast.ClassDef: _visit_class, ast.ListComp: _visit_comprehension,
    ast.SetComp: _visit_comprehension, ast.DictComp: _visit_comprehension, ast.GeneratorExp: _visit_comprehension,
}


def collect(tree):
    """[(call, scope)] for every call in `tree`, with every scope's bindings recorded —
    iteratively, so a deeply nested expression cannot exhaust the recursion limit."""
    calls, stack = [], [(tree, Scope())]
    while stack:
        node, scope = stack.pop()
        stack.extend(VISITORS.get(type(node), _children)(node, scope, calls))
    return calls


def resolve(name, scope, depth=0):
    """What `name` read in `scope` is — ("module", m), ("function", kind) — else None.
    Every binding the owning scope makes must agree, or the name is shadowed there."""
    owner = scope.owner_of(name)
    if owner is None:
        return ("function", BUILTINS[name]) if name in BUILTINS else None
    values = {_entry_value(entry, owner, depth) for entry in owner.entries[name]}
    return values.pop() if len(values) == 1 else None


def _entry_value(entry, scope, depth):
    if entry[0] != "assign":
        return None if entry is SHADOW else entry
    return callee_value(entry[1], scope, depth + 1) if depth < MAX_ALIAS_DEPTH else None


def _member(module_value, attr):
    """The function `attr` of a module value names, when it is dynamic machinery."""
    if module_value is None or module_value[0] != "module" or attr not in ALIASABLE.get(module_value[1], ()):
        return None
    return ("function", DYNAMIC[attr])


def callee_value(node, scope, depth=0):
    """What the expression `node` evaluates to, in the terms `resolve` uses: a name, a
    member of a bound module, or `getattr(<bound module>, "<member>")`."""
    if isinstance(node, ast.Name):
        return resolve(node.id, scope, depth)
    if isinstance(node, ast.Attribute):
        return _member(callee_value(node.value, scope, depth), node.attr)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2:
        return _member(callee_value(node.args[0], scope, depth), const_str(node.args[1]))
    return None


def callee_kind(func, scope):
    """The dynamic kind a call of `func` in `scope` is, else None."""
    value = callee_value(func, scope)
    return value[1] if value is not None and value[0] == "function" else None


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


def _call_imports(call, scope, depth):
    kind = callee_kind(call.func, scope)
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
