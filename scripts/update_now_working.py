#!/usr/bin/env python3
"""Summarize recent public-repo commits via Claude Haiku and write NOW.md.

Runs nightly in GitHub Actions to update NOW.md.

Two sources are merged:
  1. Own repos — /repos/{user}/{repo}/commits for every public non-fork
     repo the user owns.
  2. External contributions — /search/commits?q=author:{user}, which
     surfaces merged PRs and direct commits to repos the user does NOT own
     (e.g. upstream contributions). Own-repo results from search are
     dropped; the dedicated own-repo crawler handles those.

Only public repos are surfaced. We filter defensively on `private`,
`fork`, and `archived`.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

GITHUB_USER = "paulpessoa"
DAYS_BACK = 14
MAX_REPOS = 15
MAX_COMMITS_PER_REPO = 8
MAX_PROJECTS = 8
OUTPUT_PATH = "NOW.md"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Repos to skip even if they match the public filter (e.g. the profile repo itself).
SKIP_REPOS = {"paulpessoa"}


def gh(path: str) -> dict | list:
    url = f"https://api.github.com{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "now-working-refresher",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def list_public_repos() -> list[str]:
    repos = gh(f"/users/{GITHUB_USER}/repos?type=public&sort=pushed&per_page=100")
    out: list[str] = []
    for r in repos:
        if r.get("fork") or r.get("archived") or r.get("private"):
            continue
        name = r["name"]
        if name.lower() in SKIP_REPOS:
            continue
        out.append(name)
    return out[:MAX_REPOS]


NOISE_PREFIXES = (
    "merge pull request",
    "merge branch",
    "merge remote-tracking",
    "bump ",
    "update dependency ",
    "chore(deps):",
    "build(deps):",
)


def is_noise(msg: str) -> bool:
    m = msg.lower().lstrip()
    return m.startswith(NOISE_PREFIXES)


def recent_commits(repo: str) -> list[dict]:
    # Over-fetch so we can drop noise (merges, dependabot bumps) and still have
    # enough meaningful commits to surface.
    fetch_per_repo = MAX_COMMITS_PER_REPO * 4
    since = (datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    try:
        commits = gh(
            f"/repos/{GITHUB_USER}/{repo}/commits"
            f"?since={since}&per_page={fetch_per_repo}&author={GITHUB_USER}"
        )
    except urllib.error.HTTPError as e:
        print(f"  skip {repo}: {e.code}")
        return []
    except Exception as e:
        print(f"  skip {repo}: {e}")
        return []

    out = []
    for c in commits:
        author = (c.get("author") or {}).get("login", "") or ""
        if author.lower() in {"dependabot[bot]", "github-actions[bot]"}:
            continue
        msg = c["commit"]["message"].split("\n", 1)[0][:120]
        if is_noise(msg):
            continue
        out.append(
            {
                "repo": repo,
                "sha": c["sha"][:7],
                "msg": msg,
                "date": c["commit"]["author"]["date"],
            }
        )
        if len(out) >= MAX_COMMITS_PER_REPO:
            break
    return out


def external_contributions() -> list[dict]:
    """Commits authored by GITHUB_USER in repos owned by someone else.

    Uses the search/commits API, which indexes default-branch commits across
    all public repos. Requires an authenticated token (we pass GITHUB_TOKEN
    via gh()). Results are capped per-repo to mirror own-repo behavior.
    """
    since_dt = datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)
    try:
        data = gh(
            f"/search/commits?q=author:{GITHUB_USER}"
            f"&sort=author-date&order=desc&per_page=50"
        )
    except urllib.error.HTTPError as e:
        print(f"  search/commits error: {e.code}")
        return []
    except Exception as e:
        print(f"  search/commits error: {e}")
        return []

    out: list[dict] = []
    per_repo_count: dict[str, int] = {}
    for item in data.get("items", []):
        repo_info = item.get("repository") or {}
        if repo_info.get("fork") or repo_info.get("private"):
            continue
        owner_login = (repo_info.get("owner") or {}).get("login", "")
        if owner_login.lower() == GITHUB_USER.lower():
            continue  # own repo — covered by recent_commits()
        full_name = repo_info.get("full_name", "")
        if not full_name:
            continue

        date_str = item["commit"]["author"]["date"]
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt < since_dt:
            continue

        msg = item["commit"]["message"].split("\n", 1)[0][:120]
        if is_noise(msg):
            continue
        if per_repo_count.get(full_name, 0) >= MAX_COMMITS_PER_REPO:
            continue
        per_repo_count[full_name] = per_repo_count.get(full_name, 0) + 1
        out.append(
            {
                "repo": full_name,
                "sha": item["sha"][:7],
                "msg": msg,
                "date": date_str,
            }
        )
    return out


def humanize_ago(iso: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    delta = datetime.now(timezone.utc) - dt
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return "now"
    if hours < 24:
        return f"{int(hours)}h"
    days = int(hours // 24)
    if days == 1:
        return "1d"
    if days < 7:
        return f"{days}d"
    weeks = days // 7
    return f"{weeks}w"


def tag_for(msg: str) -> str:
    m = msg.lower()
    if any(x in m for x in ("wip", "scaffold", "draft", "experiment")):
        return "m"
    if any(x in m for x in ("fix", "bug", "patch", "hotfix")):
        return "c"
    return "g"


def group_by_repo(commits: list[dict]) -> list[dict]:
    """Group commits by repo, sorted by latest commit date (most recent first).

    Each group carries the full commit list (used by the summarizer prompt) plus
    derived display fields (count, latest sha, latest date).
    """
    groups: dict[str, dict] = {}
    for c in commits:
        g = groups.setdefault(
            c["repo"],
            {"repo": c["repo"], "commits": [], "latest_date": c["date"]},
        )
        g["commits"].append(c)
        if c["date"] > g["latest_date"]:
            g["latest_date"] = c["date"]

    out = list(groups.values())
    for g in out:
        # Newest commit first inside each project, so latest_sha is unambiguous.
        g["commits"].sort(key=lambda c: c["date"], reverse=True)
        g["count"] = len(g["commits"])
        g["latest_sha"] = g["commits"][0]["sha"]
    out.sort(key=lambda g: g["latest_date"], reverse=True)
    return out


def fallback_description(group: dict) -> str:
    """Used when Claude is unavailable or returns nothing for a project.

    Picks the newest non-trivial commit message and trims it.
    """
    msg = group["commits"][0]["msg"]
    return msg[:100]


def call_claude(prompt: str) -> str:
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 800,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())
    return "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    ).strip()


def extract_json_object(text: str) -> dict | None:
    """Pull the first {...} block out of text. Haiku sometimes wraps JSON in
    prose or fences even when told not to; this is defensive."""
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def summarize_groups(groups: list[dict]) -> tuple[str, dict[str, str]]:
    """Return (overall_summary, {repo: description}).

    One Claude call produces both the global summary line and per-project
    descriptions. Falls back to deterministic strings when the API is
    unavailable.
    """
    fallback_summary = "Building scalable applications, crafting high-performance PWAs, and integrating AI products."
    fallback_descs = {g["repo"]: fallback_description(g) for g in groups}

    if not ANTHROPIC_API_KEY or not groups:
        return fallback_summary, fallback_descs

    project_payload = [
        {
            "repo": g["repo"],
            "commit_count": g["count"],
            "messages": [c["msg"] for c in g["commits"][:MAX_COMMITS_PER_REPO]],
        }
        for g in groups
    ]

    prompt = (
        "You are summarizing recent git activity for Paul Pessoa's personal "
        "site. Below is JSON describing recent commits across his PUBLIC "
        "REPOSITORIES, grouped by project. Repo names are projects, not "
        "people. Examples: 'gaga-list' is an AI-powered smart grocery PWA, 'menvo' is a mentoring platform, 'estagionauta' is a career SaaS platform.\n\n"
        "Return ONLY a JSON object with this exact shape (no prose, no fences):\n"
        "{\n"
        '  "summary": "<one sentence, max 160 chars>",\n'
        '  "projects": [\n'
        '    {"repo": "<repo name>", "description": "<one clause, max 100 chars>"}\n'
        "  ]\n"
        "}\n\n"
        "REQUIREMENTS:\n"
        "- Include EVERY repo from the input in projects, using the exact repo "
        "name as a key.\n"
        "- summary: 2-4 most active projects, third person, present tense, no "
        "  hype words. Refer to repos as projects (e.g. 'polishing gaga-list's "
        "  features'). Do NOT name Paul.\n"
        "- description per project: capture the THEME of the work, not a list "
        "  of commits. Lowercase verb start preferred (e.g. 'shipping v0.6 "
        "  iframe interaction support'). Skip filler like 'working on'.\n"
        "- No quotes around clauses, no prefixes like 'Summary:'.\n\n"
        "INPUT:\n"
        f"{json.dumps(project_payload, indent=2)}\n"
    )

    try:
        text = call_claude(prompt)
    except urllib.error.HTTPError as e:
        print(f"anthropic api error: {e.code} {e.read().decode()[:200]}")
        return fallback_summary, fallback_descs
    except Exception as e:
        print(f"anthropic api error: {e}")
        return fallback_summary, fallback_descs

    parsed = extract_json_object(text)
    if not parsed:
        print(f"  could not parse JSON from claude response: {text[:200]}")
        return fallback_summary, fallback_descs

    summary = (parsed.get("summary") or "").replace("\n", " ").strip()[:160]
    descs: dict[str, str] = dict(fallback_descs)
    for entry in parsed.get("projects") or []:
        repo = entry.get("repo")
        desc = (entry.get("description") or "").replace("\n", " ").strip()
        if repo and desc:
            descs[repo] = desc[:100]

    return summary or fallback_summary, descs


def write_now_md(summary: str, projects: list[dict]) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "---",
        f"updatedAt: {now}",
        f"summary: {json.dumps(summary)}",
        "projects:",
    ]
    for p in projects:
        entry = (
            f"  - {{ repo: {json.dumps(p['repo'])}, "
            f"commits: {p['count']}, "
            f"msg: {json.dumps(p['msg'])}, "
            f"latest_sha: {json.dumps(p['latest_sha'])}, "
            f"ts: {json.dumps(p['ts'])}, "
            f"tag: {p['tag']} }}"
        )
        lines.append(entry)
    lines += ["---", ""]
    lines += [
        "## now.working",
        "",
        "_Auto-updated nightly. Public-repo commits only._",
        "",
        f"**Right now:** {summary}",
        "",
        "| Project | Commits | Activity | Latest |",
        "|---|---|---|---|",
    ]
    for p in projects:
        lines.append(
            f"| {p['repo']} | {p['count']} | {p['msg']} | {p['ts']} |"
        )
    lines.append("")
    with open(OUTPUT_PATH, "w") as f:
        f.write("\n".join(lines))
    print(f"Wrote {OUTPUT_PATH} ({len(projects)} projects).")


def main() -> None:
    print(f"Listing public repos for {GITHUB_USER}...")
    repos = list_public_repos()
    print(f"  {len(repos)} repos: {', '.join(repos)}")

    print("Collecting recent commits (own repos)...")
    all_commits: list[dict] = []
    for r in repos:
        all_commits.extend(recent_commits(r))

    print("Collecting external contributions via search/commits...")
    all_commits.extend(external_contributions())

    seen: set[tuple[str, str]] = set()
    deduped: list[dict] = []
    for c in all_commits:
        key = (c["repo"], c["sha"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)
    all_commits = deduped
    all_commits.sort(key=lambda c: c["date"], reverse=True)

    print(f"  {len(all_commits)} commits in last {DAYS_BACK} days")

    groups = group_by_repo(all_commits)[:MAX_PROJECTS]
    print(f"  {len(groups)} projects: {', '.join(g['repo'] for g in groups)}")

    print("Summarizing via Claude Haiku...")
    summary, descs = summarize_groups(groups)
    print(f"  summary: {summary}")

    display = []
    for g in groups:
        msg = descs.get(g["repo"]) or fallback_description(g)
        display.append(
            {
                "repo": g["repo"],
                "count": g["count"],
                "msg": msg,
                "latest_sha": g["latest_sha"],
                "ts": humanize_ago(g["latest_date"]),
                "tag": tag_for(msg),
            }
        )
    write_now_md(summary, display)


if __name__ == "__main__":
    main()
