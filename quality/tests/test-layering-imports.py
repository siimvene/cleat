#!/usr/bin/env python3
"""test-layering-imports — assert check-layering.py reading references from imports.

Over throwaway Python, TypeScript, Rust, Go and Kotlin trees laid out in
layers: a lower layer importing a higher one fails naming the file, line and
import; the allowed direction passes; a relative, an aliased and a
package-style import all resolve; an import of something outside the judged
roots is ignored; a comment is not an import; an unknown language is refused.
Writes nothing outside a temporary directory.

  python3 quality/tests/test-layering-imports.py
"""
import json, os, shutil, subprocess, sys, tempfile
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from harness import Suite, write
SCRIPT = os.path.join(os.path.dirname(HERE), "bin", "check-layering.py")
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "bin"))
from extractors import references

suite = Suite("test-layering-imports"); check = suite.check



def config_for(tmp, root, language, exempt=()):
    path = os.path.join(tmp, "quality.json")
    write(path, json.dumps({"layering": {
        "references": "imports", "language": language, "skip_dirs": [],
        "allowed": {"models": [], "services": ["models"], "views": ["services", "models"], "App": None},
        "always_allowed": [], "roots": [{"root": os.path.relpath(root, tmp), "exempt": list(exempt)}]}}))
    return path


def run(config, *args):
    proc = subprocess.run([sys.executable, SCRIPT, "--config", config, *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


tmp = tempfile.mkdtemp(prefix="layering-imports-")
try:
    # ---- python: a model importing a service is the violation; the rest is allowed
    py = os.path.join(tmp, "py", "app")
    write(os.path.join(py, "models", "thing.py"), "from app.services.store import Store  # a model reaching up\n\nclass Thing:\n    pass\n")
    write(os.path.join(py, "services", "store.py"), "from ..models.thing import Thing\nimport json\n# from app.views.page import Page — a comment, not an import\n\nclass Store:\n    pass\n")
    write(os.path.join(py, "views", "page.py"), "from app.services import store\nfrom app.models.thing import Thing\n")
    write(os.path.join(py, "main.py"), "from app.views.page import x\n")
    config = config_for(tmp, py, "python")
    code, out = run(config)
    check("python: a model importing a service fails, naming the file, line and import", code == 1 and "models/thing.py:1 -> store (services/store.py)" in out, out)
    check("python: the allowed direction, a relative import and a package import all resolve, and a comment does not", "views/page.py" not in out and "services/store.py:" not in out, out)
    refs = references.import_references(py, [py], "python", set())
    # Five: `from app.services import store` reads as the submodule services/store.py even
    # though the package itself (no __init__.py) does not resolve.
    check("python: five references read — stdlib ignored, `from pkg import submodule` resolved to the submodule", len(refs) == 5 and all(r[3].startswith(py) for r in refs) and any(r[0].endswith("views/page.py") and r[3].endswith("services/store.py") for r in refs), str(refs))
    code, out = run(config_for(tmp, py, "python", exempt=[{"file": "models/thing.py", "name": "store", "reason": "legacy"}]))
    check("python: an exempt pair passes and is counted", code == 0 and "5 cross-file references obey the layering (1 exempt)" in out, out)

    # ---- typescript: relative and aliased specifiers, index files, .js extension on a .ts file
    ts = os.path.join(tmp, "ts", "src")
    write(os.path.join(ts, "models", "thing.ts"), "import { Store } from '../services/store';\nexport class Thing {}\n")
    write(os.path.join(ts, "services", "store.ts"), "import { Thing } from '@/models/thing.js';\nimport fs from 'fs';\nexport class Store {}\n")
    write(os.path.join(ts, "views", "index.ts"), "export * from '../services';\n")
    write(os.path.join(ts, "services", "index.ts"), "export { Store } from './store';\n")
    code, out = run(config_for(tmp, ts, "typescript"))
    check("typescript: a model importing a service fails", code == 1 and "models/thing.ts:1 -> store (services/store.ts)" in out, out)
    check("typescript: an alias, a .js suffix, an index file and a bare package resolve or are ignored as they should", "services/store.ts:" not in out and "views/index.ts" not in out, out)

    # ---- rust: crate:: paths and mod declarations
    rs = os.path.join(tmp, "rs")
    write(os.path.join(rs, "src", "models", "mod.rs"), "use crate::services::store::Store;\npub struct Thing;\n")
    write(os.path.join(rs, "src", "services", "store.rs"), "use crate::models::Thing;\nuse std::collections::HashMap;\npub struct Store;\n")
    write(os.path.join(rs, "src", "services", "mod.rs"), "pub mod store;\n")
    write(os.path.join(rs, "src", "main.rs"), "mod models;\nmod services;\n")
    rs_config = os.path.join(tmp, "quality.json")
    write(rs_config, json.dumps({"layering": {"references": "imports", "language": "rust", "skip_dirs": [],
        "allowed": {"models": [], "services": ["models"], "App": None}, "always_allowed": [],
        "roots": [{"root": "rs/src", "exempt": []}]}}))
    code, out = run(rs_config)
    check("rust: a model using a service fails", code == 1 and "models/mod.rs:1 -> Store (services/store.rs)" in out, out)
    check("rust: std is ignored and mod declarations resolve", "services/store.rs:" not in out and "main.rs" not in out, out)

    # ---- go: package paths resolved by suffix under the root
    go = os.path.join(tmp, "go")
    write(os.path.join(go, "models", "thing.go"), 'package models\n\nimport (\n\t"fmt"\n\t"example.com/app/services"\n)\n')
    write(os.path.join(go, "services", "store.go"), 'package services\n\nimport "example.com/app/models"\n')
    code, out = run(config_for(tmp, go, "go"))
    check("go: a model importing a service package fails, at the line inside the import block", code == 1 and "models/thing.go:5 -> services (services)" in out, out)

    # ---- kotlin: package-qualified imports, with the package prefix stripped
    kt = os.path.join(tmp, "kt")
    write(os.path.join(kt, "models", "Thing.kt"), "package com.acme.models\nimport com.acme.services.Store\nclass Thing\n")
    write(os.path.join(kt, "services", "Store.kt"), "package com.acme.services\nimport com.acme.models.Thing\nimport java.util.List\nclass Store\n")
    code, out = run(config_for(tmp, kt, "kotlin"))
    check("kotlin: a model importing a service fails", code == 1 and "models/Thing.kt:2 -> Store (services/Store.kt)" in out, out)

    code, out = run(config_for(tmp, kt, "cobol"))
    check("an unknown language is refused naming the known ones", code == 2 and "cobol" in out and "python" in out, out)

    # ---- the four holes a security pass found against a real adopter: a package `kvart`
    # with core below api, exemptions keyed by target, an undeclared layer, and import
    # forms a line regex does not read.
    def kv_config(root, exempt=(), allowed=None, **extra):
        path = os.path.join(tmp, "kv.json")
        section = {"references": "imports", "language": "python", "skip_dirs": [],
                   "allowed": allowed or {"core": [], "api": ["core"], "App": None},
                   "always_allowed": [], "roots": [{"root": os.path.relpath(root, tmp), "exempt": list(exempt)}]}
        section.update(extra)
        write(path, json.dumps({"layering": section}))
        return path

    # 1. An exemption carrying "target" exempts that import only: a second import with the
    #    same last segment but another target fails, and the hint prints the target.
    kv = os.path.join(tmp, "kv1", "kvart")
    write(os.path.join(kv, "api", "person.py"), "x = 1\n")
    write(os.path.join(kv, "api", "legacy", "person.py"), "y = 1\n")
    write(os.path.join(kv, "core", "models.py"), "from kvart.api.legacy.person import y\nfrom kvart.api.person import x\n")
    targeted = {"file": "core/models.py", "name": "person", "target": "api/legacy/person.py", "reason": "legacy"}
    code, out = run(kv_config(kv, exempt=[targeted]))
    check("exempt target: an import of another file with the same name is not covered by the entry",
          code == 1 and "core/models.py:2 -> person (api/person.py)" in out and "api/legacy/person.py" not in out.split("Move")[0], out)
    check("exempt target: the add-to-exempt hint carries the target",
          '"target": "api/person.py"' in out, out)
    write(os.path.join(kv, "core", "models.py"), "from kvart.api.legacy.person import y\n")
    code, out = run(kv_config(kv, exempt=[targeted]))
    check("exempt target: the targeted import alone passes and is counted", code == 0 and "(1 exempt)" in out, out)
    write(os.path.join(kv, "core", "models.py"), "from kvart.api.person import x\n")
    code, out = run(kv_config(kv, exempt=[targeted]))
    check("exempt target: an entry whose target is no longer imported is dead, named with its target",
          code == 1 and "('core/models.py', 'person', 'api/legacy/person.py')" in out, out)
    legacy = {"file": "core/models.py", "name": "person", "reason": "legacy"}
    code, out = run(kv_config(kv, exempt=[legacy]))
    check("exempt target: an entry without one still matches by file and name", code == 0 and "(1 exempt)" in out, out)

    # 2. A layer missing from `allowed`: unrestricted by default, own layer and
    #    always_allowed only under unknown_layers: deny, and the failure says why.
    kv = os.path.join(tmp, "kv2", "kvart")
    write(os.path.join(kv, "api", "auth.py"), "x = 1\n")
    write(os.path.join(kv, "core", "base.py"), "x = 1\n")
    write(os.path.join(kv, "billing", "ledger.py"), "from kvart.api.auth import x\nfrom kvart.billing.money import m\n")
    write(os.path.join(kv, "billing", "money.py"), "from kvart.core.base import x\n")
    write(os.path.join(kv, "main.py"), "from kvart.api.auth import x\n")
    code, out = run(kv_config(kv))
    check("unknown_layers: an undeclared layer is unrestricted by default", code == 0, out)
    code, out = run(kv_config(kv, unknown_layers="deny", always_allowed=["core"]))
    check("unknown_layers deny: an undeclared layer importing another layer fails, saying the layer is undeclared",
          code == 1 and "billing/ledger.py:1 -> auth (api/auth.py)" in out and "undeclared" in out, out)
    check("unknown_layers deny: its own layer and always_allowed ones stay reachable",
          "billing/ledger.py:2" not in out and "billing/money.py" not in out, out)
    check("unknown_layers deny: a declared App is judged by its declaration", "main.py" not in out, out)
    code, out = run(kv_config(kv, unknown_layers="deny", allowed={"core": [], "api": ["core"]}, always_allowed=["core"]))
    check("unknown_layers deny: App undeclared is a layer like any other",
          code == 1 and "main.py:1 -> auth (api/auth.py)" in out, out)
    code, out = run(kv_config(kv, unknown_layers="maybe"))
    check("unknown_layers: a value other than allow or deny is refused", code == 2 and "unknown_layers" in out, out)

    # 3. Import forms a line regex misses, read through the parser, each at its own line.
    kv = os.path.join(tmp, "kv3", "kvart")
    write(os.path.join(kv, "api", "auth.py"), "x = 1\n")
    write(os.path.join(kv, "api", "__init__.py"), "")
    write(os.path.join(kv, "core", "comma.py"), "import os, kvart.api.auth\n")
    write(os.path.join(kv, "core", "semicolon.py"), "import os; import kvart.api.auth\n")
    write(os.path.join(kv, "core", "backslash.py"), "import os\nfrom \\\n    kvart.api.auth import x\n")
    write(os.path.join(kv, "core", "dunder.py"), "m = __import__(\"kvart.api.auth\")\n")
    write(os.path.join(kv, "core", "importlib_.py"), "import importlib\n\nm = importlib.import_module(\"kvart.api.auth\")\n")
    code, out = run(kv_config(kv))
    for label, where in (("a comma-separated import", "core/comma.py:1"), ("an import after a semicolon", "core/semicolon.py:1"),
                         ("a backslash-continued from-import", "core/backslash.py:2"), ("__import__ of a constant", "core/dunder.py:1"),
                         ("importlib.import_module of a constant", "core/importlib_.py:3")):
        check("parser: %s is read" % label, code == 1 and "%s -> auth (api/auth.py)" % where in out, out)

    # ... while every form the regexes already read still is.
    kv = os.path.join(tmp, "kv4", "kvart")
    write(os.path.join(kv, "api", "__init__.py"), "")
    write(os.path.join(kv, "api", "auth.py"), "x = 1\n")
    write(os.path.join(kv, "core", "aliased.py"), "import kvart.api.auth as a\n")
    write(os.path.join(kv, "core", "paren.py"), "from kvart.api.auth import (\n    x,\n    y,\n)\n")
    write(os.path.join(kv, "core", "relative.py"), "from ..api.auth import x\n")
    write(os.path.join(kv, "core", "typing_.py"), "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from kvart.api.auth import x\n")
    write(os.path.join(kv, "core", "local.py"), "def f():\n    from kvart.api.auth import x\n    return x\n")
    write(os.path.join(kv, "core", "star.py"), "from kvart.api.auth import *\n")
    write(os.path.join(kv, "core", "package.py"), "from kvart import api\n")
    write(os.path.join(kv, "core", "submodule.py"), "from kvart.api import auth\n")
    write(os.path.join(kv, "core", "py2.py"), "print 'not python 3'\nfrom kvart.api.auth import x\n")
    code, out = run(kv_config(kv))
    for where in ("core/aliased.py:1 -> auth", "core/paren.py:1 -> auth", "core/relative.py:1 -> auth", "core/typing_.py:3 -> auth",
                  "core/local.py:2 -> auth", "core/star.py:1 -> auth", "core/package.py:1 -> api (api/__init__.py)",
                  "core/submodule.py:1 -> auth (api/auth.py)", "core/py2.py:2 -> auth"):
        check("parser: still read — %s" % where, code == 1 and where in out, out)

    # ---- second pass: encodings, dynamic imports, relays through always_allowed and symlinks.
    def write_bytes(path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)

    # 1. The file is parsed from its bytes, the way Python reads it: a utf-7 cookie that
    #    turns a comment into an import, a BOM, a latin-1 cookie. A file that still does
    #    not parse is named, and under `unparseable: fail` fails the check.
    kv = os.path.join(tmp, "kv5", "kvart")
    write(os.path.join(kv, "api", "auth.py"), "x = 1\n")
    body = b"x = 1; from kvart.api.auth import x\n"
    write_bytes(os.path.join(kv, "core", "utf7.py"), b"# coding: utf-7\n# +AAo-from kvart.api.auth import x\n")
    write_bytes(os.path.join(kv, "core", "bom.py"), b"\xef\xbb\xbf" + body)
    write_bytes(os.path.join(kv, "core", "latin1.py"), b"# coding: latin-1\ncaf\xe9 = 1\n" + body)
    code, out = run(kv_config(kv))
    # utf-7's `+AAo-` decodes to a newline, so Python itself places that import on line 3.
    for label, where in (("a utf-7 cookie decoding a comment into an import", "core/utf7.py:3"),
                         ("a BOM", "core/bom.py:1"), ("a latin-1 cookie", "core/latin1.py:3")):
        check("encoding: %s is read as Python reads it" % label, code == 1 and "%s -> auth (api/auth.py)" % where in out, out)
    check("encoding: none of them is reported unparsed", "WARN" not in out, out)
    kv = os.path.join(tmp, "kv6", "kvart")
    write(os.path.join(kv, "api", "auth.py"), "x = 1\n")
    write(os.path.join(kv, "core", "broken.py"), "import os, kvart.api.auth\ndef f(:\n    pass\n")
    write(os.path.join(kv, "core", "nulls.py"), "x = 1\n#\0\nimport os; from kvart.api import auth\n")
    write(os.path.join(kv, "core", "harmless.py"), "def f(:\n    pass\n")
    code, out = run(kv_config(kv), "--quiet")
    check("unparseable: a file the parser refuses is named, even under --quiet",
          "WARN: core/broken.py does not parse" in out and "WARN: core/nulls.py" in out and "WARN: core/harmless.py" in out, out)
    check("unparseable: its imports are still read, statement by statement",
          code == 1 and "core/broken.py:1 -> auth (api/auth.py)" in out and "core/nulls.py:3 -> auth (api/auth.py)" in out, out)
    os.remove(os.path.join(kv, "core", "broken.py")); os.remove(os.path.join(kv, "core", "nulls.py"))
    code, out = run(kv_config(kv))
    check("unparseable: by default a warning, not a failure", code == 0 and "WARN: core/harmless.py" in out, out)
    code, out = run(kv_config(kv, unparseable="fail"))
    check("unparseable: under \"fail\" the same file fails the check", code == 1 and "FAIL: core/harmless.py does not parse" in out, out)
    code, out = run(kv_config(kv, unparseable="ignore"))
    check("unparseable: a value other than warn or fail is refused", code == 2 and "unparseable" in out, out)

    # 2. Dynamic imports by any name the file binds them to, with a constant (or constant-
    #    concatenated) module name; a name computed at run time stays out of scope.
    kv = os.path.join(tmp, "kv7", "kvart")
    write(os.path.join(kv, "api", "auth.py"), "x = 1\n")
    forms = {
        "from_alias": "from importlib import import_module as im\nim('kvart.api.auth')\n",
        "assigned": "import importlib\nload = importlib.import_module\nload('kvart.api.auth')\n",
        "getattr_": "import importlib\ngetattr(importlib, 'import_module')('kvart.api.auth')\n",
        "concat": "import importlib\nimportlib.import_module('kvart.' + 'api.auth')\n",
        "kwarg": "import importlib\nimportlib.import_module(name='kvart.api.auth')\n",
        "package_arg": "import importlib\nimportlib.import_module('.auth', 'kvart.api')\n",
        # Relative to the caller's own package this would be core/auth.py (absent); the package argument wins.
        "package_kwarg": "import importlib\nimportlib.import_module('.auth', package='kvart.api')\n",
        "fromlist": "__import__('kvart.api', fromlist=['auth'])\n",
        "exec_": "exec('from kvart.api.auth import x')\n",
        "runpy_": "import runpy\nrunpy.run_module('kvart.api.auth')\n",
        "module_alias": "import importlib as il\nil.import_module('kvart.api.auth')\n",
        "dunder_kwarg": "__import__(name='kvart.api.auth')\n",
        "builtins_attr": "import builtins\nbuiltins.__import__('kvart.api.auth')\n",
    }
    for stem, text in forms.items():
        write(os.path.join(kv, "core", stem + ".py"), text)
    write(os.path.join(kv, "core", "computed.py"), "import importlib\np = 'api'\nimportlib.import_module(f'kvart.{p}.auth')\n")
    code, out = run(kv_config(kv))
    for stem, text in forms.items():
        line = text.count("\n")
        check("dynamic: %s is read" % stem, code == 1 and "core/%s.py:%d -> auth (api/auth.py)" % (stem, line) in out, out)
    check("dynamic: a module name computed at run time is out of scope", "core/computed.py" not in out, out)
    # Only importlib's (or builtins', runpy's) own function counts: a method of some other
    # object that happens to share the name is not an import, nor is an unbound bare name.
    kv = os.path.join(tmp, "kv7b", "kvart")
    write(os.path.join(kv, "api", "auth.py"), "x = 1\n")
    write(os.path.join(kv, "core", "lookalike.py"),
          "registry.import_module('kvart.api.auth')\nloader.__import__('kvart.api.auth')\nimport_module('kvart.api.auth')\n"
          "getattr(registry, 'import_module')('kvart.api.auth')\n")
    code, out = run(kv_config(kv))
    check("dynamic: a method merely named import_module / __import__ is not an import", code == 0, out)

    # 3. Under deny, an always_allowed layer that `allowed` does not list may not relay:
    #    core -> testing/shim -> api. Listed as null in `allowed`, it is unrestricted on purpose.
    kv = os.path.join(tmp, "kv8", "kvart")
    write(os.path.join(kv, "api", "auth.py"), "x = 1\n")
    write(os.path.join(kv, "core", "evil.py"), "from kvart.testing.shim import x\n")
    write(os.path.join(kv, "testing", "shim.py"), "from kvart.api.auth import x\n")
    code, out = run(kv_config(kv, unknown_layers="deny", always_allowed=["testing"]))
    check("deny: an always_allowed layer missing from allowed cannot relay an upward import",
          code == 1 and "testing/shim.py:1 -> auth (api/auth.py)" in out and "'testing' is undeclared" in out and "core/evil.py" not in out, out)
    code, out = run(kv_config(kv, unknown_layers="deny", always_allowed=["testing"],
                              allowed={"core": [], "api": ["core"], "testing": None, "App": None}))
    check("deny: an always_allowed layer allowed lists as null is unrestricted on purpose", code == 0, out)
    code, out = run(kv_config(kv, always_allowed=["testing"]))
    check("allow: an always_allowed layer missing from allowed stays unrestricted", code == 0, out)

    # 4. Layers come from where a file really is: a symlinked directory or file in core
    #    pointing into api is api, a file symlinked out of every judged root is a
    #    violation naming the link, and a source that is a symlink is judged where it points.
    kv = os.path.join(tmp, "kv9", "kvart")
    write(os.path.join(kv, "api", "auth.py"), "x = 1\n")
    write(os.path.join(kv, "api", "helper.py"), "from kvart.api.auth import x\n")
    write(os.path.join(tmp, "kv9", "outside", "far.py"), "x = 1\n")
    os.makedirs(os.path.join(kv, "core"))
    os.symlink(os.path.join("..", "api"), os.path.join(kv, "core", "apilink"))
    os.symlink(os.path.join("..", "api", "auth.py"), os.path.join(kv, "core", "authalias.py"))
    os.symlink(os.path.join("..", "..", "outside"), os.path.join(kv, "core", "farlink"))
    os.symlink(os.path.join("..", "api", "helper.py"), os.path.join(kv, "core", "helper_alias.py"))
    write(os.path.join(kv, "core", "evil.py"), "from kvart.core.apilink.auth import x\nfrom kvart.core.authalias import y\nfrom kvart.core.farlink.far import z\n")
    code, out = run(kv_config(kv))
    check("symlink: a directory link into another layer is that layer",
          code == 1 and "core/evil.py:1 -> auth (api/auth.py) — through the symlink core/apilink/auth.py" in out, out)
    check("symlink: a file link into another layer is that layer",
          "core/evil.py:2 -> authalias (api/auth.py) — through the symlink core/authalias.py" in out, out)
    check("symlink: a link resolving outside every judged root is a violation naming the link",
          "core/evil.py:3 -> far (../outside/far.py) — through the symlink core/farlink/far.py, which resolves outside every judged root" in out, out)
    check("symlink: a source that is a link is judged in the layer it points into", "core/helper_alias.py" not in out, out)

    # ---- third pass: alias bindings are per scope, aliases come from every assignment
    # form, and an out-of-root symlink is judged by where it really points.
    kv = os.path.join(tmp, "kv10", "kvart")
    write(os.path.join(kv, "api", "auth.py"), "x = 1\n")
    shadowed = {
        "param": "import importlib\ndef f(importlib):\n    importlib.import_module('kvart.api.auth')\n",
        "local": "import importlib\ndef f():\n    importlib = registry\n    importlib.import_module('kvart.api.auth')\n",
        "deleted": "from importlib import import_module as load\ndef f():\n    del load\n    load('kvart.api.auth')\n",
        "lambda_": "import importlib\nf = lambda importlib: importlib.import_module('kvart.api.auth')\n",
        "builtin": "def f(__import__):\n    __import__('kvart.api.auth')\n",
        "comp_target": "import importlib\nx = [importlib.import_module('kvart.api.auth') for importlib in registries]\n",
    }
    seen = {
        "nested": ("import importlib\ndef f():\n    importlib.import_module('kvart.api.auth')\n", 3),
        "class_body": ("import importlib\nclass C:\n    importlib = registry\n    def m(self):\n        importlib.import_module('kvart.api.auth')\n", 5),
        "annotated": ("from typing import Callable\nimport importlib\nload: Callable = importlib.import_module\nload('kvart.api.auth')\n", 4),
        "chained": ("import importlib\na = b = importlib.import_module\nb('kvart.api.auth')\n", 3),
        "comprehension": ("import importlib\nx = [importlib.import_module('kvart.api.auth') for _ in range(1)]\n", 2),
    }
    for stem, text in shadowed.items():
        write(os.path.join(kv, "core", stem + ".py"), text)
    for stem, (text, _line) in seen.items():
        write(os.path.join(kv, "core", stem + ".py"), text)
    code, out = run(kv_config(kv))
    for stem in shadowed:
        check("scope: a name shadowed in its scope (%s) is not the import machinery" % stem, "core/%s.py" % stem not in out, out)
    for stem, (_text, line) in seen.items():
        check("scope: %s is read" % stem, code == 1 and "core/%s.py:%d -> auth (api/auth.py)" % (stem, line) in out, out)

    kv = os.path.join(tmp, "kv11", "kvart")
    write(os.path.join(kv, "core", "base.py"), "x = 1\n")
    write(os.path.join(tmp, "kv11", "outside", "src.py"), "from kvart.core.base import x\n")
    write(os.path.join(tmp, "kv11", "outside", "far.py"), "x = 1\n")
    write(os.path.join(tmp, "kv11", "outside2", "far.py"), "x = 1\n")
    os.symlink(os.path.join("..", "..", "outside", "src.py"), os.path.join(kv, "core", "src_link.py"))
    code, out = run(kv_config(kv))
    check("symlink: a source linked out of every judged root is a violation naming the link",
          code == 1 and "core/src_link.py:1 -> base (core/base.py) — the source is a symlink resolving to ../outside/src.py" in out, out)
    os.remove(os.path.join(kv, "core", "src_link.py"))
    os.symlink(os.path.join("..", "..", "outside"), os.path.join(kv, "core", "farlink"))
    write(os.path.join(kv, "core", "evil.py"), "from kvart.core.farlink.far import z\n")
    pinned = {"file": "core/evil.py", "name": "far", "target": "../outside/far.py", "reason": "vendored"}
    code, out = run(kv_config(kv, exempt=[pinned]))
    check("symlink: an exemption for an out-of-root link names where it really points", code == 0 and "(1 exempt)" in out, out)
    os.remove(os.path.join(kv, "core", "farlink"))
    os.symlink(os.path.join("..", "..", "outside2"), os.path.join(kv, "core", "farlink"))
    code, out = run(kv_config(kv, exempt=[pinned]))
    check("symlink: retargeting the link reports the new target and retires the entry",
          code == 1 and "core/evil.py:1 -> far (../outside2/far.py)" in out and "('core/evil.py', 'far', '../outside/far.py')" in out, out)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

suite.finish()
