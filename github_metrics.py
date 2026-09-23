"""
Generates data metrics for Year in Review.

Stripped and modified from super-changelog utils.js
to match the Year In Review template:

  - Top 10 repos      -> total commit count per repo
  - Locations         -> self-reported contributor location
  - Heat metrics      -> commit count/frequency + committer count per repo
  - Light metrics     -> stargazers (favorites) + subscribers (true watchers)
  - Love metrics      -> forks + merged PRs (upstream contributions)

Commit and contributor data is sourced from GitHub's contributor stats
endpoint (repo.get_stats_contributors) rather than paginating every
individual commit. That endpoint returns each contributor's weekly commit
counts for the repo's entire history in one (GitHub-cached) call, which is
what lets us compute both this period's totals and new-vs-returning
contributor status without walking full commit history per repo. The
trade-off: no per-commit message/url list, and no email address (stats are
keyed by GitHub account, not raw git author).
"""

from github import Github  # type: ignore
from github.GithubRetry import GithubRetry  # type: ignore
from github.StatsContributor import StatsContributor  # type: ignore
from datetime import datetime, timezone
import json
import os
import time
import shutil
import tempfile
import subprocess
import requests


class GithubMetrics:
    def __init__(self, token, filename=None, log_history_start=None, log_history_end=None):
        self.now = datetime.now(timezone.utc)
        self.log_history_start = log_history_start
        self.log_history_end = log_history_end

        self.timestamp = self.now.strftime("%Y-%m-%d")
        self.start_date = (
            datetime.strptime(log_history_start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if log_history_start
            else None
        )
        self.end_date = (
            datetime.strptime(log_history_end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if log_history_end
            else None
        )

        self.filename = filename
        self.token = token
        self.g = Github(token, per_page=100, lazy=True, retry=GithubRetry(total=0))

    def _check_rate_limit(self, buffer=200):
        try:
            core = self.g.get_rate_limit().resources.core
            if core.remaining < buffer:
                reset_time = core.reset.replace(tzinfo=timezone.utc)
                wait_seconds = (reset_time - datetime.now(timezone.utc)).total_seconds()
                if wait_seconds > 0:
                    print(
                        f"Rate limit low ({core.remaining} remaining). "
                        f"Sleeping {int(wait_seconds)}s until reset."
                    )
                    time.sleep(wait_seconds + 5)
        except Exception as e:
            print(f"Error checking rate limit: {e}")

    def _record_error(self, repo_data, message):
        """Log an error and flag this repo's data as incomplete, instead of
        letting a failed fetch look identical to a genuine zero in the
        output JSON."""
        print(f"  {message}")
        repo_data.setdefault("errors", []).append(message)

    def _in_period(self, dt):
        """Whether a tz-aware datetime falls within [start_date, end_date]."""
        if self.start_date and dt < self.start_date:
            return False
        if self.end_date and dt > self.end_date:
            return False
        return True

    def _get_repo_stats(self, repo, repo_data, max_wait=30, interval=2):
        # Calls the endpoint directly instead of repo.get_stats_contributors():
        # PyGithub's own version retries 202 responses forever internally, ignoring any timeout we set.
        url = f"{repo.url}/stats/contributors"
        headers = {"Authorization": f"token {self.token}", "Accept": "application/vnd.github+json"}
        deadline = time.monotonic() + max_wait
        self._check_rate_limit()
        while time.monotonic() < deadline:
            try:
                resp = requests.get(url, headers=headers, timeout=15)
            except Exception as e:
                self._record_error(repo_data, f"Error getting stats for {repo.name}: {e}")
                return []
            if resp.status_code == 202:
                time.sleep(interval)
                continue
            if resp.status_code != 200:
                self._record_error(repo_data, f"Error getting stats for {repo.name}: HTTP {resp.status_code}")
                return []
            return [StatsContributor(repo._requester, dict(resp.headers), attrs) for attrs in resp.json()]
        self._record_error(repo_data, f"Stats not ready for {repo.name} after {max_wait}s, skipping")
        return []

    # ---- Locations + Heat (committer side) -------------------------------

    def _get_contributors_via_git(self, repo, repo_data):
        """
        Clones the repository locally and uses git log to reliably extract
        contributor commit activity within [start_date, end_date], for repos
        where the stats API is unreliable (>100 contributors).

        Note: git log cannot provide GitHub `location`/`company` metadata,
        since those live on the GitHub user account, not in commit data.
        Those fields are set to None for contributors sourced this way.
        A contributor is "new" if their first-ever commit (across all of
        history) falls within the report period.
        """
        temp_dir = tempfile.mkdtemp()
        try:
            print(f"Cloning {repo.name} to parse git history...")
            clone_url = repo.clone_url
            if self.token:
                clone_url = clone_url.replace("https://", f"https://x-access-token:{self.token}@")

            # Full clone required (no --depth 1) since we need complete history
            # to determine each contributor's true first commit.
            subprocess.run(
                ["git", "clone", "--quiet", clone_url, temp_dir],
                check=True
            )

            git_log_cmd = [
                "git", "-C", temp_dir, "log",
                "--all",
                "--format=%an <%ae>|%aI"
            ]
            result = subprocess.run(git_log_cmd, capture_output=True, text=True, check=True)

            # author name -> earliest commit datetime seen (across all history)
            author_first_commit = {}
            # author name -> commit count within the report period
            author_period_commits = {}

            for line in result.stdout.strip().split("\n"):
                if not line or "|" not in line:
                    continue

                author, iso_date_str = line.split("|", 1)
                commit_dt = datetime.fromisoformat(iso_date_str)

                if author not in author_first_commit or commit_dt < author_first_commit[author]:
                    author_first_commit[author] = commit_dt

                in_period = (
                    (not self.start_date or commit_dt >= self.start_date) and
                    (not self.end_date or commit_dt <= self.end_date)
                )
                if in_period:
                    author_period_commits[author] = author_period_commits.get(author, 0) + 1

            contributors = {}
            for author, commit_count in author_period_commits.items():
                # name comes through as "Name <email>" from the format string above
                if "<" in author and ">" in author:
                    name = author.rsplit(" <", 1)[0]
                else:
                    name = author

                first_date = author_first_commit[author]
                is_new = (
                    (not self.start_date or first_date >= self.start_date) and
                    (not self.end_date or first_date <= self.end_date)
                )

                contributors[name] = {
                    "name": name,
                    "location": None,
                    "company": None,
                    "commit_count": commit_count,
                    "is_new": is_new,
                }

            repo_data["contributors"] = list(contributors.values())
            repo_data["committer_count"] = len(contributors)
            print(
                f"  Found {len(contributors)} committers via local git log "
                f"({sum(1 for c in contributors.values() if c['is_new'])} new)"
            )

        except Exception as e:
            self._record_error(repo_data, f"Error processing git log for {repo.name}: {e}")
            repo_data["contributors"] = []
            repo_data["committer_count"] = 0
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def get_contributors(self, repo, repo_data, stats):
        """New/active contributors for the period, including self-reported
        location (Locations section) and company (kept for context).
        A contributor is "new" if none of their commits (per the stats
        endpoint) predate the report period's start.

        For repos with more than 100 contributors, the stats endpoint
        becomes unreliable, so contributor data is instead derived by
        cloning the repo and parsing `git log` directly.
        """
        contributors_count = len(stats)
        if contributors_count > 50:
            try:
                contributors_count = repo.get_contributors().totalCount
            except Exception:
                contributors_count = 101  # fail safe -> use git log fallback

        if contributors_count > 100:
            print(
                f"Repository {repo.name} has more than 100 contributors. "
                f"Using local git log to determine contributor activity."
            )
            self._get_contributors_via_git(repo, repo_data)
            return

        try:
            contributors = {}

            for contributor in stats:
                author = contributor.author  # NamedUser, Organization, or None
                if author is None:
                    continue  # commits not linked to a GitHub account

                weeks_in_period = [w for w in contributor.weeks if w.c > 0 and self._in_period(w.w)]
                if not weeks_in_period:
                    continue

                had_prior_activity = any(
                    w.c > 0 and self.start_date and w.w < self.start_date for w in contributor.weeks
                )

                contributors[author.login] = {
                    "name": author.login,
                    "location": author.location,
                    "company": author.company,
                    "commit_count": sum(w.c for w in weeks_in_period),
                    "is_new": not had_prior_activity,
                }

            repo_data["contributors"] = list(contributors.values())
            repo_data["committer_count"] = len(contributors)
            print(f"  Found {len(contributors)} committers ({sum(1 for c in contributors.values() if c['is_new'])} new)")

        except Exception as e:
            self._record_error(repo_data, f"Error getting contributors for {repo_data['name']}: {e}")
            repo_data["contributors"] = []
            repo_data["committer_count"] = 0

    # ---- Top 10 repos + Heat (commit side) --------------------------------

    def get_commit_data(self, repo_data, stats):
        """Commit count/frequency, used for Top 10 ranking and Heat."""
        try:
            weekly_buckets = {}

            for contributor in stats:
                for week in contributor.weeks:
                    if week.c <= 0 or not self._in_period(week.w):
                        continue
                    week_key = week.w.strftime("%Y-W%W")
                    weekly_buckets[week_key] = weekly_buckets.get(week_key, 0) + week.c

            repo_data["total_commit_count"] = sum(weekly_buckets.values())
            repo_data["commits_per_week"] = weekly_buckets
            print(f"  Found {repo_data['total_commit_count']} commits")

        except Exception as e:
            self._record_error(repo_data, f"Error getting commit data for {repo_data['name']}: {e}")
            repo_data["total_commit_count"] = 0
            repo_data["commits_per_week"] = {}

    # ---- Light metrics -----------------------------------------------------

    def get_light_metrics(self, repo, repo_data):
        """Watchers (true subscribers) and stargazers (favorites)."""
        try:
            repo_data["stargazers_count"] = repo.stargazers_count
            # NOTE: repo.watchers_count is a v3 API alias for stargazers.
            # subscribers_count is the real "subscribed to updates" watcher count.
            repo_data["watchers_count"] = repo.subscribers_count
        except Exception as e:
            self._record_error(repo_data, f"Error getting light metrics for {repo.name}: {e}")
            repo_data["stargazers_count"] = 0
            repo_data["watchers_count"] = 0

    # ---- Love metrics --------------------------------------------------------

    def get_love_metrics(self, repo, repo_data):
        """Forks (copied projects) + merged PRs (changes contributed upstream)."""
        try:
            repo_data["forks_count"] = repo.forks_count
        except Exception as e:
            self._record_error(repo_data, f"Error getting fork count for {repo.name}: {e}")
            repo_data["forks_count"] = 0

        # merged_count lives outside the try so a mid-loop failure (e.g. a
        # timeout on one PR's merge check) keeps whatever was already
        # tallied instead of discarding the whole count back to 0.
        merged_count = 0
        try:
            if self.start_date:
                pulls = repo.get_pulls(state="closed", sort="updated", direction="desc")
                for i, pr in enumerate(pulls):
                    if i % 500 == 0:
                        self._check_rate_limit()
                    if pr.updated_at < self.start_date:
                        break  # sorted desc by update time; nothing older can match
                    if (
                        pr.merged_at
                        and pr.merged_at >= self.start_date
                        and (not self.end_date or pr.merged_at <= self.end_date)
                    ):
                        merged_count += 1
        except Exception as e:
            self._record_error(repo_data, f"Error getting merged PRs for {repo.name} (partial count kept): {e}")
        repo_data["merged_pr_count"] = merged_count

    # ---- Orchestration -------------------------------------------------------

    def get_data(self, org_name):
        # If a project registry file is present, use it instead of scanning
        # org_name's repos - one entry per line, either "owner/repo" (a
        # single repo) or a  org (every public repo under it).
        registry_path = "metrics_data/project_registry.txt"
        repos = []
        if os.path.exists(registry_path):
            with open(registry_path) as f:
                entries = [line.strip() for line in f if line.strip() and not line.startswith("#")]
            for entry in entries:
                self._check_rate_limit()
                try:
                    fetched = (
                        [self.g.get_repo(entry)] if "/" in entry
                        else self.g.get_organization(entry).get_repos(type="public")
                    )
                except Exception as e:
                    print(f"Error getting {entry}: {e}")
                    continue
                for repo in fetched:
                    self._check_rate_limit()
                    if not repo.archived:  # archived repos don't count toward this year's review
                        repos.append(repo)
        else:
            try:
                org = self.g.get_organization(org_name)
            except Exception as e:
                print(f"Error getting organization {org_name}: {e}")
                raise
            for repo in org.get_repos(type="public"):
                self._check_rate_limit()
                if not repo.archived:  # archived repos don't count toward this year's review
                    repos.append(repo)

        data = {
            "period": {"start": self.log_history_start, "end": self.log_history_end},
            "generated_at": self.now.isoformat(),
            "total_repo_count": 0,
            "repos": [],
        }

        total_repos = 0
        for repo in repos:
            total_repos += 1
            print(f"Processing repo: {repo.name}")

            repo_data = {
                "name": repo.name,
                "url": repo.html_url,
                "description": repo.description,
                "errors": [],
            }

            stats = self._get_repo_stats(repo, repo_data)
            self.get_commit_data(repo_data, stats)
            self.get_contributors(repo, repo_data, stats)
            self.get_light_metrics(repo, repo_data)
            self.get_love_metrics(repo, repo_data)

            # A repo with any recorded error has unreliable numbers (partial
            # or zeroed-out counts) rather than genuinely being inactive -
            # data_complete lets downstream rendering (or you) distinguish
            # "really 0" from "we couldn't fetch this."
            repo_data["data_complete"] = len(repo_data["errors"]) == 0

            data["repos"].append(repo_data)

        data["total_repo_count"] = total_repos

        # Pre-sort for the "Top 10 repos" section so downstream rendering
        # doesn't need to re-derive it.
        data["top_repos_by_commits"] = [
            r["name"]
            for r in sorted(data["repos"], key=lambda r: r["total_commit_count"], reverse=True)[:10]
        ]

        return data

    def save_data(self, data):
        if not self.filename:
            return None

        dirname = os.path.dirname(self.filename)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        with open(self.filename, "w") as f:
            json.dump(data, f, indent=2)

        return self.filename

    def get_and_save_data(self, org_name):
        data = self.get_data(org_name)
        return self.save_data(data)
