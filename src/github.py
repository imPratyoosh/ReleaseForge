"""
src/github.py
=============

Handles all communication with the GitHub REST API.

Why this module exists
-----------------------
To determine if an application needs updating, we must query its upstream
repository. GitHub provides different ways to track updates (Releases, Tags,
or Commits). This module abstracts those differences away.

Instead of letting HTTP request logic, rate limiting, and JSON parsing leak
into the main orchestrator, this module encapsulates it all. It exposes a
single `fetch_latest_update(app)` method that returns a clean, strongly-typed
`ReleaseData` object, regardless of whether the update came from a Git tag,
a GitHub Release, or a branch commit.

Authentication & Rate Limiting
------------------------------
Unauthenticated requests to the GitHub API are limited to 60 per hour per IP.
When running in GitHub Actions, this limit is easily exhausted. This module
automatically looks for a `GITHUB_TOKEN` environment variable and uses it to
authenticate, raising the limit to 1,000+ requests per hour.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import requests

from src.config import AppConfig

logger = logging.getLogger(__name__)


class GitHubApiError(Exception):
    """
    Raised when the GitHub API fails, rate-limits us, or returns
    unexpected data. Caught by main.py to gracefully skip the failing app.
    """


@dataclass(frozen=True)
class Asset:
    """
    Represents a downloadable file attached to a release or tag.
    """
    name: str
    download_url: str
    size: int


@dataclass(frozen=True)
class ReleaseData:
    """
    A unified representation of an update, regardless of its source
    (Release, Tag, or Commit).
    """
    version: str
    title: str
    changelog: str
    published_at: str
    html_url: str
    assets: list[Asset] = field(default_factory=list)


class GitHubFetcher:
    """
    Client for querying the GitHub API for repository updates.
    """

    BASE_URL = "https://api.github.com"

    def __init__(self) -> None:
        """
        Initializes the fetcher, configuring headers and authentication.
        """
        self.session = requests.Session()
        
        # GitHub strongly recommends custom User-Agents for API integrations.
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "ReleaseForge-Bot/1.0",
        }

        # Use GITHUB_TOKEN if available to avoid severe rate limits.
        # This is provided automatically by GitHub Actions.
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
            logger.debug("GitHubFetcher initialized with authentication token.")
        else:
            logger.warning(
                "GITHUB_TOKEN environment variable not found. "
                "API requests will be strictly rate-limited by GitHub."
            )

        self.session.headers.update(headers)

    def fetch_latest_update(self, app: AppConfig) -> ReleaseData:
        """
        Route the request to the correct strategy based on the app's config.
        
        Args:
            app: The AppConfig object defining what and how to track.
            
        Returns:
            A normalized ReleaseData object containing version and assets.
            
        Raises:
            GitHubApiError: If the fetch fails for any reason.
        """
        logger.info(f"Checking GitHub for '{app.name}' ({app.repository}) via {app.track}...")
        
        if app.track == "release":
            return self._fetch_latest_release(app)
        elif app.track == "tag":
            return self._fetch_latest_tag(app)
        elif app.track == "branch_commit":
            return self._fetch_latest_commit(app)
        else:
            # Should theoretically be caught by config.py validation, but we
            # program defensively to prevent unexpected behavior.
            raise GitHubApiError(f"Unsupported track strategy: {app.track}")

    def _fetch_latest_release(self, app: AppConfig) -> ReleaseData:
        """Fetch the latest release, respecting the prerelease filter."""
        url = f"{self.BASE_URL}/repos/{app.repository}/releases"
        data = self._make_request(url)

        if not data:
            raise GitHubApiError(f"No releases found for {app.repository}.")

        # Find the first release that matches our prerelease criteria
        target_release = None
        for release in data:
            is_prerelease = release.get("prerelease", False)
            if app.exclude_prerelease and is_prerelease:
                continue
            
            target_release = release
            break

        if not target_release:
            raise GitHubApiError(
                f"No suitable releases found for {app.repository} "
                f"(exclude_prerelease={app.exclude_prerelease})."
            )

        # Parse assets
        assets = [
            Asset(
                name=asset["name"],
                download_url=asset["browser_download_url"],
                size=asset["size"]
            )
            for asset in target_release.get("assets", [])
        ]

        return ReleaseData(
            version=target_release.get("tag_name", "unknown"),
            title=target_release.get("name") or target_release.get("tag_name", "Update"),
            changelog=target_release.get("body") or "No release notes provided.",
            published_at=target_release.get("published_at", ""),
            html_url=target_release.get("html_url", ""),
            assets=assets,
        )

    def _fetch_latest_tag(self, app: AppConfig) -> ReleaseData:
        """
        Fetch the most recent Git tag.
        Tags don't have direct release assets like APKs, so we provide the
        repository's source code tarball as the default asset.
        """
        url = f"{self.BASE_URL}/repos/{app.repository}/tags"
        data = self._make_request(url)

        if not data:
            raise GitHubApiError(f"No tags found for {app.repository}.")

        latest_tag = data[0]
        tag_name = latest_tag["name"]
        
        # GitHub provides automatic source code archives for every tag
        tarball_url = latest_tag.get("tarball_url", "")
        
        assets = []
        if tarball_url:
            assets.append(
                Asset(
                    name=f"{tag_name}-source.tar.gz",
                    download_url=tarball_url,
                    size=0  # Size is unknown without a HEAD request
                )
            )

        return ReleaseData(
            version=tag_name,
            title=f"Tag: {tag_name}",
            changelog=f"New tag created: {tag_name}",
            published_at="unknown",  # The tags endpoint doesn't return dates
            html_url=f"https://github.com/{app.repository}/releases/tag/{tag_name}",
            assets=assets,
        )

    def _fetch_latest_commit(self, app: AppConfig) -> ReleaseData:
        """
        Fetch the most recent commit on a specific branch.
        The "version" becomes the short SHA of the commit.
        """
        branch = app.branch or "main"
        url = f"{self.BASE_URL}/repos/{app.repository}/commits/{branch}"
        data = self._make_request(url)

        sha = data["sha"]
        short_sha = sha[:7]
        commit_info = data.get("commit", {})
        message = commit_info.get("message", "No commit message")
        date = commit_info.get("author", {}).get("date", "")

        # GitHub provides automatic source code archives for specific commits
        tarball_url = f"https://github.com/{app.repository}/archive/{sha}.tar.gz"

        assets = [
            Asset(
                name=f"{short_sha}-source.tar.gz",
                download_url=tarball_url,
                size=0
            )
        ]

        return ReleaseData(
            version=short_sha,
            title=f"Commit: {short_sha}",
            changelog=message,
            published_at=date,
            html_url=data.get("html_url", ""),
            assets=assets,
        )

    def _make_request(self, url: str) -> Any:
        """
        Internal helper to execute GET requests with error and rate-limit handling.
        """
        try:
            response = self.session.get(url, timeout=15)
            
            # Check for rate limiting specifically to provide a helpful error
            if response.status_code == 403 and "rate limit" in response.text.lower():
                reset_time = response.headers.get("X-RateLimit-Reset", "unknown time")
                raise GitHubApiError(
                    f"GitHub API rate limit exceeded. Resets at epoch {reset_time}. "
                    "Ensure GITHUB_TOKEN is set in your environment."
                )

            response.raise_for_status()
            return response.json()

        except requests.exceptions.Timeout as exc:
            raise GitHubApiError(f"Connection to GitHub timed out: {url}") from exc
        except requests.exceptions.RequestException as exc:
            raise GitHubApiError(f"HTTP Error querying GitHub API: {exc}") from exc
        except ValueError as exc:
            raise GitHubApiError("GitHub returned invalid JSON data.") from exc
