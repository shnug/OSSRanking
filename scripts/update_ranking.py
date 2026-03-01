#!/usr/bin/env python3
"""
Automatically detect new China .NET community projects and update the ranking.

This script:
1. Reads projects.json to get currently tracked packages and known Chinese NuGet owners.
2. Queries the NuGet Search API to discover new packages published by known owners
   that have at least MIN_DOWNLOADS total downloads.
3. For each newly discovered package, uses the GitHub API to verify that the top 1
   or top 2 contributor is located in China (including Taiwan and Hong Kong).
4. Appends any qualifying packages to projects.json.
5. Regenerates the README.md ranking section, sorted by total NuGet downloads (descending).

Environment variables:
  GITHUB_TOKEN  – Personal access token (or Actions token) used for GitHub API calls.
                  Without this the script still works but is subject to the lower
                  unauthenticated rate limit (60 req/h).
"""

import json
import os
import re
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("requests library not found. Install it with: pip install requests", file=sys.stderr)
    sys.exit(1)

# Minimum total NuGet downloads required to appear in the ranking
MIN_DOWNLOADS = 20_000

NUGET_SEARCH_URL = "https://azuresearch-usnc.nuget.org/query"
GITHUB_API_BASE = "https://api.github.com"

# Location keywords indicating a user is based in China, Taiwan, or Hong Kong.
# Matched as case-insensitive substrings of the GitHub profile "location" field.
CHINESE_LOCATION_KEYWORDS = {
    # Countries / regions
    "china", "中国",
    "taiwan", "台湾", "台灣",
    "hong kong", "hongkong", "香港",
    "prc",
    # Major mainland cities
    "beijing", "北京",
    "shanghai", "上海",
    "shenzhen", "深圳",
    "guangzhou", "广州",
    "chengdu", "成都",
    "hangzhou", "杭州",
    "wuhan", "武汉",
    "nanjing", "南京",
    "tianjin", "天津",
    "xian", "西安",
    "suzhou", "苏州",
    "chongqing", "重庆",
    "zhengzhou", "郑州",
    "qingdao", "青岛",
    "ningbo", "宁波",
    # Taiwan cities
    "taipei", "台北",
}

# Seconds to wait between GitHub user-profile API calls to respect secondary rate limits.
# GitHub's secondary rate limit triggers at ~60-90 requests/minute for authenticated users.
GITHUB_API_DELAY_BETWEEN_USERS = 1.0
# Seconds to wait between checking different repos.
GITHUB_API_DELAY_BETWEEN_REPOS = 0.2


def extract_github_repo(url: str) -> str:
    """Extract 'owner/repo' from a GitHub URL, or return '' if not a GitHub URL."""
    if not url:
        return ""
    m = re.match(r"https?://github\.com/([^/]+/[^/?#\s]+)", url, re.IGNORECASE)
    if m:
        return m.group(1).rstrip("/").removesuffix(".git")
    return ""


def is_location_chinese(location: str) -> bool:
    """Return True if *location* indicates China (including Taiwan and Hong Kong)."""
    if not location:
        return False
    loc_lower = location.lower()
    return any(kw in loc_lower for kw in CHINESE_LOCATION_KEYWORDS)


def get_top_contributors(github_repo: str, token: str, n: int = 2) -> list:
    """
    Return the login names of the top *n* non-bot contributors for *github_repo*.
    Returns an empty list on any error.
    """
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(
            f"{GITHUB_API_BASE}/repos/{github_repo}/contributors",
            headers=headers,
            params={"per_page": n + 5, "anon": "false"},
            timeout=15,
        )
        resp.raise_for_status()
        logins = [
            c["login"]
            for c in resp.json()
            if c.get("type") != "Bot" and not c.get("login", "").endswith("[bot]")
        ]
        return logins[:n]
    except Exception as exc:
        print(f"  Warning: could not fetch contributors for {github_repo}: {exc}", file=sys.stderr)
        return []


def get_user_location(username: str, token: str) -> str:
    """Return the GitHub user's self-reported location string, or '' on failure."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(
            f"{GITHUB_API_BASE}/users/{username}",
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("location") or ""
    except Exception as exc:
        print(f"  Warning: could not fetch profile for {username}: {exc}", file=sys.stderr)
        return ""


def is_chinese_project(github_repo: str, token: str) -> bool:
    """
    Return True if the top 1 or top 2 contributor of *github_repo* is located in
    China (including Taiwan and Hong Kong).

    Conservative fallback: returns True (i.e., does not filter out the package)
    when the GitHub API is unreachable or contributor data is unavailable, to
    avoid false negatives caused by transient errors.
    """
    if not github_repo:
        # No GitHub URL yet – cannot verify; leave for manual review.
        return True
    contributors = get_top_contributors(github_repo, token)
    if not contributors:
        # Could not retrieve contributors; keep conservatively.
        return True
    for login in contributors:
        location = get_user_location(login, token)
        if is_location_chinese(location):
            return True
        time.sleep(GITHUB_API_DELAY_BETWEEN_USERS)  # respect GitHub secondary rate limit
    return False


def select_major_packages(candidates: list, tracked_ids: set) -> list:
    """
    From *candidates*, return only packages that are not sub-packages of any
    already-tracked package or of another candidate in the same batch.

    A package X is a sub-package of Y when X's NuGet ID starts with Y's NuGet
    ID followed by a period '.' (case-insensitive).  For example,
    'FreeSql.Provider.MySql' is a sub-package of 'FreeSql', and
    'Xunit.DependencyInjection.Logging' is a sub-package of
    'Xunit.DependencyInjection'.

    Precondition: *tracked_ids* must contain lower-cased IDs (as produced by
    ``{p["nuget_id"].lower() for p in tracked}``).
    """
    # Combine already-tracked IDs with all candidate IDs so that a parent package
    # discovered in the same batch can still suppress its children.
    all_ids_lower: set = set(tracked_ids)  # already lower-cased
    for p in candidates:
        all_ids_lower.add(p["nuget_id"].lower())

    result = []
    for pkg in candidates:
        lower_id = pkg["nuget_id"].lower()
        is_sub = any(
            lower_id.startswith(parent_id + ".")
            for parent_id in all_ids_lower
            if parent_id != lower_id
        )
        if is_sub:
            print(f"    SKIP (sub-package): {pkg['nuget_id']}")
        else:
            result.append(pkg)
    return result


# Shields.io badge org labels (github org -> badge markdown)
ORG_BADGES = {
    "dotnetcore": "![.NET Core Community](https://img.shields.io/badge/NCC-9e20c9.svg)",
    "SciSharp": "![SciSharp](https://img.shields.io/badge/SCISHARP-865fc3.svg)",
    "NewLifeX": "![NewLife](https://img.shields.io/badge/NEWLIFE-a6ca4d.svg)",
    "mini-software": "![.NET Foundation](https://img.shields.io/badge/DNF-2b0b98.svg)",
    "masastack": "",
    "ant-design-blazor": "![.NET Foundation](https://img.shields.io/badge/DNF-2b0b98.svg)",
    "arch": "![Arch](https://img.shields.io/badge/Arch-865f00.svg)",
}


def get_nuget_stats(package_id: str) -> int:
    """Return total download count for a single NuGet package, or 0 on failure."""
    try:
        resp = requests.get(
            NUGET_SEARCH_URL,
            params={"q": f"PackageId:{package_id}", "prerelease": "false", "take": 5},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        for pkg in data.get("data", []):
            if pkg.get("id", "").lower() == package_id.lower():
                return pkg.get("totalDownloads", 0)
    except Exception as exc:
        print(f"  Warning: could not fetch stats for {package_id}: {exc}", file=sys.stderr)
    return 0


def search_nuget_by_owner(owner: str) -> list:
    """Return all NuGet packages (with totalDownloads) published by *owner*."""
    packages = []
    skip = 0
    while True:
        try:
            resp = requests.get(
                NUGET_SEARCH_URL,
                params={"q": f"owner:{owner}", "take": 100, "skip": skip, "prerelease": "false"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            print(f"  Warning: NuGet search failed for owner '{owner}': {exc}", file=sys.stderr)
            break

        batch = data.get("data", [])
        packages.extend(batch)
        if len(batch) < 100:
            break
        skip += 100
        time.sleep(0.2)  # be polite to the API

    return packages


def build_entry(pkg: dict) -> str:
    """Build a README markdown list entry for *pkg*."""
    name = pkg["name"]
    nuget_id = pkg["nuget_id"]
    github = pkg.get("github", "")
    branch = pkg.get("branch", "master")
    downloads = pkg.get("total_downloads", 0)

    nuget_version_badge = (
        f"[![NuGet Version](https://img.shields.io/nuget/v/{nuget_id}.svg?style=flat)]"
        f"(https://www.nuget.org/packages/{nuget_id}/)"
    )
    nuget_download_badge = (
        f"[![NuGet](https://img.shields.io/nuget/dt/{nuget_id})]"
        f"(https://www.nuget.org/packages/{nuget_id})"
    )

    stars_badge = ""
    last_commit_badge = ""
    if github:
        stars_badge = (
            f'<img alt="Stars" src="https://img.shields.io/github/stars/{github}'
            f'?style=flat-square&labelColor=343b41"/>'
        )
        last_commit_badge = (
            f"[![last commit](https://img.shields.io/github/last-commit/{github}/{branch})]"
            f"(https://github.com/{github})"
        )

    # Determine org badge if any
    org_badge = ""
    if github:
        org = github.split("/")[0]
        org_badge = ORG_BADGES.get(org, "")

    parts = [f"- {name}", nuget_version_badge, nuget_download_badge]
    if stars_badge:
        parts.append(stars_badge)
    if downloads:
        # Format downloads as a human-readable badge value
        if downloads >= 1_000_000:
            dl_label = f"{downloads / 1_000_000:.1f}M"
        elif downloads >= 1_000:
            dl_label = f"{downloads / 1_000:.0f}k"
        else:
            dl_label = str(downloads)
        parts.append(f"![Total Downloads](https://img.shields.io/badge/totalDownloads-{dl_label}-blue)")
    if org_badge:
        parts.append(org_badge)
    if last_commit_badge:
        parts.append(last_commit_badge)

    return " ".join(parts)


def update_readme(packages_with_stats: list, readme_path: Path) -> None:
    """Rewrite the ranking section of README.md with *packages_with_stats* sorted by downloads."""
    with open(readme_path, encoding="utf-8") as f:
        content = f.read()

    # The ranking list starts after "# 中国.NET开源项目排行榜 China .NET OSS Ranking"
    # and the [Rank by Org] link, and ends at the last list item.
    # Note: "Orgnization" is an existing typo in README.md that must be preserved here.
    header_pattern = re.compile(
        r"(# 中国\.NET开源项目排行榜 China \.NET OSS Ranking\n\n"
        r"\[点击这里按组织排名 Rank by Orgnization\]\(RankingByOrg\.md\)\n"
        r" \n)"
        r"((?:- .+\n?)+)",
        re.MULTILINE,
    )

    qualifying = [p for p in packages_with_stats if p.get("total_downloads", 0) >= MIN_DOWNLOADS]
    qualifying.sort(key=lambda p: p.get("total_downloads", 0), reverse=True)

    new_list = "\n".join(build_entry(p) for p in qualifying) + "\n"

    def replace_section(m):
        return m.group(1) + new_list

    new_content, n = header_pattern.subn(replace_section, content)
    if n == 0:
        print("Warning: could not locate ranking section in README.md – skipping rewrite.", file=sys.stderr)
        return

    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    print(f"README.md updated with {len(qualifying)} packages.")


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    projects_path = root / "projects.json"
    readme_path = root / "README.md"

    github_token: str = os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        print(
            "Warning: GITHUB_TOKEN is not set. GitHub API calls will use the lower "
            "unauthenticated rate limit (60 req/h).",
            file=sys.stderr,
        )

    with open(projects_path, encoding="utf-8") as f:
        config = json.load(f)

    tracked: list = config["packages"]
    tracked_lower: set = {p["nuget_id"].lower() for p in tracked}

    # ------------------------------------------------------------------ #
    # 1. Fetch current download counts for all already-tracked packages   #
    # ------------------------------------------------------------------ #
    print("Fetching download counts for tracked packages …")
    for pkg in tracked:
        downloads = get_nuget_stats(pkg["nuget_id"])
        pkg["total_downloads"] = downloads
        print(f"  {pkg['nuget_id']}: {downloads:,}")
        time.sleep(0.1)

    # ------------------------------------------------------------------ #
    # 2. Discover new packages from known NuGet owners                    #
    # ------------------------------------------------------------------ #
    print("\nSearching NuGet for new packages from known owners …")
    # Collect all qualifying candidates first; sub-package filtering is applied
    # after all owners have been scanned so that a parent package discovered later
    # in the same run can still suppress its children.
    new_candidates: list = []
    candidate_ids_lower: set = set()  # dedup across owners within this run

    for owner in config.get("nuget_owners", []):
        print(f"  Scanning owner: {owner}")
        results = search_nuget_by_owner(owner)
        for pkg_data in results:
            pid = pkg_data.get("id", "")
            downloads = pkg_data.get("totalDownloads", 0)
            if pid.lower() in tracked_lower or pid.lower() in candidate_ids_lower:
                continue
            if downloads < MIN_DOWNLOADS:
                continue

            # Try to derive the GitHub repo from the NuGet package metadata so we
            # can verify that the project has a Chinese top contributor.
            project_url = pkg_data.get("projectUrl", "") or ""
            github_repo = extract_github_repo(project_url)

            # Skip packages whose top 1/2 contributors are not located in China.
            if github_repo:
                if not is_chinese_project(github_repo, github_token):
                    print(
                        f"    SKIP (not Chinese): {pid} – no Chinese top contributor found "
                        f"(repo: {github_repo})"
                    )
                    continue
                time.sleep(GITHUB_API_DELAY_BETWEEN_REPOS)  # polite pacing between repos

            # Build a minimal entry; GitHub URL may need manual correction.
            # 'branch' defaults to 'master'; maintainers should update it if the repo
            # uses 'main' or another default branch.
            # 'auto_detected' is persisted so reviewers can identify entries that
            # have not yet been fully verified.
            new_entry = {
                "name": pid,
                "nuget_id": pid,
                "github": github_repo,
                "branch": "master",
                "auto_detected": True,
                "total_downloads": downloads,
            }
            new_candidates.append(new_entry)
            candidate_ids_lower.add(pid.lower())

    # Filter out sub-packages; only major (root) packages enter the ranking.
    # A package X is a sub-package of Y when X's ID starts with Y's ID + '.'
    # e.g. 'FreeSql.Provider.MySql' is a sub-package of already-tracked 'FreeSql'.
    new_candidates = select_major_packages(new_candidates, tracked_lower)

    newly_added: list = []
    for entry in new_candidates:
        tracked.append(entry)
        tracked_lower.add(entry["nuget_id"].lower())
        newly_added.append(entry)
        print(f"    NEW: {entry['nuget_id']} ({entry['total_downloads']:,} downloads)")

    # ------------------------------------------------------------------ #
    # 3. Persist updated projects.json                                    #
    # ------------------------------------------------------------------ #
    # Strip runtime-only fields before saving (keep auto_detected for human review)
    save_packages = []
    for p in tracked:
        entry = {k: v for k, v in p.items() if k not in ("total_downloads",)}
        save_packages.append(entry)

    config["packages"] = save_packages
    with open(projects_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # ------------------------------------------------------------------ #
    # 4. Rewrite README.md ranking section                                #
    # ------------------------------------------------------------------ #
    print("\nUpdating README.md …")
    update_readme(tracked, readme_path)

    if newly_added:
        print(f"\n✅ {len(newly_added)} new package(s) detected and added to projects.json:")
        for p in newly_added:
            print(f"   - {p['nuget_id']} ({p['total_downloads']:,} downloads)")
        print("\nNote: Please verify these new packages are genuine China .NET community projects")
        print("and update their 'github' field in projects.json before merging.")
    else:
        print("\n✅ No new qualifying packages found.")


if __name__ == "__main__":
    main()
