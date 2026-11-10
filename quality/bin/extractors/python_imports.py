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
  from plain, chained and annotated assignments, a parameter's default, and
  either side of a conditional or boolean expression; `global` / `nonlocal`
  bind where Python binds them (`nonlocal`: the nearest enclosing function that
  binds the name), the value read in the scope the assignment sits in. A name
  may hold whatever ANY of its bindings in the scope that owns it holds — order
  and reachability are not read, so a dead `if False:` rebinding, a later `del`
  or reassignment cannot hide the machinery (fail closed); a name whose every
  binding there is something else (a parameter without a default, `del`, a
  plain value) shadows the outer one. Aliases are solved as a fixed point,
  linear in the bindings, so no fan-out or cycle of them can stall the reader;
- `exec` / `eval` of a constant string, by parsing that string, whose names read
  on into the scope that called exec — nested up to
  EXEC_DEPTH strings deep; a file nesting deeper is reported as not read in full.

Out of scope, by design: a module name computed at run time (an f-string with a
variable, a name read from config), `getattr` with a computed attribute,
`exec`/`eval` of a computed string, `runpy.run_path`, import hooks, aliases
bound by a walrus (`(load := importlib.import_module)`), by unpacking
(`a, b = importlib, x`) or as a `for` / `with` / `except` target, names a star
import from importlib/runpy brings in, and comment / CRLF edge cases of the line
reader (it runs only on files Python cannot parse, which `unparseable: fail`
refuses anyway). A reader
that chased those would be an interpreter.

A file the parser refuses (a syntax error, a null byte, nesting past the
recursion limit) is read by a line reader instead — each `;`-separated statement
of each logical line — and reported as unparsed, so the caller can say so.
"""

import ast
import collections
import importlib.util
import io
import re
import tokenize
import warnings

# Callee name -> what it does with its first argument.
DYNAMIC = {"import_module": "import_module", "__import__": "__import__", "run_module": "run_module",
           "exec": "exec", "eval": "exec"}
# The modules a dynamic callee belongs to, and which of their names are one.
ALIASABLE = {"importlib": ("import_module", "__import__"), "builtins": ("__import__", "exec", "eval"), "runpy": ("run_module",)}
# Bare names that are dynamic without being imported: the builtins.
BUILTINS = {name: DYNAMIC[name] for name in ALIASABLE["builtins"]}
NAME_KEYWORDS = ("name", "mod_name", "source")
EXEC_DEPTH = 3


def read(path):
    """([(line, candidate specs)], parsed): every import in the file at `path`, and
    whether it was read in full (False: the line reader read it, or the parser left
    exec strings nested too deep unread). Raises OSError."""
    with open(path, "rb") as handle:
        data = handle.read()
    notes = []
    found = parse(data, 0, notes)
    if found is not None:
        return found, not notes
    return line_imports(decode(data)), False


def parse(source, depth=0, notes=None, outer=None):
    """[(line, candidate specs)] sorted by line, or None when `source` (bytes or str)
    does not parse. What could not be read in full is appended to `notes`; `outer` is
    the scope that exec'd `source`, whose bindings it reads on into."""
    notes = [] if notes is None else notes
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(source)
        calls = collect(tree, outer)
        found = [item for node in ast.walk(tree) for item in _static_imports(node)]
        found += [item for call, scope in calls for item in _call_imports(call, scope, depth, notes)]
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
EMPTY = frozenset()
# Every attribute name that is dynamic machinery on one of the modules.
MEMBERS = {member for members in ALIASABLE.values() for member in members}
# A relative level no file's package can be that deep: past it, the import is an
# ImportError at run time, so it names nothing (and the dots are never built).
MAX_LEVEL = 4096


class Scope:
    """One Python scope (module, function, lambda, class, comprehension) and what each
    name is bound to in it: ("module", name), ("function", dynamic kind), ("assign",
    value expression, the scope it is evaluated in) or SHADOW — a parameter, a `del`,
    any other binding. Reading is flow-insensitive and fails closed: a name may hold
    whatever any of its bindings in the scope that owns it holds, so one binding to the
    machinery anywhere in that scope makes a call through the name an import."""

    def __init__(self, parent=None, is_class=False, body=(), outer=None):
        self.parent, self.is_class, self.outer = parent, is_class, outer
        self.root = parent.root if parent else self
        self.entries, self.pending = {}, []
        self.scopes = [] if parent is None else None
        self.root.scopes.append(self)
        self.globals, self.nonlocals = _declared(body)

    def bind(self, name, entry):
        """Record a binding where Python makes it: a `global` name in the module scope, a
        `nonlocal` one once every scope is read (`home_pending`), any other here."""
        if name in self.globals:
            self.root.entries.setdefault(name, []).append(entry)
        elif name in self.nonlocals:
            self.pending.append((name, entry))
        else:
            self.entries.setdefault(name, []).append(entry)

    def nonlocal_home(self, name):
        """Where Python binds a `nonlocal name` declared here: the nearest enclosing
        function scope that binds the name itself — one declaring it nonlocal too passes
        it on, a class or the module is never it."""
        scope, nearest = self.parent, None
        while scope is not None and scope is not self.root:
            if not scope.is_class:
                nearest = nearest or scope
                if name in scope.entries:
                    return scope
            scope = scope.parent
        return nearest or self.root

    def owner_of(self, name):
        """The scope whose binding a reference to `name` here sees, else None. A class
        body's names are not visible in the functions nested in it; a `global` name is
        read from the module scope."""
        scope, here = self, True
        while scope is not None:
            found = scope._holds(name) if here or not scope.is_class else None
            if found is not None:
                return found or None
            scope, here = scope.parent, False
        return None

    def _holds(self, name):
        """This scope's answer for `name`: the scope that holds it, False when it is a
        `global` the module does not bind, None to keep looking outward."""
        if name in self.globals:
            return self.root if name in self.root.entries else False
        return self if name in self.entries and name not in self.nonlocals else None


def home_pending(root):
    """Move every binding made through `nonlocal` to the scope Python binds it in —
    homes found first, from what each scope binds itself, then moved."""
    homes = [(scope.nonlocal_home(name), name, entry) for scope in root.scopes for name, entry in scope.pending]
    for home, name, entry in homes:
        home.entries.setdefault(name, []).append(entry)


NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef,
                 ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _declared(body):
    """(global names, nonlocal names) a scope's own statements declare, nested scopes
    excluded — read before any binding, wherever in the body the declaration sits."""
    names = {ast.Global: set(), ast.Nonlocal: set()}
    stack = list(body)
    while stack:
        node = stack.pop()
        if type(node) in names:
            names[type(node)].update(node.names)
        elif not isinstance(node, NESTED_SCOPES):
            stack.extend(ast.iter_child_nodes(node))
    return names[ast.Global], names[ast.Nonlocal]


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
        scope.bind(target.id, ("assign", value, scope))
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


def _visit_declaration(node, scope, calls):
    """`global` / `nonlocal`: read up front by the scope (`_declared`), nothing to visit."""
    return []


def _visit_bound_name(node, scope, calls):
    """An except handler's or a match pattern's captured name shadows."""
    for name in (getattr(node, "name", None), getattr(node, "rest", None)):
        if name:
            scope.bind(name, SHADOW)
    return _children(node, scope, calls)


def _parameters(args):
    """([(parameter name, its default or None)], annotations): defaults pair with the
    last positional parameters, and with keyword-only ones one by one."""
    positional = args.posonlyargs + args.args
    defaults = [None] * (len(positional) - len(args.defaults)) + args.defaults
    pairs = list(zip(positional, defaults)) + list(zip(args.kwonlyargs, args.kw_defaults))
    pairs += [(a, None) for a in (args.vararg, args.kwarg) if a]
    return [(a.arg, default) for a, default in pairs], [a.annotation for a, _ in pairs if a.annotation]


def _outer_parts(node, annotations):
    """What a def or lambda evaluates in its enclosing scope: decorators, defaults, annotations."""
    returns = [node.returns] if getattr(node, "returns", None) else []
    defaults = node.args.defaults + [d for d in node.args.kw_defaults if d]
    return getattr(node, "decorator_list", []) + defaults + annotations + returns


def _visit_function(node, scope, calls):
    """Decorators, defaults and annotations run in the enclosing scope; parameters and
    the body are the function's own. A parameter may hold its default, which is
    evaluated where the def is: `def f(il=importlib)` makes `il` the module."""
    if not isinstance(node, ast.Lambda):
        scope.bind(node.name, SHADOW)
    params, annotations = _parameters(node.args)
    outer = _outer_parts(node, annotations)
    body = node.body if isinstance(node.body, list) else [node.body]
    inner = Scope(scope, body=body)
    for name, default in params:
        inner.bind(name, SHADOW if default is None else ("assign", default, scope))
    return [(n, scope) for n in outer] + [(n, inner) for n in body]


def _visit_class(node, scope, calls):
    scope.bind(node.name, SHADOW)
    inner = Scope(scope, is_class=True, body=node.body)
    outer = node.decorator_list + node.bases + [k.value for k in node.keywords]
    return [(n, scope) for n in outer] + [(n, inner) for n in node.body]


def _visit_comprehension(node, scope, calls):
    return _children(node, Scope(scope), calls)


VISITORS = {
    ast.Call: _visit_call, ast.Name: _visit_name, ast.Import: _visit_import, ast.ImportFrom: _visit_import_from,
    ast.Assign: _visit_assign, ast.AnnAssign: _visit_ann_assign, ast.Global: _visit_declaration, ast.Nonlocal: _visit_declaration,
    ast.ExceptHandler: _visit_bound_name, ast.MatchAs: _visit_bound_name, ast.MatchStar: _visit_bound_name,
    ast.MatchMapping: _visit_bound_name, ast.FunctionDef: _visit_function, ast.AsyncFunctionDef: _visit_function,
    ast.Lambda: _visit_function, ast.ClassDef: _visit_class, ast.ListComp: _visit_comprehension,
    ast.SetComp: _visit_comprehension, ast.DictComp: _visit_comprehension, ast.GeneratorExp: _visit_comprehension,
}


def collect(tree, outer=None):
    """[(call, scope)] for every call in `tree`, with every scope's bindings recorded and
    solved — iteratively, so a deeply nested expression cannot exhaust the recursion
    limit. `outer`: the scope that exec'd this code, read on into past its module."""
    root = Scope(outer=outer)
    calls, stack = [], [(tree, root)]
    while stack:
        node, scope = stack.pop()
        stack.extend(VISITORS.get(type(node), _children)(node, scope, calls))
    home_pending(root)
    solve(root)
    return calls


# ---------------------------------------------------------------- what a name may hold

def solve(root):
    """Every binding's possible values, into `root.values` {(scope, name): frozenset}: a
    fixed point, grown from nothing. Each binding is read once, then again only when a
    name its value reads grows; a value set only grows and holds at most every module
    and function there is, so the work is linear in the bindings, whatever cycles or
    fan-out the aliases make."""
    values = collections.ChainMap({}, root.outer.root.values) if root.outer else {}
    work = [((scope, name), entry) for scope in root.scopes for name, entries in scope.entries.items() for entry in entries]
    readers = _readers(work, values)
    root.values = values
    while work:
        node, entry = work.pop()
        found = _entry_values(entry, values)
        if not found <= values[node]:
            values[node] = values[node] | found
            work.extend(readers.get(node, ()))


def _readers(work, values):
    """{(scope, name): the bindings whose value reads it}, every binding's node in
    `values` starting from nothing."""
    readers = {}
    for node, entry in work:
        values[node] = EMPTY
        for read in _reads(entry[1], entry[2]) if entry[0] == "assign" else ():
            readers.setdefault(read, []).append((node, entry))
    return readers


def _entry_values(entry, values):
    """What one binding may hold, given what every binding may hold so far."""
    if entry[0] == "assign":
        return evaluate(entry[1], entry[2], values)
    return EMPTY if entry is SHADOW else frozenset([entry])


def _accessed(node):
    """(expression, attribute) for `x.attr` and `getattr(x, "attr")`, else (None, None)."""
    if isinstance(node, ast.Attribute):
        return node.value, node.attr
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2:
        return node.args[0], const_str(node.args[1])
    return None, None


def _alternatives(node):
    """The expressions a conditional or boolean expression may evaluate to, else None."""
    if isinstance(node, ast.IfExp):
        return [node.body, node.orelse]
    if isinstance(node, ast.BoolOp):
        return node.values
    return None


def _walk_value(node, scope, on_name, on_member):
    """Fold the expression `node` through the forms a value is read in: a name, a member
    of the machinery's modules (`x.attr`, `getattr(x, "attr")`), either side of a
    conditional or boolean expression. Anything else holds nothing."""
    if isinstance(node, ast.Name):
        return on_name(node.id, scope)
    options = _alternatives(node)
    if options is not None:
        return frozenset().union(*(_walk_value(n, scope, on_name, on_member) for n in options))
    target, attr = _accessed(node)
    if attr not in MEMBERS:
        return EMPTY
    return on_member(_walk_value(target, scope, on_name, on_member), attr)


def _bindings(name, scope):
    """([(scope, name)] a read of `name` in `scope` may see, whether the builtin may be
    seen). A function's own binding (or a closure's) is the answer. Anything else is a
    name lookup — a class body that binds the name, the module, no binding at all —
    which on a miss goes to the module and then the builtins, never to an enclosing
    function; for exec'd code, on into the scope that exec'd it (`_looked_up`)."""
    owner = scope.owner_of(name)
    if owner is not None and not owner.is_class and owner is not owner.root:
        return [(owner, name)], False
    return ([(owner, name)] if owner is not None else []) + _looked_up(name, scope.root), True


def _looked_up(name, root):
    """[(scope, name)] a name lookup that misses reaches from the module `root`: its own
    binding and, when `root` is exec'd code, every binding along the chain of the scope
    that exec'd it — its locals, any closure it may hold, its module — and so on out.
    Which of those the run reads is not knowable from the text, so all of them count."""
    found, scope = [], root
    while scope is not None:
        if name in scope.entries:
            found.append((scope, name))
        scope = scope.parent if scope.parent is not None else scope.outer
    return found


def _reads(node, scope):
    """The bindings {(scope, name)} the value of `node` in `scope` depends on."""
    return _walk_value(node, scope, lambda name, where: frozenset(_bindings(name, where)[0]), lambda reads, _attr: reads)


def evaluate(node, scope, values):
    """What the expression `node` may evaluate to: a frozenset of ("module", m) and
    ("function", kind), read from `values`."""
    def on_name(name, where):
        nodes, builtin = _bindings(name, where)
        found = frozenset().union(*(values.get(n, EMPTY) for n in nodes))
        return found | frozenset([("function", BUILTINS[name])]) if builtin and name in BUILTINS else found
    return _walk_value(node, scope, on_name, _members)


def _members(found, attr):
    """The machinery `attr` names on any module in `found`."""
    return frozenset(("function", DYNAMIC[attr]) for kind, module in found
                     if kind == "module" and attr in ALIASABLE.get(module, ()))


def callee_kinds(func, scope):
    """Every dynamic kind a call of `func` in `scope` may be, sorted."""
    return sorted(value[1] for value in evaluate(func, scope, scope.root.values) if value[0] == "function")


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


def _call_imports(call, scope, depth, notes):
    """[(line, candidate specs)] the call imports, under every kind its callee may be."""
    return [item for kind in callee_kinds(call.func, scope) for item in _kind_imports(call, kind, scope, depth, notes)]


def _kind_imports(call, kind, scope, depth, notes):
    name = const_str(_arg(call, 0, NAME_KEYWORDS))
    if name is None or (not name and kind != "__import__"):
        return []
    if kind == "exec":
        return _exec_imports(call.lineno, name, scope, depth, notes)
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
    fromlist entry as a submodule of `name` first, else `name` itself. A relative one
    may name nothing but its level: `__import__("", fromlist=["x"], level=1)`. A level
    past MAX_LEVEL names nothing (Python raises), and its dots are never built."""
    level = _const_int(_arg(call, 4, ("level",)))
    if _names_nothing(name, level):
        return []
    base, joiner = "." * level + name, "." if name else ""
    specs = [[base + joiner + entry, base] for entry in _const_strs(_arg(call, 3, ("fromlist",))) if entry and entry != "*"]
    return specs or [[base]]


def _names_nothing(name, level):
    """Whether `__import__(name, level=level)` cannot name a module: no name and no
    level, or a level deeper than any package (Python raises)."""
    return level > MAX_LEVEL or not (name or level)


def _const_int(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, int) and node.value > 0 else 0


def _const_strs(node):
    return [const_str(e) for e in node.elts] if isinstance(node, (ast.List, ast.Tuple)) else []


def _exec_imports(line, code, scope, depth, notes):
    """The imports in a constant string handed to exec/eval in `scope` — the string reads
    that scope's names — reported at the call's line. Past EXEC_DEPTH nested strings are
    not read, and `notes` says so: the file is then reported as not read in full."""
    if depth >= EXEC_DEPTH:
        notes.append("exec nested past %d levels" % EXEC_DEPTH)
        return []
    return [(line, specs) for _inner, specs in parse(code, depth + 1, notes, scope) or []]


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
