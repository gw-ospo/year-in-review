// Points at one generated snapshot from metrics_data/data/ (see scripts/generate_metrics.py).
// Update this filename to feature a different run.
const DATA_FILE = "metrics_2025-08-01_to_2026-08-01.json";

const EMPTY_DATA = {
  period: { start: null, end: null },
  heat: { commits: 0, committers: 0 },
  light: { watchers: 0, stars: 0 },
  love: { forks: 0, prsMerged: 0 },
  topRepos: [],
  locations: [],
};

function sumBy(repos, key) {
  return repos.reduce((total, repo) => total + (repo[key] || 0), 0);
}

function uniqueCommitterCount(repos) {
  const logins = new Set();
  repos.forEach((repo) => {
    (repo.contributors || []).forEach((c) => logins.add(c.name));
  });
  return logins.size;
}

function topReposByCommits(repos, limit = 10) {
  return [...repos]
    .sort((a, b) => b.total_commit_count - a.total_commit_count)
    .slice(0, limit)
    .map((repo) => ({ name: repo.name, commits: repo.total_commit_count }));
}

function uniqueLocations(repos) {
  const locations = new Set();
  repos.forEach((repo) => {
    (repo.contributors || []).forEach((c) => {
      if (c.location) locations.add(c.location);
    });
  });
  return [...locations];
}

export async function loadYearInReviewData() {
  try {
    const res = await fetch(`data/${DATA_FILE}`);
    const raw = await res.json();
    const repos = raw.repos || [];

    return {
      period: raw.period || { start: null, end: null },
      heat: {
        commits: sumBy(repos, "total_commit_count"),
        committers: uniqueCommitterCount(repos),
      },
      light: {
        watchers: sumBy(repos, "watchers_count"),
        stars: sumBy(repos, "stargazers_count"),
      },
      love: {
        forks: sumBy(repos, "forks_count"),
        prsMerged: sumBy(repos, "merged_pr_count"),
      },
      topRepos: topReposByCommits(repos),
      locations: uniqueLocations(repos),
    };
  } catch (e) {
    console.error("Failed to load Year in Review data:", e);
    return EMPTY_DATA;
  }
}
