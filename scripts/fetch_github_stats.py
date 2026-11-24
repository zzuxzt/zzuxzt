#!/usr/bin/env python3
"""Fetch Awesome GitHub Stats-compatible data directly from GitHub GraphQL.

Uses only Python's standard library. Read GITHUB_TOKEN, falling back to GH_TOKEN.
The source metadata documents retained legacy counting rules and intentional
corrections. Missing, inaccessible or truncated data raises FetchError, never a
zero-filled result. No request is made to the retired card-rendering service.
"""

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
ENDPOINT = "https://api.github.com/graphql"
MAX_REPOSITORIES = 100
RANK_POINTS = {
    "commits": 1,
    "contributedToNotOwnerRepositories": 10,
    "contributedToOwnRepositories": 1,
    "createdRepositories": 1,
    "directStars": 3.5,
    "followers": 1,
    "indirectStars": 1,
    "issues": 1,
    "pullRequests": 1,
    "pullRequestsToAnotherRepositories": 5,
    "commitsToMyRepositories": 1,
    "commitsToAnotherRepositories": 10,
    "contributedTo": 1,
}

USER_QUERY = """
query UserOverview($login: String!) {
  user(login: $login) {
    login
    name
    contributionsCollection { contributionYears }
    pullRequests(first: 1) { totalCount }
    issues(first: 1) { totalCount }
    followers { totalCount }
  }
}
"""

YEAR_QUERY = """
query ContributionsForYear($login: String!, $from: DateTime!, $to: DateTime!, $limit: Int!) {
  user(login: $login) {
    login
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      totalRepositoryContributions
      restrictedContributionsCount
      totalRepositoriesWithContributedCommits
      totalRepositoriesWithContributedPullRequests
      commitContributionsByRepository(maxRepositories: $limit) {
        contributions { totalCount }
        repository {
          id
          name
          nameWithOwner
          owner { login }
          stargazerCount
          isPrivate
        }
      }
      pullRequestContributionsByRepository(
        maxRepositories: $limit, excludeFirst: false, excludePopular: false
      ) {
        contributions { totalCount }
        repository {
          id
          name
          nameWithOwner
          owner { login }
          stargazerCount
          isPrivate
        }
      }
    }
  }
}
"""


class FetchError(ValueError):
    """A failed or incomplete fetch; callers must retain the previous card."""


def _object(value, label):
    if not isinstance(value, dict):
        raise FetchError(f"Missing or invalid {label}")
    return value


def _integer(value, label):
    if type(value) is not int or value < 0:
        raise FetchError(f"{label} must be a nonnegative integer")
    return value


def _string(value, label):
    if not isinstance(value, str) or not value.strip():
        raise FetchError(f"Missing or invalid {label}")
    return value


def _timestamp(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise FetchError("The fetch timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class GitHubClient:
    def __init__(self, token=None):
        self._token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not isinstance(self._token, str) or not self._token.strip():
            raise FetchError(
                "Set GITHUB_TOKEN or GH_TOKEN to a GitHub token with access to the "
                "user's contribution data. The default GitHub Actions token may lack "
                "that access; the workflow can instead use its GH_STATS_TOKEN secret."
            )

    def query(self, query, variables):
        body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        request = urllib.request.Request(
            ENDPOINT,
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + self._token,
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "github-profile-stats-repair",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                document = json.load(response)
        except urllib.error.HTTPError as error:
            detail = (
                " Check the token's access or API rate limit; the default Actions "
                "token may need to be replaced by the GH_STATS_TOKEN secret."
                if error.code in (401, 403, 429) else " Retry after GitHub recovers."
            )
            raise FetchError(f"GitHub API returned HTTP {error.code}.{detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise FetchError("GitHub API request failed or returned invalid JSON; no statistics saved.") from None
        _object(document, "GraphQL response")
        errors = document.get("errors")
        if errors:
            paths = []
            if isinstance(errors, list):
                for item in errors:
                    path = item.get("path") if isinstance(item, dict) else None
                    if isinstance(path, list) and all(
                        (isinstance(part, str) and part.replace("_", "").isalnum())
                        or type(part) is int for part in path
                    ):
                        paths.append(".".join(str(part)[:64] for part in path[:12]))
            path_detail = " Fields: " + ", ".join(paths[:4]) + "." if paths else ""
            messages = " ".join(
                str(item.get("message", "")) for item in errors if isinstance(item, dict)
            ) if isinstance(errors, list) else ""
            if any(term in messages.lower() for term in ("accessible", "forbidden", "credential", "permission")):
                raise FetchError(
                    "GitHub GraphQL denied access to requested data. The default "
                    "Actions token may be insufficient; use a suitable GH_STATS_TOKEN "
                    "secret mapped to GITHUB_TOKEN. No statistics saved." + path_detail
                )
            if "rate limit" in messages.lower():
                raise FetchError("GitHub GraphQL rate limit exceeded; retain the previous card and retry later.")
            raise FetchError("GitHub GraphQL returned errors; partial data was rejected and no statistics saved." + path_detail)
        return _object(document.get("data"), "GraphQL data")


def _year_repositories(collection, year, kind):
    field, total_field = (
        ("commitContributionsByRepository", "totalRepositoriesWithContributedCommits")
        if kind == "commit" else
        ("pullRequestContributionsByRepository", "totalRepositoriesWithContributedPullRequests")
    )
    expected = _integer(collection.get(total_field), f"{year}.{total_field}")
    entries = collection.get(field)
    if not isinstance(entries, list):
        raise FetchError(f"Missing repository list for {year} {kind}")
    if len(entries) != expected or len(entries) > MAX_REPOSITORIES:
        raise FetchError(
            f"Incomplete {year} {kind} repository list: received {len(entries)}, "
            f"GitHub reports {expected}, limit {MAX_REPOSITORIES}. "
            "Refusing to publish a truncated total."
        )
    seen, validated = set(), []
    for entry in entries:
        _object(entry, f"{year} {kind} contribution")
        repo = _object(entry.get("repository"), "repository")
        repository_id = _string(repo.get("id"), "repository.id")
        owner = _string(_object(repo.get("owner"), "repository.owner").get("login"), "owner.login")
        name = _string(repo.get("name"), "repository.name")
        full_name = _string(repo.get("nameWithOwner"), "repository.nameWithOwner")
        if full_name != f"{owner}/{name}":
            raise FetchError("Repository name and owner are inconsistent")
        if repository_id in seen:
            raise FetchError(f"Duplicate repository in one {year} {kind} list")
        seen.add(repository_id)
        if type(repo.get("isPrivate")) is not bool:
            raise FetchError("Missing repository visibility")
        if repo["isPrivate"]:
            raise FetchError(
                "The token exposed private repository details. Refusing to put "
                "private identities in a public audit file; use a token limited "
                "to the public profile repository."
            )
        count = _integer(
            _object(entry.get("contributions"), "repository.contributions").get("totalCount"),
            "contributions.totalCount",
        )
        if count == 0:
            raise FetchError("A contributed repository unexpectedly has zero contributions")
        validated.append({
            "id": repository_id,
            "name": name,
            "nameWithOwner": full_name,
            "owner": owner,
            "stars": _integer(repo.get("stargazerCount"), "repository.stargazerCount"),
            "year": year,
            "type": kind,
            "contributionCount": count,
        })
    return validated


def calculate_rank(stats):
    """Use the original default weights and the bundled original rank levels."""
    score = sum(_integer(stats.get(key), key) * weight for key, weight in RANK_POINTS.items())
    if not math.isfinite(score):
        raise FetchError("Invalid rank score")
    try:
        levels = json.loads((ROOT / "assets" / "rank-config.json").read_text(encoding="utf-8"))["levels"]
        level = max(
            (item for item in levels if score >= item["points"]),
            key=lambda item: item["points"],
        )["rank"]
    except (OSError, ValueError, KeyError, TypeError):
        raise FetchError("Invalid local rank configuration") from None
    return level, score


def fetch_payload(username="zzuxzt", token=None, *, client=None, now=None):
    """Return an old-/rank-compatible payload plus repository and source audit.

    client and now are optional deterministic test seams. Production callers can
    simply use fetch_payload("zzuxzt"). No token value is stored in the result.
    """
    _string(username, "username")
    started = now or datetime.now(timezone.utc)
    _timestamp(started)
    started = started.astimezone(timezone.utc)
    client = client or GitHubClient(token)
    data = client.query(USER_QUERY, {"login": username})
    user = _object(data.get("user"), "GitHub user")
    login = _string(user.get("login"), "user.login")
    if login.casefold() != username.casefold():
        raise FetchError("GitHub returned a different user")
    name = user.get("name")
    if name is not None and not isinstance(name, str):
        raise FetchError("Invalid user.name")
    years = _object(user.get("contributionsCollection"), "contributionsCollection").get("contributionYears")
    if not isinstance(years, list) or any(type(y) is not int or not 1 <= y <= started.year for y in years):
        raise FetchError("Invalid or missing contributionYears")
    if len(set(years)) != len(years):
        raise FetchError("Duplicate contributionYears")
    years = sorted(years, reverse=True)
    stats = {"login": login, "name": name}
    for field in ("pullRequests", "issues", "followers"):
        stats[field] = _integer(_object(user.get(field), field).get("totalCount"), f"{field}.totalCount")

    commits = created = 0
    commit_entries, pr_entries, ranges = [], [], []
    for year in years:
        begin = datetime(year, 1, 1, tzinfo=timezone.utc)
        end = started if year == started.year else datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        variables = {"login": login, "from": _timestamp(begin), "to": _timestamp(end), "limit": MAX_REPOSITORIES}
        year_user = _object(client.query(YEAR_QUERY, variables).get("user"), f"{year} user")
        if year_user.get("login") != login:
            raise FetchError(f"Unexpected user in {year} response")
        collection = _object(year_user.get("contributionsCollection"), f"{year} contributionsCollection")
        public_commits = _integer(collection.get("totalCommitContributions"), f"{year}.totalCommitContributions")
        restricted = _integer(collection.get("restrictedContributionsCount"), f"{year}.restrictedContributionsCount")
        created_count = _integer(collection.get("totalRepositoryContributions"), f"{year}.totalRepositoryContributions")
        year_commits = _year_repositories(collection, year, "commit")
        year_prs = _year_repositories(collection, year, "pr")
        if public_commits and not year_commits:
            raise FetchError(f"Commit total without accessible repository records in {year}")
        commits += public_commits + restricted
        created += created_count
        commit_entries.extend(year_commits)
        pr_entries.extend(year_prs)
        ranges.append({
            "year": year, "from": variables["from"], "to": variables["to"],
            "commitRepositories": len(year_commits), "pullRequestRepositories": len(year_prs),
            "totalCommitContributions": public_commits,
            "restrictedContributionsCount": restricted,
            "totalRepositoryContributions": created_count,
        })

    # Preserve upstream's precedence: all yearly commit lists, newest first,
    # followed by all yearly PR lists. Each repository contributes stars once.
    repositories = {}
    for entry in commit_entries + pr_entries:
        key = entry["id"]
        if key not in repositories:
            repositories[key] = {
                "id": key, "name": entry["name"], "nameWithOwner": entry["nameWithOwner"], "owner": entry["owner"],
                "stars": entry["stars"], "isOwn": entry["owner"].casefold() == login.casefold(),
                "legacyFirstContributionCount": entry["contributionCount"],
                "sources": [],
            }
        repositories[key]["sources"].append({
            "year": entry["year"], "type": entry["type"],
            "contributionCount": entry["contributionCount"], "observedStars": entry["stars"],
        })
    own = [repo for repo in repositories.values() if repo["isOwn"]]
    others = [repo for repo in repositories.values() if not repo["isOwn"]]
    own_legacy_count = sum(repo["legacyFirstContributionCount"] for repo in own)
    if commits < own_legacy_count:
        raise FetchError("Legacy own-repository contribution count exceeds total commits; refusing a negative statistic")
    finished = _timestamp(now or datetime.now(timezone.utc))
    stats.update({
        "lastFetch": finished,
        "commits": commits,
        "createdRepositories": created,
        "commitsToMyRepositories": own_legacy_count,
        "commitsToAnotherRepositories": commits - own_legacy_count,
        "pullRequestsToAnotherRepositories": sum(
            entry["contributionCount"] for entry in pr_entries
            if entry["owner"].casefold() != login.casefold()
        ),
        "directStars": sum(repo["stars"] for repo in own),
        "indirectStars": sum(repo["stars"] for repo in others),
        "contributedTo": len(repositories),
        "contributedToOwnRepositories": len(own),
        "contributedToNotOwnerRepositories": len(others),
    })
    level, score = calculate_rank(stats)
    return {
        "rankPoints": dict(RANK_POINTS), "userStats": stats, "level": level, "score": score,
        "repositories": sorted(repositories.values(), key=lambda repo: repo["nameWithOwner"].casefold()),
        "source_url": ENDPOINT,
        "source": {
            "provider": "GitHub GraphQL API", "endpoint": ENDPOINT,
            "fetchedAt": finished, "years": years, "yearRanges": ranges,
            "repositoryLimitPerYearPerType": MAX_REPOSITORIES,
            "repositoryListsCheckedAgainstTotals": True,
            "repositoryVisibility": "public; private repository identities are rejected",
            "starsDefinition": "Current stars of the unique union of commit and PR contribution repositories across all reported years",
            "legacySemantics": {
                "commits": "Sum of yearly totalCommitContributions plus restrictedContributionsCount; restricted contributions are not necessarily all commits",
                "commitsToMyRepositories": "Sum of each owned repository's first retained contributions.totalCount across years, with commit records preferred over PR records; this is not an all-time per-owner sum",
                "pullRequests": "All-time user.pullRequests.totalCount, all PR states",
                "issues": "All-time user.issues.totalCount",
                "rank": "Original default RankPoints and bundled rank-config.json",
            },
            "differencesFromOriginal": [
                "Stable GitHub repository IDs deduplicate repositories even if names or owners change between requests",
                "Ownership uses actual owner.login, not a substring match on nameWithOwner",
                "100 repository rows per year/type instead of the original default 25; incomplete lists fail closed",
                "UTC calendar-year intervals do not overlap; the current year ends at fetch start",
            ],
        },
    }


def write_payload(payload, output):
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".github-stats-", delete=False) as stream:
            temp_name = stream.name
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, output)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default="zzuxzt")
    parser.add_argument("--output", type=Path, required=True, help="Destination for validated JSON")
    args = parser.parse_args()
    write_payload(fetch_payload(args.username), args.output)


if __name__ == "__main__":
    try:
        main()
    except (FetchError, OSError, ValueError) as error:
        print(f"GitHub statistics fetch failed: {error}", file=sys.stderr)
        sys.exit(1)
