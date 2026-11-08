#!/usr/bin/env python3
"""Check whether the upstream of this fork has moved on and how urgent a merge is.

Deterministic part of the upstream-sync routine (see
.github/workflows/upstream-sync-check.yml). It needs nothing but git, and
optionally `gh` (published upstream advisories) and `uv` (pip-audit on both
lockfiles). Every external probe fails soft: a missing tool lowers the
confidence of the report but never aborts it.

Writes <out>/report.json and <out>/report.md and, inside GitHub Actions,
the step outputs `priority`, `upstream_ahead`, `upstream_head`, `conflicts`.

Priority levels:
  critical  upstream fixes a known vulnerability (advisory or dependency CVE
            that is present in the fork's lockfile but not in upstream's)
  high      security-relevant upstream commits (keywords or sensitive paths)
  medium    dependency / supply-chain updates (lockfile, Dockerfile, actions)
  low       other upstream changes
  none      upstream is not ahead
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SECURITY_RE = re.compile(
    r"\b(security|secur|cve-\d{4}-\d+|ghsa-[a-z0-9-]+|vuln\w*|exploit|xss|csrf|ssrf|"
    r"injection|traversal|rce|sanitiz\w*|escap\w*|leak\w*|secret\w*|credential\w*|"
    r"tokens?|auth|authn|authz|authenticat\w*|authoriz\w*|oauth\w*|oidc|permission\w*|scope\w*|privilege\w*|bypass\w*|unsafe|"
    r"harden\w*|tls|ssl|certificate\w*|redact\w*|validat\w*|reject\w*)\b",
    re.IGNORECASE,
)
DEPENDENCY_RE = re.compile(
    r"(^|/)(uv\.lock|pyproject\.toml|requirements[^/]*\.txt|Dockerfile[^/]*|"
    r"docker-compose[^/]*\.ya?ml|package(-lock)?\.json|flake\.lock)$|^\.github/workflows/"
)
TRAILER_RE = re.compile(r"^[A-Za-z-]+: ")
PRIORITY_ORDER = ["none", "low", "medium", "high", "critical"]


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> str:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {result.stderr.strip()}")
    return result.stdout


def git(*args: str, check: bool = True) -> str:
    return run(["git", *args], check=check)


def bump(current: str, candidate: str) -> str:
    return max(current, candidate, key=PRIORITY_ORDER.index)


def commit_list(base: str, head: str) -> list[dict[str, Any]]:
    """Non-merge commits in base..head with subject, body and touched files."""
    sep, end = "\x1f", "\x1e"
    raw = git(
        "log",
        "--no-merges",
        f"--format={end}%H{sep}%an{sep}%aI{sep}%s{sep}%b{sep}",
        "--name-only",
        f"{base}..{head}",
    )
    commits = []
    for chunk in raw.split(end)[1:]:
        sha, author, date, subject, body, files = chunk.split(sep, 5)
        commits.append(
            {
                "sha": sha,
                "author": author,
                "date": date,
                "subject": subject.strip(),
                "body": body.strip(),
                "files": [f for f in files.strip().splitlines() if f],
            }
        )
    return commits


def classify(commit: dict[str, Any], sensitive: list[str]) -> list[str]:
    reasons = []
    # Trailers (Co-authored-by, Signed-off-by, ...) are not content.
    body = "\n".join(line for line in commit["body"].splitlines() if not TRAILER_RE.match(line))
    text = f"{commit['subject']}\n{body}"
    hits = sorted({m.group(0).lower() for m in SECURITY_RE.finditer(text)})
    if hits:
        reasons.append("keywords: " + ", ".join(hits[:6]))
    touched = [f for f in commit["files"] if any(f.startswith(p) for p in sensitive)]
    if touched:
        reasons.append("sensitive paths: " + ", ".join(touched[:4]))
    return reasons


def merge_conflicts(ours: str, theirs: str) -> list[str] | None:
    """Files that would conflict in a merge, without touching the worktree.

    Returns None when this git is too old for `merge-tree --write-tree`.
    """
    result = subprocess.run(
        [
            "git",
            "merge-tree",
            "--write-tree",
            "--name-only",
            "--no-messages",
            ours,
            theirs,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return []
    if result.returncode == 1:
        return [line for line in result.stdout.splitlines()[1:] if line.strip()]
    print(f"merge-tree unavailable: {result.stderr.strip()}", file=sys.stderr)
    return None


def upstream_advisories(repo: str, since: str) -> list[dict[str, Any]] | None:
    """Published GitHub security advisories of the upstream repo (needs `gh`)."""
    if not shutil.which("gh"):
        return None
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/security-advisories?state=published&per_page=100"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"advisory lookup failed: {result.stderr.strip()}", file=sys.stderr)
        return None
    advisories = []
    for adv in json.loads(result.stdout or "[]"):
        published = adv.get("published_at") or ""
        if published >= since:
            advisories.append(
                {
                    "ghsa_id": adv.get("ghsa_id"),
                    "cve_id": adv.get("cve_id"),
                    "severity": adv.get("severity"),
                    "summary": adv.get("summary"),
                    "published_at": published,
                    "url": adv.get("html_url"),
                }
            )
    return advisories


def audit_lockfile(ref: str) -> set[str] | None:
    """Vulnerability ids (package:id) of the uv.lock at `ref`, via pip-audit."""
    if not shutil.which("uv"):
        return None
    if subprocess.run(["git", "cat-file", "-e", f"{ref}:uv.lock"], capture_output=True).returncode:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for name in ("pyproject.toml", "uv.lock"):
            (tmp_path / name).write_text(git("show", f"{ref}:{name}"))
        try:
            reqs = run(
                [
                    "uv",
                    "export",
                    "--frozen",
                    "--all-extras",
                    "--no-hashes",
                    "--no-emit-project",
                ],
                cwd=tmp_path,
            )
        except RuntimeError as exc:
            print(f"uv export failed for {ref}: {exc}", file=sys.stderr)
            return None
        (tmp_path / "requirements.txt").write_text(reqs)
        result = subprocess.run(
            [
                "uvx",
                "pip-audit",
                "--progress-spinner",
                "off",
                "--format",
                "json",
                "--no-deps",
                "--disable-pip",
                "-r",
                str(tmp_path / "requirements.txt"),
            ],
            capture_output=True,
            text=True,
        )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(
            f"pip-audit failed for {ref}: {result.stderr.strip()[:500]}",
            file=sys.stderr,
        )
        return None
    return {
        f"{dep['name']}:{vuln['id']}"
        for dep in data.get("dependencies", [])
        for vuln in dep.get("vulns", [])
    }


def render_markdown(report: dict[str, Any]) -> str:
    r = report
    lines = [
        f"<!-- upstream-head: {r['upstream_head']} -->",
        f"## Upstream-Abgleich `{r['upstream_repo']}` → `{r['fork_branch']}`",
        "",
        f"- Priorität: **{r['priority']}**",
        f"- Upstream voraus: {r['upstream_ahead']} Commits, Fork voraus: {r['fork_ahead']} Commits",
        f"- Merge-Basis: `{r['merge_base'][:10]}` ({r['merge_base_date'][:10]})",
    ]
    conflicts = r["conflicts"]
    if conflicts is None:
        lines.append("- Konflikte: nicht geprüft (git zu alt)")
    elif conflicts:
        lines.append(
            f"- Konflikte beim Merge: {len(conflicts)} Datei(en): "
            + ", ".join(f"`{c}`" for c in conflicts[:15])
        )
    else:
        lines.append("- Konflikte beim Merge: keine (sauberer Merge möglich)")
    if r["advisories"]:
        lines += ["", "### Veröffentlichte Upstream-Advisories seit der Merge-Basis"]
        for a in r["advisories"]:
            lines.append(f"- [{a['ghsa_id']}]({a['url']}) {a['severity']}: {a['summary']}")
    if r["vulns_fixed_by_merge"]:
        lines += ["", "### Abhängigkeits-Schwachstellen, die der Merge behebt"]
        lines += [f"- `{v}`" for v in r["vulns_fixed_by_merge"]]
    if r["vulns_introduced_by_merge"]:
        lines += [
            "",
            "### Schwachstellen, die nur Upstream hat (Merge würde sie einführen)",
        ]
        lines += [f"- `{v}`" for v in r["vulns_introduced_by_merge"]]
    sec = [c for c in r["commits"] if c["security_reasons"]]
    if sec:
        lines += ["", "### Sicherheitsrelevante Upstream-Commits"]
        for c in sec:
            lines.append(
                f"- `{c['sha'][:8]}` {c['subject']} — _{'; '.join(c['security_reasons'])}_"
            )
    dep = [c for c in r["commits"] if c["dependency_change"] and not c["security_reasons"]]
    if dep:
        lines += ["", "### Abhängigkeits-/Supply-Chain-Updates"]
        lines += [f"- `{c['sha'][:8]}` {c['subject']}" for c in dep]
    rest = [c for c in r["commits"] if not c["security_reasons"] and not c["dependency_change"]]
    if rest:
        lines += ["", "<details><summary>Weitere Upstream-Commits</summary>", ""]
        lines += [f"- `{c['sha'][:8]}` {c['subject']}" for c in rest]
        lines += ["", "</details>"]
    if r["notes"]:
        lines += ["", "### Hinweise zur Prüfung"] + [f"- {n}" for n in r["notes"]]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--upstream-repo", required=True, help="owner/name of the upstream repo")
    parser.add_argument("--upstream-branch", default="main")
    parser.add_argument("--fork-ref", default="HEAD", help="fork ref to compare (default HEAD)")
    parser.add_argument(
        "--sensitive-path",
        action="append",
        default=[],
        help="path prefix that marks a commit security-relevant (repeatable)",
    )
    parser.add_argument("--out", default="upstream-report")
    args = parser.parse_args()

    remote_url = f"https://github.com/{args.upstream_repo}.git"
    if git("remote", "get-url", "upstream", check=False).strip() != remote_url:
        git("remote", "remove", "upstream", check=False)
        git("remote", "add", "upstream", remote_url)
    git("fetch", "--quiet", "--no-tags", "upstream", args.upstream_branch)

    upstream = f"upstream/{args.upstream_branch}"
    fork = git("rev-parse", args.fork_ref).strip()
    upstream_head = git("rev-parse", upstream).strip()
    base = git("merge-base", fork, upstream).strip()
    base_date = git("show", "-s", "--format=%aI", base).strip()
    upstream_ahead = int(git("rev-list", "--count", f"{fork}..{upstream}"))
    fork_ahead = int(git("rev-list", "--count", f"{upstream}..{fork}"))

    notes: list[str] = []
    priority = "none" if upstream_ahead == 0 else "low"

    commits = commit_list(fork, upstream) if upstream_ahead else []
    for c in commits:
        c["security_reasons"] = classify(c, args.sensitive_path)
        c["dependency_change"] = any(DEPENDENCY_RE.search(f) for f in c["files"])
        if c["security_reasons"]:
            priority = bump(priority, "high")
        elif c["dependency_change"]:
            priority = bump(priority, "medium")
        c.pop("body")

    conflicts = merge_conflicts(fork, upstream) if upstream_ahead else []

    advisories = upstream_advisories(args.upstream_repo, base_date)
    if advisories is None:
        notes.append("Upstream-Advisories nicht abgefragt (`gh` fehlt oder kein Zugriff).")
        advisories = []
    elif advisories and upstream_ahead:
        priority = bump(priority, "critical")

    fixed: list[str] = []
    introduced: list[str] = []
    if upstream_ahead:
        fork_vulns = audit_lockfile(fork)
        upstream_vulns = audit_lockfile(upstream)
        if fork_vulns is None or upstream_vulns is None:
            notes.append("Lockfile-Audit übersprungen (kein `uv`/`uv.lock` oder pip-audit-Fehler).")
        else:
            fixed = sorted(fork_vulns - upstream_vulns)
            introduced = sorted(upstream_vulns - fork_vulns)
            if fixed:
                priority = bump(priority, "critical")
            if fork_vulns & upstream_vulns:
                notes.append(
                    "Offene Abhängigkeits-Schwachstellen in Fork **und** Upstream "
                    "(Merge allein behebt sie nicht): "
                    + ", ".join(sorted(fork_vulns & upstream_vulns)[:10])
                )

    report = {
        "upstream_repo": args.upstream_repo,
        "upstream_branch": args.upstream_branch,
        "fork_branch": args.fork_ref,
        "fork_head": fork,
        "upstream_head": upstream_head,
        "merge_base": base,
        "merge_base_date": base_date,
        "upstream_ahead": upstream_ahead,
        "fork_ahead": fork_ahead,
        "priority": priority,
        "conflicts": conflicts,
        "advisories": advisories,
        "vulns_fixed_by_merge": fixed,
        "vulns_introduced_by_merge": introduced,
        "commits": commits,
        "notes": notes,
    }

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    (out / "report.md").write_text(render_markdown(report))

    if gh_output := os.environ.get("GITHUB_OUTPUT"):
        with open(gh_output, "a") as fh:
            fh.write(f"priority={priority}\n")
            fh.write(f"upstream_ahead={upstream_ahead}\n")
            fh.write(f"upstream_head={upstream_head}\n")
            fh.write(f"conflicts={'unknown' if conflicts is None else len(conflicts)}\n")
    print(f"priority={priority} upstream_ahead={upstream_ahead} fork_ahead={fork_ahead}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
