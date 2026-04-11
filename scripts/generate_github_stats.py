#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

REST_API_URL = "https://api.github.com"
GRAPHQL_API_URL = "https://api.github.com/graphql"
USER_AGENT = "github-stats-card-generator"
ACTIVE_WINDOW_DAYS = 365
DEFAULT_YEAR_CARD_NAME = "stats-2026.svg"

OCTOCAT_PATH = (
    "M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 "
    "0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13"
    "-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07"
    "-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-"
    ".2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 "
    "2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 "
    "2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 "
    "2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"
)

CONTRIBUTIONS_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      totalRepositoriesWithContributedCommits
    }
  }
}
"""

SVG_TEMPLATE = """<svg xmlns="http://www.w3.org/2000/svg" width="360" height="200" viewBox="0 0 360 200">
  <style>
    text {{ font-family: 'Segoe UI', Ubuntu, 'Helvetica Neue', Sans-Serif; }}
    .title {{ fill: #3b4252; font-size: 21px; font-weight: 500; }}
    .label {{ fill: #2e3440; font-size: 13px; }}
    .value {{ fill: #2e3440; font-size: 15px; font-weight: 600; }}
    .icon {{ fill: #8fbcbb; }}
  </style>
  <rect x="1" y="1" width="358" height="198" rx="6" ry="6" fill="#eceff4" stroke="#e5e9f0" />
  <text x="24" y="38" class="title">{title}</text>
  <g transform="translate(24,58)">
{rows}
  </g>
  <g transform="translate(286,76) scale(3.85)" class="icon">
    <path fill-rule="evenodd" d="{octocat_path}"/>
  </g>
</svg>
"""


@dataclass(frozen=True)
class Repo:
    created_at: dt.datetime
    pushed_at: dt.datetime | None
    stars: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate profile stats SVG cards from live GitHub data."
    )
    parser.add_argument(
        "--username",
        default=os.getenv("GITHUB_USERNAME") or os.getenv("GITHUB_REPOSITORY_OWNER"),
        help="GitHub username to read stats from.",
    )
    parser.add_argument(
        "--output-dir",
        default="assets",
        help="Directory where the generated SVG cards should be written.",
    )
    parser.add_argument(
        "--timezone",
        default=os.getenv("GH_STATS_TIMEZONE", "UTC"),
        help="IANA timezone used for current-year boundaries.",
    )
    parser.add_argument(
        "--year-card-name",
        default=DEFAULT_YEAR_CARD_NAME,
        help="Keep the existing yearly stats filename so the README does not need to change.",
    )
    args = parser.parse_args()
    if not args.username:
        parser.error("missing GitHub username; pass --username or set GITHUB_USERNAME")
    return args


def resolve_token() -> str:
    token = os.getenv("GH_STATS_TOKEN") or os.getenv("GITHUB_TOKEN")
    if token:
        return token
    raise SystemExit("Missing GitHub token. Set GH_STATS_TOKEN or GITHUB_TOKEN.")


def isoformat_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def parse_github_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def build_headers(token: str, *, content_type: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {token}",
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def read_json(request: Request) -> Any:
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API request failed ({exc.code}): {message}") from exc
    except URLError as exc:
        raise RuntimeError(f"GitHub API request failed: {exc.reason}") from exc


def rest_get_json(path: str, token: str) -> Any:
    request = Request(f"{REST_API_URL}{path}", headers=build_headers(token))
    return read_json(request)


def graphql_query(query: str, variables: dict[str, Any], token: str) -> dict[str, Any]:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = Request(
        GRAPHQL_API_URL,
        data=payload,
        headers=build_headers(token, content_type="application/json"),
        method="POST",
    )
    response = read_json(request)
    if response.get("errors"):
        raise RuntimeError(f"GitHub GraphQL query failed: {response['errors']}")
    data = response.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("GitHub GraphQL query returned an unexpected response.")
    return data


def fetch_profile(username: str, token: str) -> dict[str, Any]:
    profile = rest_get_json(f"/users/{quote(username)}", token)
    if not isinstance(profile, dict):
        raise RuntimeError("GitHub user lookup returned an unexpected response.")
    return profile


def fetch_repositories(username: str, token: str) -> list[Repo]:
    repos: list[Repo] = []
    page = 1

    while True:
        batch = rest_get_json(
            f"/users/{quote(username)}/repos?per_page=100&page={page}&type=owner&sort=updated",
            token,
        )
        if not isinstance(batch, list):
            raise RuntimeError("GitHub repository lookup returned an unexpected response.")
        if not batch:
            break

        repos.extend(
            Repo(
                created_at=parse_github_datetime(item["created_at"]),
                pushed_at=parse_github_datetime(item.get("pushed_at")),
                stars=int(item.get("stargazers_count", 0)),
            )
            for item in batch
        )

        if len(batch) < 100:
            break
        page += 1

    return repos


def fetch_contribution_totals(
    username: str, token: str, from_dt: dt.datetime, to_dt: dt.datetime
) -> dict[str, int]:
    response = graphql_query(
        CONTRIBUTIONS_QUERY,
        {
            "login": username,
            "from": isoformat_utc(from_dt),
            "to": isoformat_utc(to_dt),
        },
        token,
    )
    user = response.get("user")
    if not isinstance(user, dict):
        raise RuntimeError(f"GitHub user '{username}' was not found in GraphQL.")

    contributions = user.get("contributionsCollection")
    if not isinstance(contributions, dict):
        raise RuntimeError("GitHub contributions lookup returned an unexpected response.")

    return {
        "commits": int(contributions["totalCommitContributions"]),
        "contributed_repos": int(contributions["totalRepositoriesWithContributedCommits"]),
    }


def render_rows(rows: list[tuple[str, int]]) -> str:
    fragments = []
    for index, (label, value) in enumerate(rows):
        fragments.append(
            f"""    <g transform="translate(0,{index * 26})">
      <circle cx="7" cy="7" r="6" class="icon" />
      <text x="22" y="11" class="label">{html.escape(label)}</text>
      <text x="224" y="11" text-anchor="end" class="value">{value}</text>
    </g>"""
        )
    return "\n".join(fragments)


def render_card(title: str, rows: list[tuple[str, int]]) -> str:
    return SVG_TEMPLATE.format(
        title=html.escape(title),
        rows=render_rows(rows),
        octocat_path=OCTOCAT_PATH,
    )


def write_if_changed(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    if existing == content:
        return False
    path.write_text(content, encoding="utf-8", newline="\n")
    return True


def main() -> int:
    args = parse_args()
    token = resolve_token()

    timezone = ZoneInfo(args.timezone)
    now_local = dt.datetime.now(timezone)
    now_utc = now_local.astimezone(dt.timezone.utc).replace(microsecond=0)
    year_start_local = dt.datetime(now_local.year, 1, 1, tzinfo=timezone)
    next_year_start_local = dt.datetime(now_local.year + 1, 1, 1, tzinfo=timezone)
    year_start_utc = year_start_local.astimezone(dt.timezone.utc)
    next_year_start_utc = next_year_start_local.astimezone(dt.timezone.utc)
    active_cutoff_utc = now_utc - dt.timedelta(days=ACTIVE_WINDOW_DAYS)

    profile = fetch_profile(args.username, token)
    repos = fetch_repositories(args.username, token)

    account_created_at = parse_github_datetime(profile.get("created_at"))
    if account_created_at is None:
        raise RuntimeError("GitHub profile did not include an account creation date.")

    overall_contributions = fetch_contribution_totals(
        args.username, token, account_created_at, now_utc
    )
    yearly_contributions = fetch_contribution_totals(
        args.username, token, year_start_utc, now_utc
    )

    public_repos = int(profile.get("public_repos", len(repos)))
    overall_active_repos = sum(
        1 for repo in repos if repo.pushed_at is not None and repo.pushed_at >= active_cutoff_utc
    )
    total_stars = sum(repo.stars for repo in repos)
    yearly_created_repos = sum(
        1
        for repo in repos
        if year_start_utc <= repo.created_at < next_year_start_utc
    )
    yearly_active_repos = [
        repo for repo in repos if repo.pushed_at is not None and repo.pushed_at >= year_start_utc
    ]
    yearly_project_stars = sum(repo.stars for repo in yearly_active_repos)

    overall_card = render_card(
        "Overall",
        [
            ("Public Repos", public_repos),
            ("Active Repos", overall_active_repos),
            ("Total Stars", total_stars),
            ("Total Commits", overall_contributions["commits"]),
            ("Contributed Repos", overall_contributions["contributed_repos"]),
        ],
    )
    yearly_card = render_card(
        f"{now_local.year} Activity",
        [
            ("Repos Created", yearly_created_repos),
            ("Active Repos", len(yearly_active_repos)),
            ("Commits", yearly_contributions["commits"]),
            ("Contributed Repos", yearly_contributions["contributed_repos"]),
            ("Project Stars", yearly_project_stars),
        ],
    )

    output_dir = Path(args.output_dir)
    overall_path = output_dir / "stats-overall.svg"
    year_path = output_dir / args.year_card_name

    overall_changed = write_if_changed(overall_path, overall_card)
    year_changed = write_if_changed(year_path, yearly_card)

    print(
        json.dumps(
            {
                "username": args.username,
                "timezone": args.timezone,
                "overall_card_changed": overall_changed,
                "year_card_changed": year_changed,
                "overall_output": str(overall_path),
                "year_output": str(year_path),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
