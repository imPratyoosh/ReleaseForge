"""
src/fdroid.py
=============

Handles the generation and maintenance of a local F-Droid repository.

Why this module exists
-----------------------
To serve apps directly to Android devices without going through Google Play, 
we need an F-Droid compatible repository. F-Droid relies on an `index.jar` 
or `index-v1.json` file to tell the client app what updates are available.

This module acts as a Python wrapper around the official `fdroidserver` CLI 
tools. It takes the downloaded APKs, copies them into the correct directory 
structure, and triggers the metadata and index generation commands.

Design Decisions
-----------------------
1. Subprocess execution: `fdroidserver` is a complex CLI tool written in 
   Python, but its internal API is not stable for direct importing. Calling 
   it via `subprocess` is the officially supported and safest way to use it.
2. Idempotent Initialization: The module checks if a repository already 
   exists (by looking for `config.yml`) and initializes one cleanly if it 
   does not. This is perfect for CI environments where the repo folder 
   might be cached or freshly checked out.
3. Safe Cleanup: To prevent the Git repository from bloating endlessly, 
   this module deletes old APKs belonging to an app before generating the 
   new index. It uses the app's `asset_regex` to ensure it only deletes 
   APKs it actually manages.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

from src.config import AppConfig, FdroidConfig
from src.downloader import DownloadedAsset

logger = logging.getLogger(__name__)


class FdroidError(Exception):
    """
    Raised when F-Droid repository initialization, APK copying, or 
    index generation fails.
    """


class FdroidPublisher:
    """
    Manages a local F-Droid repository using the fdroidserver CLI.
    """

    def __init__(self, config: FdroidConfig) -> None:
        """
        Initializes the F-Droid publisher and ensures the target 
        directories exist.
        """
        self.config = config
        self.base_dir = self.config.repo_path.resolve()
        
        # F-Droid expects APKs in a specific subfolder named "repo"
        self.repo_dir = self.base_dir / "repo"
        
        # We don't initialize fdroid immediately here, because we only 
        # want to run it if F-Droid publishing is actually triggered.

    def publish(self, app: AppConfig, assets: list[DownloadedAsset]) -> None:
        """
        Copies downloaded APKs into the F-Droid repository and triggers 
        an index update.
        
        Args:
            app: The configuration for the app being processed.
            assets: The locally downloaded APK files.
            
        Raises:
            FdroidError: If the CLI tool fails or file operations fail.
        """
        if not self.config.enabled:
            logger.debug("F-Droid publishing is globally disabled. Skipping.")
            return
            
        if "fdroid" not in app.publish:
            logger.debug(f"[{app.id}] F-Droid not in publish targets. Skipping.")
            return

        logger.info(f"[{app.id}] Publishing to F-Droid repository...")

        self._ensure_initialized()
        
        # 1. Copy new assets into the repo directory
        copied_files = self._copy_assets(app, assets)
        
        # 2. Cleanup old versions of this specific app
        if self.config.keep_versions > 0:
            self._cleanup_old_versions(app, copied_files)
            
        # 3. Update the F-Droid index
        self._update_index()

    def _ensure_initialized(self) -> None:
        """
        Checks if the F-Droid repository structure exists. If not, initializes it.
        """
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        config_file = self.base_dir / "config.yml"

        if config_file.exists():
            return

        logger.info("F-Droid repository not found. Initializing a new one...")
        
        try:
            # 'fdroid init' creates the config.yml, keystore, and directory structure.
            # --quiet suppresses interactive prompts.
            subprocess.run(
                ["fdroid", "init", "--quiet"],
                cwd=self.base_dir,
                check=True,
                capture_output=True,
                text=True
            )
            logger.debug(f"Successfully initialized F-Droid repo at {self.base_dir}")
        except FileNotFoundError as exc:
            raise FdroidError(
                "The 'fdroid' command is not available in the system PATH. "
                "Ensure 'fdroidserver' is installed."
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise FdroidError(
                f"Failed to initialize F-Droid repo: {exc.stderr.strip()}"
            ) from exc

    def _copy_assets(
        self, app: AppConfig, assets: list[DownloadedAsset]
    ) -> list[Path]:
        """
        Copies downloaded assets to the F-Droid 'repo' directory.
        
        Returns:
            A list of Paths pointing to the newly copied files in the repo dir.
        """
        copied_paths = []
        for asset in assets:
            # We only publish APK files to F-Droid
            if not asset.original_name.lower().endswith(".apk"):
                logger.debug(f"[{app.id}] Skipping non-APK file for F-Droid: {asset.original_name}")
                continue

            target_path = self.repo_dir / asset.original_name
            
            try:
                # shutil.copy2 preserves file metadata (timestamps, etc.)
                shutil.copy2(asset.path, target_path)
                copied_paths.append(target_path)
                logger.debug(f"[{app.id}] Copied '{asset.original_name}' to F-Droid repo.")
            except OSError as exc:
                raise FdroidError(f"Failed to copy '{asset.original_name}' to repo: {exc}") from exc
                
        if not copied_paths:
            logger.warning(f"[{app.id}] No valid APKs were copied to the F-Droid repo.")
            
        return copied_paths

    def _cleanup_old_versions(self, app: AppConfig, recently_copied: list[Path]) -> None:
        """
        Deletes older APKs for this specific app to prevent repo bloat.
        
        Since one version might have multiple APKs (e.g., arm64, x86), we 
        allow (keep_versions * max_expected_apks) files to remain.
        """
        try:
            asset_regex = re.compile(app.asset_regex)
        except re.error:
            logger.warning(f"[{app.id}] Invalid regex '{app.asset_regex}', skipping cleanup.")
            return

        # Find all files in the repo dir that match this app's asset_regex
        matched_apks = [
            p for p in self.repo_dir.iterdir()
            if p.is_file() and asset_regex.match(p.name)
        ]

        # F-Droid keeps all architectures for a given version. If we have 3 
        # architectures configured, a single "version" means 3 files.
        max_files = self.config.keep_versions * max(1, len(app.architectures))

        if len(matched_apks) <= max_files:
            return

        # Sort by modification time, newest first
        matched_apks.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        # Separate the files we want to keep vs delete
        to_delete = matched_apks[max_files:]

        for old_apk in to_delete:
            # Defensive check: Never delete a file we *just* copied, 
            # even if the math somehow went wrong.
            if old_apk in recently_copied:
                continue

            try:
                old_apk.unlink()
                logger.info(f"[{app.id}] Deleted old F-Droid asset: {old_apk.name}")
            except OSError as exc:
                logger.warning(f"[{app.id}] Failed to delete old asset {old_apk.name}: {exc}")

    def _update_index(self) -> None:
        """
        Runs `fdroid update` to scan the new APKs and generate the repo index.
        """
        logger.info("Generating F-Droid repository index...")
        
        try:
            # --create-metadata ensures it extracts icons and details from new APKs
            # --rename-apks is sometimes useful, but we skip it to keep filenames intact
            result = subprocess.run(
                ["fdroid", "update", "--create-metadata", "--quiet"],
                cwd=self.base_dir,
                check=True,
                capture_output=True,
                text=True
            )
            
            if result.stdout:
                logger.debug(f"fdroid update output: {result.stdout.strip()}")
                
            logger.info("Successfully updated F-Droid index.")
            
        except subprocess.CalledProcessError as exc:
            # F-Droid can sometimes exit with a non-zero status for non-fatal 
            # warnings (like unsigned APKs). We log the error but do not crash 
            # the entire pipeline, as the index might still have been generated.
            logger.error(
                f"fdroid update returned error code {exc.returncode}. "
                f"Stderr: {exc.stderr.strip()}"
            )
            raise FdroidError("Failed to update F-Droid index. See logs for details.") from exc

