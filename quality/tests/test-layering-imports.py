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
finally:
    shutil.rmtree(tmp, ignore_errors=True)

suite.finish()
