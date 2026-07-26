"""
src/telegram.py
===============

Handles formatting and publishing updates to a Telegram channel or chat.

Why this module exists
-----------------------
Telegram's Bot API is powerful but has strict limitations: messages cannot 
exceed 4096 characters, captions are limited to 1024 characters, HTML parsing
is extremely unforgiving (unclosed tags or raw `<` symbols will reject the
entire message), and direct file uploads are capped at 50MB.

This module abstracts these quirks away. It takes clean, internal data 
structures (`AppConfig`, `ReleaseData`, `DownloadedAsset`), safely escapes 
all text to prevent markup errors, applies templating, and manages the 
two-step process of sending a release announcement followed by attaching 
the APKs.

Design Decisions
-----------------------
1. Two-Step Publishing: Instead of trying to cram the changelog into a 
   tiny 1024-character document caption, we send a rich text message first, 
   then upload the APKs as replies to that initial message. This keeps the 
   channel organized and bypasses caption limits.
2. Graceful Fallback: If an APK exceeds the 50MB Bot API limit, the module 
   skips the upload but still sends the announcement (which includes the 
   download link), ensuring subscribers are still notified.
"""

from __future__ import annotations

import html
import logging
from typing import Any

import requests

from src.config import AppConfig, TelegramConfig
from src.downloader import DownloadedAsset
from src.github import ReleaseData

logger = logging.getLogger(__name__)


class TelegramError(Exception):
    """
    Raised when the Telegram API rejects a request or a network error occurs.
    Keeps failure isolated so `main.py` can continue with F-Droid publishing
    even if Telegram fails.
    """


class TelegramPublisher:
    """
    Client for formatting and sending messages/files to Telegram.
    """

    # Telegram's strict limits
    MAX_MESSAGE_LENGTH = 4096
    MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB limit for bots

    # Fallback template if the user didn't specify one in config.toml
    DEFAULT_TEMPLATE = (
        "🆕 <b>{app_name}</b>\n\n"
        "📦 <b>Version:</b> {version}\n"
        "📄 <b>Channel:</b> {channel}\n\n"
        "📋 <b>Changes:</b>\n"
        "{changelog}\n\n"
        "🔗 <a href=\"{release_url}\">View Release</a>"
    )

    def __init__(self, config: TelegramConfig) -> None:
        """
        Initializes the publisher with the global Telegram configuration.
        """
        self.config = config
        self.base_url = f"https://api.telegram.org/bot{config.bot_token}"
        self.session = requests.Session()
        
        # We don't set a custom User-Agent here because the Telegram Bot API 
        # doesn't require one, and requests handles multipart/form-data best 
        # with its default settings.

    def publish(
        self, app: AppConfig, release: ReleaseData, assets: list[DownloadedAsset]
    ) -> None:
        """
        The main entry point for publishing an app update to Telegram.
        
        1. Formats and sends the announcement message.
        2. Uploads all downloaded APKs as replies to that message.
        
        Args:
            app: The application configuration.
            release: The metadata fetched from GitHub.
            assets: The locally downloaded files ready for upload.
            
        Raises:
            TelegramError: If the initial message fails to send.
        """
        if not self.config.enabled:
            logger.debug("Telegram publishing is globally disabled. Skipping.")
            return
            
        if "telegram" not in app.publish:
            logger.debug(f"[{app.id}] Telegram not in publish targets. Skipping.")
            return

        logger.info(f"[{app.id}] Publishing to Telegram...")

        message_text = self._format_message(app, release)
        message_id = self._send_text_message(message_text)

        for asset in assets:
            self._send_document(app.id, asset, reply_to_message_id=message_id)

    def _format_message(self, app: AppConfig, release: ReleaseData) -> str:
        """
        Populate the template with safe, HTML-escaped data.
        """
        template = self.config.message_template or self.DEFAULT_TEMPLATE

        # Telegram's HTML parser crashes if it sees unescaped <, >, or & in standard text.
        # We MUST escape dynamic content like changelogs that developers might format weirdly.
        safe_name = html.escape(app.name)
        safe_version = html.escape(release.version)
        safe_channel = html.escape(app.release_channel.capitalize())
        safe_changelog = html.escape(release.changelog)

        text = template.format(
            app_name=safe_name,
            version=safe_version,
            channel=safe_channel,
            changelog=safe_changelog,
            release_url=release.html_url,
        )

        # Telegram will reject messages over 4096 characters.
        # If the changelog is massive, we truncate the message cleanly.
        if len(text) > self.MAX_MESSAGE_LENGTH:
            truncation_notice = "...\n<i>(Changelog truncated for length)</i>\n"
            allowed_length = self.MAX_MESSAGE_LENGTH - len(truncation_notice) - 100
            
            # Rebuild with a truncated changelog
            short_changelog = safe_changelog[:allowed_length]
            text = template.format(
                app_name=safe_name,
                version=safe_version,
                channel=safe_channel,
                changelog=short_changelog + truncation_notice,
                release_url=release.html_url,
            )

        return text

    def _send_text_message(self, text: str) -> int:
        """
        Sends the announcement message.
        
        Returns:
            The message_id of the sent message (used to thread replies).
            
        Raises:
            TelegramError: If the API rejects the message or a network error occurs.
        """
        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": self.config.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_notification": self.config.silent,
            "disable_web_page_preview": self.config.disable_web_page_preview,
        }

        response_data = self._make_request(url, json=payload)
        return response_data["result"]["message_id"]

    def _send_document(
        self, app_id: str, asset: DownloadedAsset, reply_to_message_id: int
    ) -> None:
        """
        Uploads a local file to Telegram as a reply to the main announcement.
        """
        max_allowed_bytes = min(
            self.MAX_FILE_SIZE_BYTES,
            self.config.max_upload_size_mb * 1024 * 1024
        )

        if asset.size_bytes > max_allowed_bytes:
            logger.warning(
                f"[{app_id}] File '{asset.original_name}' ({asset.size_bytes / 1024 / 1024:.1f} MB) "
                f"exceeds Telegram limit. Skipping direct upload."
            )
            return

        logger.info(f"[{app_id}] Uploading '{asset.original_name}' to Telegram...")

        url = f"{self.base_url}/sendDocument"
        data = {
            "chat_id": self.config.chat_id,
            "reply_to_message_id": reply_to_message_id,
            "disable_notification": self.config.silent,
        }

        try:
            with asset.path.open("rb") as f:
                files = {"document": (asset.original_name, f, "application/vnd.android.package-archive")}
                self._make_request(url, data=data, files=files)
            logger.debug(f"[{app_id}] Successfully uploaded {asset.original_name}.")
            
        except OSError as exc:
            logger.error(f"[{app_id}] Failed to read file {asset.path} for upload: {exc}")
        except TelegramError as exc:
            logger.error(f"[{app_id}] Failed to upload {asset.original_name}: {exc}")
            # We don't re-raise here. If one APK upload fails, we still want 
            # to attempt uploading the other architectures (if any exist).

    def _make_request(
        self, url: str, json: dict[str, Any] | None = None, data: dict[str, Any] | None = None, files: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Internal helper for executing POST requests and parsing Telegram's response.
        """
        try:
            # We use a longer timeout for file uploads (files is not None)
            timeout = 120 if files else 15
            response = self.session.post(url, json=json, data=data, files=files, timeout=timeout)
            response_data = response.json()

            if not response_data.get("ok"):
                error_code = response_data.get("error_code", "Unknown")
                description = response_data.get("description", "No description provided.")
                raise TelegramError(f"API Error {error_code}: {description}")

            return response_data

        except requests.exceptions.RequestException as exc:
            raise TelegramError(f"Network error communicating with Telegram: {exc}") from exc
        except ValueError as exc:
            raise TelegramError("Telegram returned an invalid JSON response.") from exc

