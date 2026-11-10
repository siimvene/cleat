#!/usr/bin/env python3
"""check-layering — fail if a source references a type from a layer it may not
depend on, unless the reference is on the project's exemption list.

Each judged tree is laid out in layers, by top-level directory under its own
root — `layering.roots` in `quality.json`, a list of `{"root", "exempt"}` — and
the layering is what lets a type be understood without reading the rest of the
tree: a model is a value, a service does work over models, a view model drives
a view. Nothing enforced it in the project this check was written for, and it
drifted the way unenforced structure does — a model read the global config
store, a service reached into a view model, and a model that needs a service
cannot be moved out, tested without it, or read on its own. Thirty-three such
references stood in the app when the check was written; the check judged the
app alone until the tree it moves reference-heavy files into, the local Swift
package, got a root of its own too — a file that crosses out of the app by
moving into the package cannot leave the ratchet just by moving.

What may depend on what is `layering.allowed` in `quality.json`, shared by
every root: each layer name maps to the layers it may reference besides
itself, or to null for a layer that may reach anything. In the project this
was written for that reads: Extensions on nothing; Models on Extensions;
Runtime on Models; Services on Runtime and Models; ViewModels on Services and
below; Views and the app entry (files at the root, layer `App`) on anything.
`layering.always_allowed` names layers reachable from every other — there,
`Testing/`, the "is this process the XCTest host" switch, since it is a fact
about the process, not a layer. Directories in `layering.skip_dirs` are not
read at all — a skipped file is never judged as a source, so a shim under a
skipped directory inside a layer can relay that layer's imports; skip only
what holds no production code. `layering.unknown_layers` decides a layer
`allowed` does not list (a new top-level directory, or `App` when it is left
out): "allow", the default, leaves it unrestricted; "deny" lets it reference
only itself and the `always_allowed` layers, and each such failure says the
layer is undeclared. Under "deny" that holds for an `always_allowed` layer too
— being reachable from everywhere is no licence to reach everywhere, or it
relays (core -> testing/shim -> api); list it in `allowed` (null for
unrestricted) to say what it may reach. A file's layer is where it really is:
a symlink in one layer to a file in another is the other layer, and a symlink
— source or target — resolving outside every judged root is a violation naming
the link; that end is printed and keyed (exemption `file` or `target`) by where
the link really points, relative to the root, so retargeting it retires the
entry. Every source file and directory the walk meets is checked, not only the
ends of a reference: a source-file link out of every root that imports nothing
under one, and a directory link out of every root (the walk never descends a
directory link, so nothing under it would be judged), are each a violation
named `symlink`, printed at line 0 and keyed by the link (`file`) and where it
really points (`target`). A directory link that stays inside a judged root is
not descended either: the files it reaches are judged where they really are,
by the walk of that directory — as a file link is judged where it points.

How a reference is read (`extractors/references.py`) depends on
`layering.references`: `identifiers` — the default, for Swift — reads every
top-level `struct`/`class`/`enum`/`actor`/`protocol`/`typealias` name declared
in the judged trees as owned by the file that declares it, and a capitalised
identifier in another file's *code* that matches one as a reference to that
file; `imports`, with `layering.language` one of python, typescript,
javascript, rust, go, kotlin, java, resolves each import line to the file it
names under a judged root — for python every import the parser finds in the
file's bytes, under its BOM or `coding:` cookie (`extractors/python_imports.py`:
comma lists, semicolons, continuations, `from pkg import name` as the
submodule `pkg.name` when there is one, and `importlib.import_module` /
`__import__` / `runpy.run_module` / `exec` of a constant name by any name the
file binds them to; out of scope: a computed module name, `getattr` with a
computed attribute, `exec` of a computed string, `runpy.run_path`, aliases
bound by a walrus, unpacking or a `for`/`with` target, star imports from
importlib/runpy; a name is the machinery if any of its bindings may be). A file
the parser refuses is read statement by statement and named on a WARN line —
a FAIL with `layering.unparseable: "fail"` (default "warn") — and so is a file
nesting `exec`/`eval` strings deeper than the reader follows; `ast-grep` is the identifiers reading through a
parser, for nine languages, needing `ast-grep` installed. For `identifiers`: Comments and string literals are
stripped first, because prose is not a dependency — a repo's headers name
types in other layers constantly, and a mention in a comment would make every
file depend on what it talks about. The reading is by name, so a nested type
that shadows a top-level name elsewhere reads as the top-level one; the line
reported lets a person see which it was.

It is a ratchet, per root. Every reference that violated the rules when its
root was added is in that root's `exempt` — a list of `{"file", "name",
"target", "reason"}` objects, the file relative to the root, the name the
referenced type (for imports, the import's last segment), the target the
referenced file relative to its own root as the failure line prints it in
parentheses, the reason what it would take to remove it — so the check landed green
for that root and one more is a decision somebody makes on purpose. The
success line prints, for each root, how many are exempt beside how many
references were judged, so no root's list can grow quietly. An entry that no
longer names a violation — the reference was removed, or the file was — fails
the check too, in the root it was written for: a dead exemption is the same
rot one level up, and the way to a shorter list is to delete the entry the
moment the reference goes. An exemption belongs to one root; it is never read
against another root's tree, so a reference moved from one judged root to the
other fails there rather than quietly inheriting the old root's exemption. To
add an entry: the reference must already exist and violate the rules in that
root, since the check fails on an entry that names nothing. `target` is
optional for older lists: an entry without it matches every reference from
that file by that name, whatever file it resolves to, so `from app.billing
import x` would ride on an entry written for `models/billing.py`. New
entries carry it — the failure prints each one ready to paste, target included
— and an entry with a target covers that reference only.

It reads the tree and starts no build, so it is safe while the app is running.

  ./quality/bin/check-layering.py
  ./quality/bin/check-layering.py --quiet        # only print on failure
  ./quality/bin/check-layering.py --app DIR      # judge one handed-in tree instead of the configured roots (the tests use this)
  ./quality/bin/check-layering.py --config FILE  # the quality.json to read the rules from
"""

import argparse
import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quality_config

from extractors import references


class Root:
    """One judged tree: its absolute path and its own exemption list."""

    def __init__(self, path, exempt):
        self.path = path
        # (referencing file, referenced name, referenced file or None) -> the reason it
        # stands. Paths are relative to this root (the referenced file to its own root);
        # None is an entry without "target", which matches the name whatever it resolves to.
        self.exempt = exempt


class Rules:
    """The `layering` section of a quality.json, read into the shapes the judge uses."""

    def __init__(self, config):
        self.skip_dirs = set(config.get("layering", "skip_dirs"))
        # Layer -> the layers it may reference, besides itself. None means anything.
        self.allowed = {
            layer: None if layers is None else set(layers)
            for layer, layers in config.get("layering", "allowed").items()
        }
        # Reachable from every layer: a fact about the process, not a layer.
        self.always_allowed = set(config.get("layering", "always_allowed"))
        self.roots = [self._root(config, entry) for entry in config.get("layering", "roots")]
        section = config.section("layering")
        # How a reference is read: `identifiers` (Swift — declared type names) or
        # `imports` (a language with explicit imports), with `language` naming which.
        self.reader = section.get("references", "identifiers")
        self.language = section.get("language", "swift")
        self.extensions = tuple(section.get("extensions", [".swift"]))
        if self.reader not in ("identifiers", "imports", "ast-grep"):
            raise KeyError('%s: "layering.references" must be "identifiers", "imports" or "ast-grep"; got %r' % (config.file, self.reader))
        # A layer `allowed` does not list: "allow" leaves it unrestricted, "deny" confines
        # it to itself and the always-allowed layers.
        self.unknown_layers = section.get("unknown_layers", "allow")
        if self.unknown_layers not in ("allow", "deny"):
            raise KeyError('%s: "layering.unknown_layers" must be "allow" or "deny"; got %r' % (config.file, self.unknown_layers))
        # A Python file the parser refuses, read statement by statement instead: "warn"
        # names it, "fail" fails the check on it.
        self.unparseable = section.get("unparseable", "warn")
        if self.unparseable not in ("warn", "fail"):
            raise KeyError('%s: "layering.unparseable" must be "warn" or "fail"; got %r' % (config.file, self.unparseable))

    def undeclared(self, layer):
        """Whether `layer` is confined for being missing from `allowed` (unknown_layers:
        deny) — an always_allowed layer too: being reachable from everywhere is no licence
        to reach everywhere, or it relays."""
        return self.unknown_layers == "deny" and layer not in self.allowed

    def permits(self, from_layer, to_layer):
        """Whether `from_layer` may reference `to_layer`."""
        if to_layer == from_layer or to_layer in self.always_allowed:
            return True
        if from_layer not in self.allowed:
            return not self.undeclared(from_layer)
        allowed = self.allowed[from_layer]
        return allowed is None or to_layer in allowed

    @staticmethod
    def _exempt_key(item):
        """(file, name, target or None) for one exemption entry; raises on a malformed one."""
        file, name, target = item["file"], item["name"], item.get("target")
        if not (isinstance(file, str) and isinstance(name, str)) or not (target is None or (isinstance(target, str) and target)):
            raise ValueError(item)
        return file, name, None if target is None else os.path.normpath(target)

    @staticmethod
    def _root(config, entry):
        try:
            root, raw_exempt = entry["root"], entry["exempt"]
        except (KeyError, TypeError):
            raise KeyError('%s: every "layering.roots" entry needs "root" and "exempt"; got %r'
                            % (config.file, entry))
        exempt = {}
        for item in raw_exempt:
            try:
                exempt[Rules._exempt_key(item)] = item["reason"]
            except (KeyError, TypeError, AttributeError, ValueError):
                raise KeyError('%s: every "layering.exempt" entry needs "file" and "name" (strings) and "reason", '
                               'and may name a "target" (the referenced file\'s path); got %r' % (config.file, item))
        return Root(os.path.abspath(config.path(root)), exempt)


def layer_of(relative):
    parts = relative.split(os.sep)
    return parts[0] if len(parts) > 1 else "App"


def place(path, roots):
    """`path`'s real location relative to the first judged root that really contains it,
    or None when its realpath is under none of them — a symlink resolving elsewhere."""
    real = os.path.realpath(path)
    for root in roots:
        real_root = os.path.realpath(root)
        if real.startswith(os.path.join(real_root, "")):
            return os.path.relpath(real, real_root)
    return None


def outside_root(path, root):
    """Where `path` really is, relative to `root`'s real path — for a symlink resolving
    outside every judged root, so an exemption keys on where it points, not the link."""
    return os.path.relpath(os.path.realpath(path), os.path.realpath(root))


def escapes(roots, path, app, target, target_root):
    """(source, referenced, note) when the reference's source or target is a symlink
    resolving outside every judged root — always a violation — else None. Either end
    is named by where it really points, relative to its root, so an exemption keys on
    that and retargeting the link retires it."""
    source, notes = os.path.relpath(path, app), []
    if place(path, roots) is None:
        source = outside_root(path, app)
        notes.append("the source is the symlink %s, which resolves outside every judged root" % os.path.relpath(path, app))
    referenced = place(target, roots)
    if referenced is None:
        referenced = outside_root(target, target_root)
        notes.append("through the symlink %s, which resolves outside every judged root" % os.path.relpath(target, target_root))
    return (source, referenced, "; ".join(notes)) if notes else None


def classify(rules, roots, path, app, target, target_root):
    """(source, referenced, note) for one reference: `referenced` None when the layering
    permits it. Layers come from where a file really is, so a symlink in one layer to a
    file in another relays nothing; a source or target symlink resolving outside every
    judged root is a violation."""
    escaped = escapes(roots, path, app, target, target_root)
    if escaped:
        return escaped
    source, as_written, real = os.path.relpath(path, app), os.path.relpath(target, target_root), place(target, roots)
    from_layer = layer_of(place(path, roots))
    if rules.permits(from_layer, layer_of(real)):
        return source, None, ""
    notes = ["through the symlink %s" % as_written] if real != as_written else []
    if rules.undeclared(from_layer):
        notes.append("layer %r is undeclared: `layering.allowed` does not list it and `unknown_layers` is deny" % from_layer)
    return source, real, "; ".join(notes)


def judge(app, exempt, rules, strict_dead, references_in_app, roots=None, links=()):
    """(violations, dead exemptions, judged, exempt used) for the tree at `app`,
    judged against `exempt` — `strict_dead` when a file an entry names but the
    tree lacks still counts as dead (the tree is one of the configured roots,
    not a fixture handed in with --app, where a missing file is almost always
    just a fixture that has no reason to reproduce every file in the list).
    `references_in_app` is what the extractor read: (from file, line, name,
    referenced file, its root). `roots` are the judged roots a file's real location is
    placed under (default: `app` alone); `links` the symlinks under `app` resolving
    outside every one of them (`escaping_links`)."""
    roots = roots or [app]
    references = references_in_app
    findings = [(number, name) + classify(rules, roots, path, app, target, target_root)
                for path, number, name, target, target_root in references]
    findings += link_findings(app, links, {reference[0] for reference in references})
    violations = []
    exempt_used = set()
    for number, name, source, referenced, note in findings:
        if referenced is None:
            continue
        key = exempt_key(exempt, source, name, referenced)
        if key is not None:
            exempt_used.add(key)
            continue
        violations.append((source, number, name, referenced, note))
    return violations, dead_entries(app, exempt, exempt_used, strict_dead), len(references), len(exempt_used)


def escaping_links(app, roots, skip_dirs, suffixes):
    """Every symlink under `app` — a directory, or a file ending in one of `suffixes` —
    resolving outside every judged root, walked as the readers walk: `skip_dirs` pruned,
    a directory link listed but never descended."""
    found = []
    for dirpath, dirnames, filenames in os.walk(app):
        dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs)
        found += [path for path in walked(dirpath, dirnames, filenames, suffixes) if escapes_roots(path, roots)]
    return found


def walked(dirpath, dirnames, filenames, suffixes):
    """The directories and source files one step of the walk meets, as paths."""
    return [os.path.join(dirpath, entry) for entry in dirnames + sorted(f for f in filenames if f.endswith(suffixes))]


def escapes_roots(path, roots):
    """Whether `path` is a symlink resolving outside every judged root."""
    return os.path.islink(path) and place(path, roots) is None


def link_findings(app, links, sources):
    """(line 0, "symlink", link, where it really points, note) per escaping link no
    reference already reports — a directory link, whose contents nothing judges, or a
    source-file link importing nothing under a root. Keyed by the link and its real
    target, so retargeting the link retires an exemption written for it."""
    return [(0, "symlink", os.path.relpath(link, app), outside_root(link, app),
             "the %s symlink %s resolves outside every judged root"
             % ("directory" if os.path.isdir(link) else "source", os.path.relpath(link, app)))
            for link in links if link not in sources]


def source_suffixes(rules):
    """The file endings the configured reader reads as sources."""
    if rules.reader == "identifiers":
        return rules.extensions
    return references.SUFFIXES.get(rules.language, rules.extensions)


def dead_entries(app, exempt, exempt_used, strict_dead):
    """The entries no violating reference used — for a file the tree lacks too, when strict."""
    return sorted(
        (key for key in set(exempt) - exempt_used
         if strict_dead or os.path.isfile(os.path.join(app, key[0]))),
        key=lambda key: (key[0], key[1], key[2] or ""),
    )


def exempt_key(exempt, source, name, referenced):
    """The entry covering this reference: one naming its target first, else one without a
    target (it covers the name wherever it resolves); None when neither is listed."""
    return next((key for key in ((source, name, referenced), (source, name, None)) if key in exempt), None)


def targets_for(args, rules, config):
    """What to judge: with --app, one handed-in tree under every root's exemptions at
    once and no dead-exemption strictness (a fixture stands in for whichever root it is
    shaped like); else each configured root under its own list."""
    if args.app:
        merged = {}
        for root in rules.roots:
            merged.update(root.exempt)
        app = os.path.abspath(args.app)
        return [(app, merged, False, None)], [app]
    targets = [(root.path, root.exempt, True, os.path.relpath(root.path, config.root)) for root in rules.roots]
    return targets, [root.path for root in rules.roots]


def reader_for(rules, owner_roots):
    """A function app → references, for the configured reader. Owners come from every
    judged root at once, so a name that moved from one root into another still resolves."""
    if rules.reader == "imports":
        if rules.language not in references.IMPORT_RES:
            raise KeyError("no import reader for %r — one of: %s" % (rules.language, ", ".join(sorted(references.IMPORT_RES))))
        return lambda app, unparsed: references.import_references(app, owner_roots, rules.language, rules.skip_dirs, unparsed)
    if rules.reader == "ast-grep":
        if rules.language not in references.ASTGREP:
            raise KeyError("no ast-grep reader for %r — one of: %s" % (rules.language, ", ".join(sorted(references.ASTGREP))))
        return lambda app, _unparsed: references.astgrep_references(app, owner_roots, rules.language, rules.skip_dirs)
    code, owner = references.build_owner_index(owner_roots, rules.skip_dirs, rules.extensions)
    return lambda app, _unparsed: references.identifier_references(app, code, owner, rules.skip_dirs, rules.extensions)


def main():
    parser = argparse.ArgumentParser(description="fail on a cross-layer reference that is not on the exemption list")
    parser.add_argument("--quiet", action="store_true", help="only print on failure")
    parser.add_argument("--app", help="judge one handed-in tree instead of the configured roots (the tests use this)")
    quality_config.add_config_argument(parser)
    args = parser.parse_args()
    config = quality_config.load(args.config)
    try:
        rules = Rules(config)
    except KeyError as problem:
        print("FAIL: %s" % problem.args[0])
        return 2

    targets, owner_roots = targets_for(args, rules, config)
    try:
        read = reader_for(rules, owner_roots)
    except KeyError as problem:
        print("FAIL: %s" % problem.args[0])
        return 2

    failed = False
    for app, exempt, strict_dead, label in targets:
        unparsed = []
        try:
            found = read(app, unparsed)
        except references.ToolError as problem:
            print("FAIL: %s" % problem, file=sys.stderr)
            return 2
        failed = print_unparsed(unparsed, app, rules, config) or failed
        links = escaping_links(app, owner_roots, rules.skip_dirs, source_suffixes(rules))
        violations, dead, judged, exempt_used = judge(app, exempt, rules, strict_dead, found, owner_roots, links)
        failed = print_verdict(violations, dead, judged, exempt_used, label, config, args.quiet) or failed
    return 1 if failed else 0


def print_unparsed(unparsed, app, rules, config):
    """Name every file the parser refused — even under --quiet, since a file read by
    the weaker line reader must never pass silently; True when that fails the check."""
    failing = rules.unparseable == "fail"
    for path in unparsed:
        print("%s: %s does not parse as Python (or cannot be read, or nests exec/eval strings deeper than is read) — "
              "its imports were read only in part, which can miss some; %s" % ("FAIL" if failing else "WARN", os.path.relpath(path, app),
              "`layering.unparseable` is \"fail\" in %s" % config.file if failing
              else "set `layering.unparseable: \"fail\"` in %s to make this a failure" % config.file))
    return failing and bool(unparsed)


def print_violations(violations, where, config):
    print("FAIL: %d cross-layer reference(s) the layering does not allow%s." % (len(violations), where))
    print("Layers may depend downward only — see `layering.allowed` in %s." % config.file)
    for source, number, name, target, note in violations:
        print("  %s:%d -> %s (%s)%s" % (source, number, name, target, " — " + note if note else ""))
    print("Move the type to the layer that needs it, inject it, or — on purpose — add the entry to the "
          "`exempt` list%s of the `layering` section in %s, with the reason:" % (where, config.file))
    for source, _number, name, target, _note in violations:
        print("  " + json.dumps({"file": source, "name": name, "target": target,
                                  "reason": "why it stands, and what would remove it"}))


def print_dead(dead, label, config):
    print("FAIL: %d EXEMPT entr%s in %s%s name%s no violating reference — the reference is gone, so the entry must go too:"
          % (len(dead), "y" if len(dead) == 1 else "ies", config.file,
             " for %s" % label if label else "", "s" if len(dead) == 1 else ""))
    for source, name, target in dead:
        print("  (%r, %r)" % (source, name) if target is None else "  (%r, %r, %r)" % (source, name, target))


def print_verdict(violations, dead, judged, exempt_used, label, config, quiet):
    """Print one root's result; True when it failed."""
    failed = bool(violations or dead)
    if violations:
        print_violations(violations, " in %s" % label if label else "", config)
    if dead:
        print_dead(dead, label, config)
    if not failed and not quiet:
        prefix = "%s: " % label if label else ""
        print("OK: %s%d cross-file references obey the layering (%d exempt)" % (prefix, judged, exempt_used))
    return failed


if __name__ == "__main__":
    sys.exit(main())
