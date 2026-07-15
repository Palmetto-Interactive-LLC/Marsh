#!/usr/bin/env python3
"""Repoint GitHub Actions `runs-on:` to production Daytona runners across repos.

Rewrites, in `runs-on:` context only:
  - blacksmith-*                  (paid managed)         -> [self-hosted, daytona]
  - ubuntu-latest / ubuntu-*      (GitHub-hosted)        -> [self-hosted, daytona]
  - [self-hosted, <x>] arrays                            -> [self-hosted, daytona]
Leaves untouched: matrix expressions (${{ ... }}), windows/macos runners, and any
runs-on already targeting daytona. Heavy jobs can be hand-bumped to
[self-hosted, daytona, large] afterward.

Dry-run by default; --apply writes. --repo-root scans one repo; default scans all
sibling repos under the parent of this project.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

TARGET = "[self-hosted, daytona]"
# runs-on: <value>  (captures indent + key + value), single-line form.
LINE = re.compile(r'^(?P<indent>\s*)runs-on:\s*(?P<val>.+?)\s*$')
REWRITE_SCALAR = re.compile(r'^(marsh|daytona|blacksmith[\w.-]*|ubuntu-[\w.-]+|ubuntu-latest)$')


def should_rewrite(val: str) -> bool:
    v = val.strip()
    if "${{" in v:               # matrix / expression — never touch
        return False
    if "daytona" in v and "self-hosted" in v:
        return False
    if v.startswith("[") and v.endswith("]"):
        inner = [x.strip().strip('"\'') for x in v[1:-1].split(",")]
        # self-hosted arrays -> retarget; skip win/mac
        if any(x in ("windows", "macos", "macOS") for x in inner):
            return False
        return "self-hosted" in inner or "marsh" in inner or any(
            REWRITE_SCALAR.match(x) for x in inner)
    return bool(REWRITE_SCALAR.match(v.strip('"\'')))


def process_file(path: pathlib.Path, apply: bool) -> list[str]:
    changes: list[str] = []
    lines = path.read_text().splitlines(keepends=True)
    out = []
    for i, line in enumerate(lines, 1):
        m = LINE.match(line.rstrip("\n"))
        if m and should_rewrite(m.group("val")):
            newline = f'{m.group("indent")}runs-on: {TARGET}\n'
            changes.append(f"  {path}:{i}  {m.group('val').strip()}  ->  {TARGET}")
            out.append(newline)
        else:
            out.append(line)
    if apply and changes:
        path.write_text("".join(out))
    return changes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--repo-root", help="single repo to scan (default: all sibling repos)")
    args = ap.parse_args()

    roots: list[pathlib.Path]
    if args.repo_root:
        roots = [pathlib.Path(args.repo_root).resolve()]
    else:
        parent = pathlib.Path(__file__).resolve().parents[2]  # sibling repos
        roots = [p for p in parent.iterdir() if (p / ".github" / "workflows").is_dir()]

    total = 0
    for root in sorted(roots):
        wf = root / ".github" / "workflows"
        if not wf.is_dir():
            continue
        repo_changes: list[str] = []
        for f in sorted(list(wf.glob("*.yml")) + list(wf.glob("*.yaml"))):
            repo_changes += process_file(f, args.apply)
        if repo_changes:
            print(f"\n== {root.name} ({len(repo_changes)} runs-on) ==")
            print("\n".join(repo_changes))
            total += len(repo_changes)
    print(f"\n{'APPLIED' if args.apply else 'DRY-RUN'}: {total} runs-on lines"
          f"{' rewritten' if args.apply else ' would change'} -> {TARGET}")
    print("Review, then commit per-repo on a branch and open PRs. Bump heavy jobs to "
          "[self-hosted, marsh, large] by hand.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
