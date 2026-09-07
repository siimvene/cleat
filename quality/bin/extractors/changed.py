"""changed — which lines the working tree changed against a base, from git.

`base_ref()` picks what to diff against: an explicit ref, else the pull
request's base when CI says so (`GITHUB_BASE_REF`), else the merge-base with
the default branch, else HEAD — so on the default branch with nothing pushed
"changed" means uncommitted. `changed_lines()` reads `git diff -U0` from that
base to the working tree, plus every line of every untracked file, as
{repo-relative path: set of line numbers}.

Whitespace is not a change: the diff runs with `--ignore-all-space`, so a
line a formatter reflowed, re-indented or re-spaced is not "changed" and a
format-only commit changes nothing. Before, a formatter pass over a tree
marked every line it touched, and the duplication gate reported every
pre-existing clone under those lines as new (25 pairs on one pilot branch,
none introduced by it). A line the formatter joined or split is still a
change, as it should be. The gates this scopes key on functions, sites and
clones, none of which an indentation-only edit moves.
"""

import os
import re
import subprocess

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
DIFF_FLAGS = ("-U0", "--no-color", "--no-ext-diff", "--ignore-all-space")


class ChangedError(Exception):
    """git could not be read; the message says why."""


def _git(repo, *args):
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout


def _exists(repo, ref):
    return _git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}")[0] == 0


def base_ref(repo, explicit=None):
    """The ref to diff against — see the module docstring for the order."""
    if explicit:
        return explicit
    pr_base = os.environ.get("GITHUB_BASE_REF")
    if pr_base and _exists(repo, "origin/" + pr_base):
        return "origin/" + pr_base
    for candidate in ("origin/main", "origin/master", "main", "master"):
        if _exists(repo, candidate):
            code, base = _git(repo, "merge-base", candidate, "HEAD")
            if code == 0 and base.strip():
                return base.strip()
    return "HEAD"


def _parse_diff(out):
    changed = {}
    current = None
    for line in out.splitlines():
        if line.startswith("+++ "):
            name = line[4:].strip()
            current = None if name == "/dev/null" else (name[2:] if name.startswith("b/") else name)
            continue
        match = HUNK_RE.match(line)
        if match and current is not None:
            start, count = int(match.group(1)), int(match.group(2) or 1)
            changed.setdefault(current, set()).update(range(start, start + count))
    return changed


def _untracked(repo, changed):
    _, out = _git(repo, "ls-files", "--others", "--exclude-standard")
    for name in out.splitlines():
        path = os.path.join(repo, name)
        if os.path.isfile(path):
            with open(path, errors="replace") as handle:
                changed.setdefault(name, set()).update(range(1, handle.read().count("\n") + 2))
    return changed


def base_text(repo, base, path):
    """The text of repo-relative `path` as `base` had it, or None when the base has no
    such file (a new file, or a tree that is not a repository)."""
    code, out = _git(repo, "show", "%s:%s" % (base, path))
    return out if code == 0 else None


def changed_lines(repo, base):
    """{repo-relative path: {line, …}} for every line added or changed between `base`
    and the working tree, untracked files included in full."""
    code, out = _git(repo, "diff", *DIFF_FLAGS, base, "--")
    if code != 0:
        code, out = _git(repo, "diff", *DIFF_FLAGS, "--")
    if code != 0:
        raise ChangedError("git diff failed in %s" % repo)
    return _untracked(repo, _parse_diff(out))
