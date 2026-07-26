"""
src/state.py
==============

Manages the persistent state of ReleaseForge across GitHub Actions runs.

Why this module exists
-----------------------
Since this application runs on a schedule inside ephemeral GitHub Actions
runners, it has no persistent memory or database. Without state, it would
download and publish the same APKs every single time it runs.

This module reads and writes a simple `state.json` file. It tracks the
latest processed version of each app (keyed by the immutable `id` defined
in config.toml). 

By committing this `state.json` file back to the repository at the end of
the GitHub Actions workflow, we achieve a serverless, database-free
persistence layer perfectly suited for GitOps.

Atomic Saves & Safety
-----------------------
Writing directly to `state.json` can be risky if the runner crashes mid-write,
leaving a corrupted JSON file. To prevent this, the `StateManager` writes to
a temporary file first, then atomically renames it.

Extensibility
-----------------------
Currently, we only track the `version` string. However, the `AppState`
dataclass and the JSON structure are designed as nested objects rather
than simple key-value pairs (e.g., `{"youtube": {"version": "1.0"}}` instead
of `{"youtube": "1.0"}`). This allows us to easily add fields later, such
as `last_commit_sha`, `published_date`, or `asset_hash`, without breaking
backward compatibility.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Setting up a module-level logger. The main app will configure the handler.
logger = logging.getLogger(__name__)


class StateError(Exception):
    """
    Raised when the state file cannot be read, parsed, or written.
    
    Like ConfigError, keeping this specific allows the main orchestrator
    to handle state corruption gracefully rather than crashing with raw
    IOErrors or JSONDecodeErrors.
    """


@dataclass
class AppState:
    """
    Represents the current known state of a single tracked application.
    
    Using a dataclass allows us to easily serialize/deserialize this data
    and guarantees we always have expected fields, defaulting gracefully
    if an older state.json is missing newer fields.
    """
    version: str
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppState:
        """Safely initialize an AppState from a raw JSON dictionary."""
        return cls(
            version=data.get("version", ""),
        )


class StateManager:
    """
    Loads, queries, updates, and saves the application state.
    
    This class keeps the state in memory during the application's runtime.
    The main orchestrator should call `.save()` at the end of the run
    to persist any updates to disk.
    """

    def __init__(self, state_file: str | Path = "state.json") -> None:
        """
        Initialize the StateManager.
        
        Args:
            state_file: Path to the JSON file where state is stored.
                        Defaults to "state.json" in the current directory.
        """
        self.state_file = Path(state_file)
        self._state: dict[str, AppState] = {}
        self._load()

    def _load(self) -> None:
        """
        Load state from disk into memory.
        
        If the file does not exist, it initializes an empty state (perfect
        for the very first run of the application).
        
        Raises:
            StateError: If the file exists but contains invalid JSON or
                        is not a JSON object (dict).
        """
        if not self.state_file.exists():
            logger.info(f"State file '{self.state_file}' not found. Starting fresh.")
            self._state = {}
            return

        try:
            raw_text = self.state_file.read_text(encoding="utf-8")
            if not raw_text.strip():
                # Treat an empty file the same as a missing file
                self._state = {}
                return
                
            data = json.loads(raw_text)
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"Failed to read or parse state file: {exc}") from exc

        if not isinstance(data, dict):
            raise StateError(
                f"Invalid state format in {self.state_file}. "
                f"Expected a JSON object, got {type(data).__name__}."
            )

        # Deserialize the raw dictionary into AppState objects
        self._state = {
            app_id: AppState.from_dict(app_data)
            for app_id, app_data in data.items()
            if isinstance(app_data, dict)
        }
        
        logger.debug(f"Loaded state for {len(self._state)} apps.")

    def has_changed(self, app_id: str, latest_version: str) -> bool:
        """
        Check if an app has a new version that we haven't processed yet.
        
        Args:
            app_id: The immutable `id` of the app (from config.toml).
            latest_version: The version string just fetched from GitHub.
            
        Returns:
            True if the version is new or unseen. False if we already
            have this exact version in our state.
        """
        if app_id not in self._state:
            # We have never processed this app before
            return True
            
        known_version = self._state[app_id].version
        return known_version != latest_version

    def update(self, app_id: str, version: str) -> None:
        """
        Update the in-memory state for a specific app.
        
        Note: This does NOT write to disk immediately. `.save()` must be
        called later to persist this change.
        
        Args:
            app_id: The immutable `id` of the app.
            version: The newly processed version string.
        """
        self._state[app_id] = AppState(version=version)
        logger.debug(f"Updated in-memory state for '{app_id}' -> {version}")

    def save(self) -> None:
        """
        Write the in-memory state back to the JSON file safely.
        
        Uses an atomic write pattern (write to a temporary file, then
        rename) to ensure that if the script crashes during the write,
        the original state file is not corrupted.
        
        Raises:
            StateError: If writing to disk fails (e.g., permissions issue).
        """
        # Convert AppState objects back to standard dictionaries for JSON
        raw_data = {
            app_id: asdict(app_state)
            for app_id, app_state in self._state.items()
        }
        
        temp_file = self.state_file.with_suffix(".tmp")
        try:
            with temp_file.open("w", encoding="utf-8") as f:
                # Use indent=2 for readability so humans can easily inspect
                # state.json in the GitHub repository.
                json.dump(raw_data, f, indent=2, sort_keys=True)
                f.write("\n")  # Ensure file ends with a newline (POSIX standard)
                
            # Atomic replace (works on POSIX; Python 3.3+ handles this cleanly on Windows too)
            temp_file.replace(self.state_file)
            logger.info(f"State successfully saved to '{self.state_file}'.")
            
        except OSError as exc:
            # Clean up the temp file if something went wrong
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except OSError:
                    pass
            raise StateError(f"Failed to write state file: {exc}") from exc


if __name__ == "__main__":
    # Small manual smoke test
    logging.basicConfig(level=logging.DEBUG)
    
    print("--- Testing StateManager ---")
    manager = StateManager("test_state.json")
    
    # Check if a fake app needs an update
    if manager.has_changed("test-app", "v1.0.0"):
        print("test-app (v1.0.0) is NEW. Updating state...")
        manager.update("test-app", "v1.0.0")
    
    # Save it
    manager.save()
    print("State saved to test_state.json.")
    
    # Load it again to verify
    manager2 = StateManager("test_state.json")
    if not manager2.has_changed("test-app", "v1.0.0"):
        print("Success: StateManager recognized test-app v1.0.0 is already processed.")
        
    # Cleanup test file
    Path("test_state.json").unlink(missing_ok=True)

