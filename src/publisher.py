"""
src/publisher.py
================

Orchestrates the distribution of downloaded assets to various destinations.

Why this module exists
-----------------------
An application might need to be published to F-Droid, announced on Telegram, 
and in the future, pushed to Discord, Matrix, or a website. 

Instead of hardcoding a messy block of `if wants_telegram: ... if wants_fdroid: ...` 
in the main loop, this module implements a Publisher Registry pattern. 

Design Decisions
-----------------------
1. Plugin Architecture (Protocol): We define a `Publisher` Protocol (an interface). 
   Any class that implements `publish(app, release, assets)` can act as a publisher. 
   Adding Discord support later just means writing `discord.py` and registering it here,
   without changing the orchestrator logic.
2. Fault Isolation: If Telegram's API is down, we do not want to crash the 
   script and prevent the F-Droid repository from updating. The `PublishDispatcher` 
   executes each publisher inside an isolated `try...except` block.
3. Global vs App-Level Toggles: Even if an app requests `"telegram"`, this 
   dispatcher checks if Telegram is enabled *globally* in `config.toml` before 
   attempting to use it.
"""

from __future__ import annotations

import logging
from typing import Protocol

from src.config import AppConfig, Config
from src.downloader import DownloadedAsset
from src.fdroid import FdroidPublisher
from src.github import ReleaseData
from src.telegram import TelegramPublisher

logger = logging.getLogger(__name__)


class Publisher(Protocol):
    """
    The structural type (interface) that all publisher modules must satisfy.
    This guarantees that the dispatcher can loop through any new publishers
    identically without caring about their internal implementation.
    """
    def publish(
        self, app: AppConfig, release: ReleaseData, assets: list[DownloadedAsset]
    ) -> None:
        ...


class PublishDispatcher:
    """
    Routes downloaded assets to the appropriate publishing channels.
    """

    def __init__(self, config: Config) -> None:
        """
        Initializes the dispatcher and registers enabled publishers.
        
        Args:
            config: The root configuration object, used to determine which 
                    publishers are globally enabled.
        """
        self._publishers: dict[str, Publisher] = {}

        # Register Telegram if globally enabled
        if config.telegram.enabled:
            self._publishers["telegram"] = TelegramPublisher(config.telegram)
            logger.debug("Registered publisher: Telegram")

        # Register F-Droid if globally enabled
        if config.fdroid.enabled:
            self._publishers["fdroid"] = FdroidPublisher(config.fdroid)
            logger.debug("Registered publisher: F-Droid")
            
        # Future publishers (Discord, Matrix, RSS) will be registered here.

    def dispatch(
        self, app: AppConfig, release: ReleaseData, assets: list[DownloadedAsset]
    ) -> bool:
        """
        Sends the release and assets to all requested, globally enabled publishers.
        
        Args:
            app: The configuration for the app being processed.
            release: The metadata fetched from GitHub.
            assets: The locally downloaded files ready for distribution.
            
        Returns:
            True if all attempted publishers succeeded. False if any failed.
            (Used by main.py to decide if it should fully commit the state).
        """
        if not app.publish:
            logger.info(f"[{app.id}] No publishers defined for this app. Skipping.")
            return True

        if not assets:
            logger.warning(f"[{app.id}] No downloaded assets provided. Skipping publishing.")
            return False

        all_successful = True

        for target in app.publish:
            # 1. Check if the publisher exists and is globally enabled
            if target not in self._publishers:
                logger.warning(
                    f"[{app.id}] Requested publisher '{target}' is either not "
                    f"implemented or globally disabled in config.toml. Skipping."
                )
                continue

            publisher = self._publishers[target]
            
            # 2. Execute the publisher, catching and isolating failures
            try:
                publisher.publish(app, release, assets)
            except Exception as exc:
                # Catch-all Exception ensures one failing publisher doesn't break the chain.
                # For example, if Telegram rejects a file, F-Droid still runs.
                logger.error(f"[{app.id}] Publisher '{target}' failed: {exc}")
                all_successful = False

        return all_successful

