#!/usr/bin/env python3
"""check-duplication — fail on copied code in the lines you changed, and on the
repository getting more duplicated overall.

Duplication is the measured failure mode of agent-paced development: a block
copied and adjusted is faster than a function extracted, and nothing in the
build says otherwise. Two judgments, from one reading:

  changed lines   any clone one of whose copies overlaps a line changed
                  against the base (see extractors/changed.py) fails — the
                  block you just wrote, or just touched, has a twin —
                  unless the base already held every copy: a formatter
                  that joins or splits lines changes every line of a block
                  without writing a new one, and that debt is not new
  density         the share of significant lines inside some clone, held by
                  a baseline that only ever lowers

Clones are not baselined by pair: a pair's identity shifts as either side is
edited, so such a baseline would churn stale entries forever. The density is
one number; the changed-lines scope needs no baseline at all.

Clones come from the built-in finder (exact after whitespace normalisation,
nothing to install) or from a jscpd report when `report.jscpd` names one.

  "duplication": {
    "roots":     ["src"],                            # trees read
    "languages": ["python", "typescript"],           # which suffixes (the escapes table's)
    "exclude":   ["*.test.ts"],                      # file globs left unread
    "min_lines": 6,                                  # shortest clone reported (default 6)
    "skip_rust_tests": true,                         # lines inside `#[cfg(test)]` modules are not production (default)
    "baseline":  "quality/duplication-baseline.json",
    "base_ref":  "origin/main",                      # optional; see extractors/changed.py
    "report":    {"jscpd": "reports/jscpd-report.json"}   # optional: read the scanner instead
  }

  quality/bin/check-duplication.py
  quality/bin/check-duplication.py --write-baseline   # accept today's density
  quality/bin/check-duplication.py --base REF          # what "changed" is measured against
  quality/bin/check-duplication.py --repo-only         # skip the changed-lines judgment
  quality/bin/check-duplication.py --strict
"""

import argparse
import importlib.util
import json
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import quality_config
import ratchet
from extractors import changed, duplication, patterns

_spec = importlib.util.spec_from_file_location("check_escapes", os.path.join(HERE, "check-escapes.py"))
check_escapes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_escapes)

SECTION = "duplication"
DENSITY_KEY = "duplicated share of significant lines"


def sources(section, config):
    roots = config.paths(section.get("roots", ["."]))
    skip = set(check_escapes.DEFAULT_SKIP_DIRS) | set(section.get("skip_dirs", []))
    suffixes = section.get("suffixes") or sorted(
        {s for name in section.get("languages", []) for s in check_escapes.language(name)["suffixes"]})
    if not suffixes:
        raise KeyError("%s: \"%s\" names no \"languages\" and no \"suffixes\" — nothing to read" % (config.file, SECTION))
    return list(patterns.files(roots, suffixes, skip, section.get("exclude", [])))


def clones_for(section, config, paths):
    report = section.get("report", {})
    if "jscpd" in report:
        with open(config.path(report["jscpd"])) as handle:
            return duplication.from_jscpd(json.load(handle), config.root), "jscpd"
    return duplication.find(paths, config.root, int(section.get("min_lines", 6)), section.get("skip_rust_tests", True)), "built-in"


def describe(clone):
    copies = ", ".join("%s:%d-%d" % loc for loc in clone.locations)
    return "%d lines  %s" % (clone.lines, copies)


# The finder's punctuation-only lines, plus the colon: a parenthesised `with (...):` puts
# it on a `):` line the finder drops, where the unparenthesised form kept it on a kept line.
_DROP = str.maketrans("", "", " :" + "".join(sorted(duplication.PUNCTUATION_ONLY)))


def _flat(lines):
    """Significant lines joined with every space, bracket, semicolon, comma and colon
    removed: the shape of a block a formatter cannot change by joining, splitting,
    re-indenting or re-wrapping its lines. The finder already drops lines made only of
    that punctuation, so a block's identity here is its other characters, consistently."""
    return "".join(text.translate(_DROP) for _, text in lines)


def already_at_base(clone, config, base, skip_rust_tests=True, cache=None):
    """Whether the base already held every copy of `clone`: each copy's significant text,
    whitespace removed, is a substring of the base version of its file, read the same way.
    A formatter that joined or split the copy's lines changed every one of them (so the
    clone touches the diff) but wrote no new block: that is the debt the density baseline
    already holds, not a twin the agent just made. A copy in a file the base lacks, a copy
    moved between files, or one the formatter rewrote beyond whitespace and brackets (quote
    style, say) reads as new and is reported; that residue errs toward reporting. The
    other way round: an edit that changes only brackets, commas or colons inside a copy
    (`(v,)` to `[v]`) reads as reformatting, and the flattened text has no line separators
    (a join must still match), so a block whose characters ran together elsewhere in the
    base file would be held — at six or more significant lines that is the block itself.
    Occurrences are counted per file: a second copy pasted into a file that already had
    one is new, since the base holds fewer occurrences than the working tree. `cache`
    holds each file's flattened base and working text for the run: one `git show` and
    one read per file, not per copy."""
    cache = {} if cache is None else cache
    for path, start, end in clone.locations:
        held = _flat_base(path, config, base, cache, skip_rust_tests)
        if held is None:
            return False
        lines = _tree_lines(path, config, skip_rust_tests, cache)
        copy = _flat(l for l in lines if start <= l[0] <= end)
        if held.count(copy) < _flat(lines).count(copy):
            return False
    return True


def _flat_base(path, config, base, cache, skip_rust_tests):
    """The flattened base text of `path` (its inline Rust test modules skipped, as the working
    tree's are), None when the base lacks the file; one git read per file."""
    if ("base", path) not in cache:
        text = changed.base_text(config.root, base, path)
        skip = patterns.rust_test_ranges_of(text) if text is not None and skip_rust_tests and path.endswith(".rs") else ()
        cache["base", path] = None if text is None else _flat(duplication.significant(text, skip))
    return cache["base", path]


def _tree_lines(path, config, skip_rust_tests, cache):
    """The significant lines of the working-tree file at `path`; one read per file."""
    if ("tree", path) not in cache:
        cache["tree", path] = duplication.significant_in(config.path(path), skip_rust_tests)
    return cache["tree", path]


def judge_changed(clones, config, base, skip_rust_tests=True):
    """(the clones that touch a changed line and are new, how many lines changed, how many
    touching clones the base already held) — nothing when the tree is not a git repository,
    so density alone is judged."""
    try:
        lines = changed.changed_lines(config.root, base)
    except changed.ChangedError:
        return [], 0, 0
    touching = [c for c in clones if c.touches(lines)]
    cache = {}
    new = [c for c in touching if not already_at_base(c, config, base, skip_rust_tests, cache)]
    return new, sum(len(v) for v in lines.values()), len(touching) - len(new)


def print_held(held):
    if held:
        print("NOTE: %d clone pair(s) under changed lines already had every copy at the base — reformatted, "
              "not written; the density baseline holds that debt" % held)


def measure(section, config):
    """(finding, clones, provenance): the density as one Finding, and what was read."""
    paths = sources(section, config)
    clones, tool = clones_for(section, config, paths)
    duplicated, total = duplication.density(clones, paths, config.root, section.get("skip_rust_tests", True))
    share = round(100.0 * duplicated / total, 2) if total else 0.0
    finding = ratchet.Finding(".", 0, DENSITY_KEY, {"percent": share, "duplicated_lines": duplicated, "total_lines": total})
    measured = ratchet.provenance(tool, None, {k: section[k] for k in sorted(section) if k != "baseline"})
    return finding, clones, measured


def print_touching(touching, changed_count):
    print("FAIL: %d clone pair(s) overlap the %d line(s) changed against the base — the block has a twin:"
          % (len(touching), changed_count))
    for clone in touching[:20]:
        print("  %s" % describe(clone))
    print("Extract the shared block into one function both sites call, or make the copies genuinely "
          "different. A clone in changed lines is judged without a baseline: there is nothing to accept.")


GATE = ratchet.Gate(
    noun="measurement(s)", over="of duplication with no baseline yet",
    fix="Extract the copied blocks into shared functions until the share is back under the baseline. "
        "Accepting more duplication is a policy decision for a person — see quality/README.md.",
    remedy="quality/bin/check-duplication.py --write-baseline",
    show=lambda v: "%.2f%% duplicated (%s of %s lines)" % (v["percent"], v.get("duplicated_lines", "?"), v.get("total_lines", "?")),
    brief=lambda v: "%.2f%%" % v["percent"])


def ok_line_for(values, pairs, changed_count):
    tail = "" if changed_count is None else "; none overlap the %d changed line(s)" % changed_count
    return "OK: %.2f%% of %d significant lines duplicated (%d clone pairs), within the baseline%s" % (
        values["percent"], values["total_lines"], pairs, tail)


def parse_args():
    parser = argparse.ArgumentParser(description="fail on copied code in changed lines, and on rising duplication")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--base", help="the ref changed lines are measured against")
    parser.add_argument("--repo-only", action="store_true", help="judge the density only")
    parser.add_argument("--changed-only", action="store_true", help="judge the changed-lines clones only, no baseline needed (gate.py --changed)")
    ratchet.add_strict_argument(parser)
    quality_config.add_config_argument(parser)
    return parser.parse_args()


def judge_all(args, section, config, baseline_path, finding, clones, measured):
    """The changed-lines judgment, then the density ratchet; the exit code."""
    touching, changed_count, held = [], 0, 0
    if not args.repo_only:
        base = changed.base_ref(config.root, args.base or section.get("base_ref"))
        touching, changed_count, held = judge_changed(clones, config, base, section.get("skip_rust_tests", True))
    if touching:
        print_touching(touching, changed_count)
    if not args.quiet:
        print_held(held)
    entries, stored = ratchet.read(baseline_path)
    ok_line = ok_line_for(finding.values, len(clones), None if args.repo_only else changed_count)
    code = ratchet.report(ratchet.judge([finding], entries, ["percent"], stored, measured), GATE, len(entries), ok_line,
                          quiet=args.quiet, strict=args.strict)
    return 1 if touching else code


def main():
    args = parse_args()
    config = quality_config.load(args.config)
    try:
        section = config.section(SECTION)
        baseline_path = config.path(config.get(SECTION, "baseline"))
        finding, clones, measured = measure(section, config)
    except (KeyError, OSError, ValueError) as problem:
        print("FAIL: %s" % (problem.args[0] if problem.args else problem), file=sys.stderr)
        return 2
    if args.changed_only:
        base = changed.base_ref(config.root, args.base or section.get("base_ref"))
        touching, changed_count, held = judge_changed(clones, config, base, section.get("skip_rust_tests", True))
        if touching:
            print_touching(touching, changed_count)
            print_held(held)
            return 1
        if not args.quiet:
            print("OK: none of %d clone pair(s) overlap the %d changed line(s)" % (len(clones), changed_count))
            print_held(held)
        return 0
    if args.write_baseline:
        ratchet.write(baseline_path, [finding], measured)
        v = finding.values
        print("baseline written: %.2f%% of %d significant lines duplicated (%d clone pairs)" % (v["percent"], v["total_lines"], len(clones)))
        return 0
    return judge_all(args, section, config, baseline_path, finding, clones, measured)


if __name__ == "__main__":
    sys.exit(main())
