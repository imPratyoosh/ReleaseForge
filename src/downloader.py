"""
src/downloader.py
=================

Handles the safe filtering and downloading of release assets.

Why this module exists
-----------------------
GitHub Releases often contain multiple assets: source code tarballs, debug
builds, signatures, and APKs for different CPU architectures. 
We do not want to download all of them. 

This module is responsible for applying the rules defined in `config.toml`
(like `asset_regex` and `architectures`) to filter down to the exact files
we care about. It then downloads them safely using chunked streaming and
atomic file operations.

Atomic Downloads & Safety
-----------------------
Network interruptions happen. If a download fails halfway through, we do not
want a corrupted, half-written `.apk` file lying around that F-Droid or 
Telegram might try to publish. This module writes to a `.tmp` file first, 
and only renames it to `.apk` once the download completes successfully.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from src.config import AppConfig
from src.github import Asset, ReleaseData

logger = logging.getLogger(__name__)


class DownloaderError(Exception):
    """
    Raised when asset filtering fails (e.g., no matching assets found)
    or a network/disk error occurs during download. Caught by main.py
    to safely skip to the next application.
    """


@dataclass(frozen=True)
class DownloadedAsset:
    """
    Represents a file successfully saved to disk, alongside the metadata
    needed by downstream publishers (F-Droid / Telegram).
    """
    path: Path
    original_name: str
    size_bytes: int


class ReleaseDownloader:
    """
    Filters and downloads release assets based on app configuration.
    """

    # Common substrings found in APK filenames mapped to our config architectures.
    # This helps reliably identify architectures even if the developer uses slight
    # variations (e.g., "arm64" vs "arm64-v8a").
    ABI_MAPPING = {
        "arm64-v8a": ["arm64-v8a", "arm64", "aarch64"],
        "armeabi-v7a": ["armeabi-v7a", "armv7", "armeabi"],
        "x86": ["x86"],
        "x86_64": ["x86_64", "x64"],
    }

    def __init__(self, base_download_dir: str | Path = "./downloads") -> None:
        """
        Initialize the Downloader.
        
        Args:
            base_download_dir: The root directory where assets will be saved.
        """
        self.base_download_dir = Path(base_download_dir)
        self.session = requests.Session()
        
        # A basic User-Agent is polite and prevents some basic firewall blocks.
        self.session.headers.update({
            "User-Agent": "ReleaseForge-Downloader/1.0",
        })

    def process_release(self, app: AppConfig, release: ReleaseData) -> list[DownloadedAsset]:
        """
        Main entry point. Filters the assets and downloads the matching ones.
        
        Args:
            app: The configuration defining the filtering rules.
            release: The raw release data fetched from GitHub.
            
        Returns:
            A list of successfully downloaded assets.
            
        Raises:
            DownloaderError: If no assets match, or a download fails.
        """
        if not release.assets:
            raise DownloaderError(f"No assets attached to release {release.version}.")

        matching_assets = self._filter_assets(app, release.assets)
        
        if not matching_assets:
            raise DownloaderError(
                f"Release {release.version} has {len(release.assets)} assets, "
                f"but none matched the config rules (regex: '{app.asset_regex}', "
                f"arch: {app.architectures})."
            )

        # Create a dedicated directory for this specific app and version
        # e.g., downloads/youtube-revanced/v2.1.0/
        app_dir = self.base_download_dir / app.id / release.version
        app_dir.mkdir(parents=True, exist_ok=True)

        downloaded = []
        for asset in matching_assets:
            file_path = self._download_file(asset.download_url, app_dir / asset.name)
            downloaded.append(
                DownloadedAsset(
                    path=file_path,
                    original_name=asset.name,
                    size_bytes=file_path.stat().st_size
                )
            )

        return downloaded

    def _filter_assets(self, app: AppConfig, assets: list[Asset]) -> list[Asset]:
        """
        Apply regex and architecture filtering to the list of assets.
        """
        filtered = []
        
        # Pre-compile regexes for performance (config.py already validated these)
        asset_regex = re.compile(app.asset_regex)
        exclude_regex = re.compile(app.exclude_regex) if app.exclude_regex else None

        for asset in assets:
            name = asset.name.lower()

            # 1. Must match the inclusion regex
            if not asset_regex.match(asset.name):
                logger.debug(f"[{app.id}] Asset '{asset.name}' skipped (failed regex).")
                continue

            # 2. Must NOT match the exclusion regex (if provided)
            if exclude_regex and exclude_regex.match(asset.name):
                logger.debug(f"[{app.id}] Asset '{asset.name}' skipped (matched exclude_regex).")
                continue

            # 3. Must match requested architectures
            if not self._matches_architecture(name, app.architectures):
                logger.debug(f"[{app.id}] Asset '{asset.name}' skipped (arch mismatch).")
                continue

            logger.info(f"[{app.id}] Found matching asset: {asset.name}")
            filtered.append(asset)

        return filtered

    def _matches_architecture(self, filename: str, requested_archs: list[str]) -> bool:
        """
        Determine if an APK filename satisfies the requested architectures.
        
        If "universal" is requested, and the filename doesn't contain any
        specific architecture string (like "arm64"), it is considered universal.
        """
        if "universal" in requested_archs:
            # An explicit "universal" tag in the filename guarantees it
            if "universal" in filename:
                return True
                
            # If it doesn't contain ANY specific ABI strings, assume it's universal
            contains_specific_abi = any(
                abi_str in filename 
                for aliases in self.ABI_MAPPING.values() 
                for abi_str in aliases
            )
            if not contains_specific_abi:
                return True

        # Check for specific requested architectures
        for arch in requested_archs:
            if arch in self.ABI_MAPPING:
                # If any of the known aliases for this arch are in the filename
                if any(alias in filename for alias in self.ABI_MAPPING[arch]):
                    return True

        return False

    def _download_file(self, url: str, dest_path: Path) -> Path:
        """
        Download a file securely in chunks to a temporary file, then rename it.
        
        Args:
            url: The download URL.
            dest_path: The final path where the file should reside.
            
        Returns:
            The Path to the successfully downloaded file.
            
        Raises:
            DownloaderError: On network failures or IO errors.
        """
        if dest_path.exists():
            logger.info(f"File {dest_path.name} already exists, skipping download.")
            return dest_path

        # Write to a .tmp file first
        tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")
        
        logger.info(f"Downloading {dest_path.name}...")
        try:
            with self.session.get(url, stream=True, timeout=30) as response:
                response.raise_for_status()
                
                with tmp_path.open("wb") as f:
                    # Download in 8KB chunks to keep memory usage low
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            
            # Atomic rename (safe on POSIX, supported on modern Windows)
            tmp_path.replace(dest_path)
            logger.debug(f"Successfully downloaded to {dest_path}")
            return dest_path
            
        except (requests.RequestException, OSError) as exc:
            # Clean up the broken download if possible
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise DownloaderError(f"Failed to download {dest_path.name}: {exc}") from exc

