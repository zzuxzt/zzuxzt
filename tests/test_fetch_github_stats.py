"""Focused checks for the direct GitHub fetcher's counting and failure rules."""

import copy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("fetch_github_stats", ROOT / "scripts" / "fetch_github_stats.py")
fetcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetcher)
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def contribution(owner, name, stars, count, private=False, repository_id=None):
    return {
        "contributions": {"totalCount": count},
        "repository": {
            "id": repository_id or f"repo:{owner}/{name}",
            "name": name, "nameWithOwner": f"{owner}/{name}",
            "owner": {"login": owner}, "stargazerCount": stars, "isPrivate": private,
        },
    }


def year_data(commits, restricted, created, commit_repos, pr_repos):
    return {
        "totalCommitContributions": commits,
        "restrictedContributionsCount": restricted,
        "totalRepositoryContributions": created,
        "totalRepositoriesWithContributedCommits": len(commit_repos),
        "totalRepositoriesWithContributedPullRequests": len(pr_repos),
        "commitContributionsByRepository": commit_repos,
        "pullRequestContributionsByRepository": pr_repos,
    }


class FixtureClient:
    def __init__(self):
        self.calls = []
        self.overview = {
            "login": "zzuxzt", "name": "Zhanteng Xie",
            "contributionsCollection": {"contributionYears": [2026, 2025]},
            "pullRequests": {"totalCount": 10}, "issues": {"totalCount": 4},
            "followers": {"totalCount": 33},
        }
        self.years = {
            2026: year_data(200, 3, 2, [
                contribution("zzuxzt", "nav", 9, 8),
                contribution("lab", "research", 100, 10),
                contribution("friend", "zzuxzt-utils", 7, 2),
            ], [
                contribution("lab", "research", 100, 2),
                contribution("lab", "only-pr", 20, 1),
                contribution("zzuxzt", "pr-only", 3, 1),
            ]),
            2025: year_data(50, 0, 1, [
                contribution("zzuxzt", "nav", 9, 6),
                contribution("lab", "research", 100, 12),
                contribution("old", "retired", 5, 4),
            ], [
                contribution("lab", "research", 100, 1),
                contribution("lab", "only-pr", 20, 3),
                contribution("zzuxzt", "pr-only", 3, 2),
            ]),
        }

    def query(self, query, variables):
        self.calls.append(copy.deepcopy(variables))
        if query == fetcher.USER_QUERY:
            return {"user": copy.deepcopy(self.overview)}
        year = int(variables["from"][:4])
        return {"user": {"login": "zzuxzt", "contributionsCollection": copy.deepcopy(self.years[year])}}


class CountingTests(unittest.TestCase):
    def test_renamed_repository_keeps_one_star_total_by_stable_id(self):
        client = FixtureClient()
        client.years[2025]["commitContributionsByRepository"][1] = contribution(
            "previous-owner", "old-name", 100, 12,
            repository_id="repo:lab/research",
        )
        result = fetcher.fetch_payload(client=client, now=NOW)
        self.assertEqual(result["userStats"]["contributedTo"], 6)
        self.assertEqual(result["userStats"]["indirectStars"], 132)
        audit = {item["id"]: item for item in result["repositories"]}
        self.assertEqual(audit["repo:lab/research"]["nameWithOwner"], "lab/research")
        self.assertEqual(len(audit["repo:lab/research"]["sources"]), 4)

    def test_cross_year_commit_and_pr_union_counts_each_repository_once(self):
        result = fetcher.fetch_payload(client=FixtureClient(), now=NOW)
        stats = result["userStats"]
        self.assertEqual(stats["directStars"], 12)
        self.assertEqual(stats["indirectStars"], 132)
        self.assertEqual(stats["contributedTo"], 6)
        self.assertEqual(stats["contributedToOwnRepositories"], 2)
        self.assertEqual(stats["contributedToNotOwnerRepositories"], 4)
        audit = {item["nameWithOwner"]: item for item in result["repositories"]}
        self.assertEqual(len(audit["lab/research"]["sources"]), 4)
        self.assertEqual({item["type"] for item in audit["lab/research"]["sources"]}, {"commit", "pr"})
        # A username in another person's repository name must not imply ownership.
        self.assertFalse(audit["friend/zzuxzt-utils"]["isOwn"])
        self.assertIn("lab/only-pr", audit)
        self.assertIn("old/retired", audit)
        self.assertEqual(sum(repo["stars"] for repo in audit.values()), 144)

    def test_legacy_counts_and_weights_are_retained(self):
        result = fetcher.fetch_payload(client=FixtureClient(), now=NOW)
        stats = result["userStats"]
        self.assertEqual(stats["commits"], 253)  # 200 + 3 restricted + 50.
        self.assertEqual(stats["createdRepositories"], 3)
        self.assertEqual(stats["commitsToMyRepositories"], 9)  # First nav commit count 8 + PR-only count 1.
        self.assertEqual(stats["commitsToAnotherRepositories"], 244)
        self.assertEqual(stats["pullRequestsToAnotherRepositories"], 7)
        self.assertEqual(stats["pullRequests"], 10)
        self.assertEqual(stats["issues"], 4)
        self.assertEqual((result["level"], result["score"]), ("A+", 3009.0))

    def test_original_known_rank_snapshot_is_reproduced(self):
        original_stats = {
            "commits": 253, "contributedToNotOwnerRepositories": 21,
            "contributedToOwnRepositories": 5, "createdRepositories": 69,
            "directStars": 9, "followers": 33, "indirectStars": 2122,
            "issues": 4, "pullRequests": 8, "pullRequestsToAnotherRepositories": 8,
            "commitsToMyRepositories": 20, "commitsToAnotherRepositories": 233,
            "contributedTo": 26,
        }
        self.assertEqual(fetcher.calculate_rank(original_stats), ("A+", 5151.5))

    def test_nonoverlapping_calendar_years_and_explicit_limit(self):
        client = FixtureClient()
        result = fetcher.fetch_payload(client=client, now=NOW)
        by_year = {int(call["from"][:4]): call for call in client.calls if "from" in call}
        self.assertEqual(by_year[2026]["to"], "2026-11-24T12:00:00Z")
        self.assertEqual(by_year[2025]["to"], "2025-12-31T23:59:59Z")
        self.assertEqual(by_year[2026]["from"], "2026-01-01T00:00:00Z")
        self.assertTrue(all(call["limit"] == 100 for call in by_year.values()))
        self.assertTrue(result["source"]["repositoryListsCheckedAgainstTotals"])
        self.assertEqual(result["source_url"], "https://api.github.com/graphql")


class FailureTests(unittest.TestCase):
    def test_truncated_repository_list_is_rejected(self):
        client = FixtureClient()
        client.years[2026]["totalRepositoriesWithContributedCommits"] = 101
        with self.assertRaisesRegex(fetcher.FetchError, "Incomplete.*Refusing"):
            fetcher.fetch_payload(client=client, now=NOW)

    def test_missing_count_is_not_treated_as_zero(self):
        client = FixtureClient()
        del client.years[2025]["restrictedContributionsCount"]
        with self.assertRaises(fetcher.FetchError):
            fetcher.fetch_payload(client=client, now=NOW)

    def test_private_repository_identity_is_not_published(self):
        client = FixtureClient()
        client.years[2026]["commitContributionsByRepository"][0]["repository"]["isPrivate"] = True
        with self.assertRaisesRegex(fetcher.FetchError, "private repository details"):
            fetcher.fetch_payload(client=client, now=NOW)

    def test_graphql_partial_data_with_auth_error_is_rejected_without_token(self):
        document = {"data": {"user": {}}, "errors": [{"message": "Resource not accessible by integration", "path": ["user", "contributionsCollection"]}]}
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return json.dumps(document).encode()
        with patch.object(fetcher.urllib.request, "urlopen", return_value=Response()):
            with self.assertRaises(fetcher.FetchError) as error:
                fetcher.GitHubClient("test-only-credential").query("query {}", {})
        self.assertIn("GH_STATS_TOKEN", str(error.exception))
        self.assertIn("user.contributionsCollection", str(error.exception))
        self.assertNotIn("test-only-credential", str(error.exception))

    def test_cli_style_fetch_failure_leaves_existing_output_unchanged(self):
        client = FixtureClient()
        client.years[2026]["commitContributionsByRepository"][0]["repository"]["stargazerCount"] = -1
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "snapshot.json"
            output.write_bytes(b"previous-valid-snapshot")
            with self.assertRaises(fetcher.FetchError):
                fetcher.write_payload(fetcher.fetch_payload(client=client, now=NOW), output)
            self.assertEqual(output.read_bytes(), b"previous-valid-snapshot")


if __name__ == "__main__":
    unittest.main()
