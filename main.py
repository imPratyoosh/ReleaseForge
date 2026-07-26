"""
main.py
=======

The central orchestrator for ReleaseForge.

Why this module exists
-----------------------
This is the entry point of the application. It wires together all the isolated
components we built in `src/`. It reads the configuration, checks the state, 
fetches updates, coordinates downloads, and dispatches to publishers.

Design Decisions
-----------------------
1. No Global State: The `ReleaseForge` class encapsulates all dependencies 
   (config, state, github, downloader, dispatcher). This makes the code 
   testable and prevents state leakage between runs.
2. Resilience: If one app crashes due to a bad regex or a network timeout, 
   the application catches the error, increments a failure counter, and 
   gracefully continues to the next app. 
3. Safe State Updates: The state is ONLY updated if the publishing phase 
   succeeds. If F-Droid fails to generate an index, we don't update `state.json`. 
   This ensures the script will try again on its next run.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

from src.config import ConfigError, load_config
from src.downloader import DownloaderError, ReleaseDownloader
from src.fdroid import FdroidError
from src.github import GitHubApiError, GitHubFetcher
from src.publisher import PublishDispatcher
from src.state import StateError, StateManager
from src.telegram import TelegramError

# Set up a root logger placeholder. It will be reconfigured based on config.toml.
logger = logging.getLogger("releaseforge")


@dataclass
class RunSummary:
    """Tracks statistics for the final execution report."""
    checked: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0

    def display(self) -> None:
        """Prints a clean summary to the console at the end of the run."""
        print("\n" + "=" * 40)
        print("🚀 ReleaseForge Run Summary")
        print("=" * 40)
        print(f"Apps checked: {self.checked}")
        print(f"Updated:      {self.updated}")
        print(f"Skipped:      {self.skipped}")
        print(f"Failed:       {self.failed}")
        print("=" * 40 + "\n")


class ReleaseForge:
    """
    The main application runner.
    Initializes dependencies and executes the core processing loop.
    """

    def __init__(self) -> None:
        """
        Loads configuration and initializes all subsystems.
        Exits the program immediately if the configuration or state is broken,
        as we cannot safely proceed without them.
        """
        try:
            self.config = load_config()
            self._setup_logging(self.config.logging.level)
            logger.info(f"Loaded config: {self.config.repository.name}")

            self.state = StateManager()
            self.github = GitHubFetcher()
            self.downloader = ReleaseDownloader()
            self.dispatcher = PublishDispatcher(self.config)
            
            self.summary = RunSummary()
            
        except ConfigError as exc:
            print(f"❌ Configuration Error: {exc}", file=sys.stderr)
            sys.exit(1)
        except StateError as exc:
            print(f"❌ State Error: {exc}", file=sys.stderr)
            sys.exit(1)

    def _setup_logging(self, level_name: str) -> None:
        """
        Configures console logging with clean formatting.
        """
        level = getattr(logging, level_name.upper(), logging.INFO)
        
        # We override the root logger to catch logs from all src/ modules
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            handlers=[logging.StreamHandler(sys.stdout)]
        )
        
        # Suppress overly verbose logs from third-party libraries
        logging.getLogger("requests").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    def run(self) -> None:
        """
        Executes the main pipeline for all enabled applications.
        """
        apps = self.config.enabled_apps()
        if not apps:
            logger.warning("No enabled apps found in config.toml. Exiting.")
            return

        logger.info(f"Starting run for {len(apps)} enabled applications.")

        for app in apps:
            self.summary.checked += 1
            self._process_app(app)

        # Save state changes to disk
        try:
            self.state.save()
        except StateError as exc:
            logger.error(f"Failed to save state at the end of the run: {exc}")

        # Print the final report
        self.summary.display()
        
        # If any apps failed, exit with a non-zero status code so GitHub Actions
        # flags the run as having issues (but still completes the workflow).
        if self.summary.failed > 0:
            sys.exit(1)

    def _process_app(self, app) -> None:
        """
        Processes a single application with strict error isolation.
        """
        logger.info(f"\n--- Processing {app.name} ({app.id}) ---")
        
        try:
            # 1. Fetch latest metadata from GitHub
            release = self.github.fetch_latest_update(app)
            
            # 2. Check if we already processed this version
            if not self.state.has_changed(app.id, release.version):
                logger.info(f"[{app.id}] Version {release.version} is already processed. Skipped.")
                self.summary.skipped += 1
                return

            logger.info(f"[{app.id}] 🌟 New update found: {release.version}")

            # 3. Filter and Download assets
            assets = self.downloader.process_release(app, release)
            
            # 4. Dispatch to publishers (Telegram, F-Droid, etc.)
            publish_success = self.dispatcher.dispatch(app, release, assets)

            # 5. Commit to state ONLY if publishing was flawless
            if publish_success:
                self.state.update(app.id, release.version)
                self.summary.updated += 1
                logger.info(f"[{app.id}] ✅ Successfully published version {release.version}")
            else:
                logger.warning(
                    f"[{app.id}] ⚠️ Publishing encountered errors. State not updated "
                    f"so we can retry on the next run."
                )
                self.summary.failed += 1

        except (GitHubApiError, DownloaderError, TelegramError, FdroidError) as exc:
            # These are known, expected errors (network timeouts, bad configs).
            logger.error(f"[{app.id}] Pipeline failed: {exc}")
            self.summary.failed += 1
            
        except Exception as exc:
            # Catch-all for unexpected crashes (e.g., unexpected JSON formats)
            # to ensure the loop continues to the next app.
            logger.exception(f"[{app.id}] ❌ Unexpected crash during processing: {exc}")
            self.summary.failed += 1


if __name__ == "__main__":
    app = ReleaseForge()
    app.run()
