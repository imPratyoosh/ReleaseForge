"""
src/config.py
==============

Loads and validates ReleaseForge's single configuration file (config.toml)
into typed, immutable dataclasses.

Why this module exists
-----------------------
Every other module in ReleaseForge (github.py, downloader.py, fdroid.py,
telegram.py, publisher.py, main.py) needs configuration data, but none of
them should know how to parse TOML, substitute secrets, or validate field
values. That knowledge lives here, once, so:

  * Adding a new config option means touching this file (and config.toml),
    not every module that happens to use it.
  * Validation errors are caught in one place, at startup, with clear
    messages -- instead of surfacing as confusing crashes deep in
    github.py or fdroid.py later on.

Secret handling
----------------
Values like `bot_token = "${BOT_TOKEN}"` in config.toml are NOT resolved by
the TOML parser itself (TOML has no concept of variable substitution). This
module walks the parsed data and replaces any string matching `${NAME}`
with the value of the `NAME` environment variable. This keeps real secrets
out of git entirely -- in GitHub Actions, `NAME` is populated from a
repository/organization Secret.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Python 3.11+ has tomllib in the standard library. ReleaseForge targets
# Python 3.12, so we use it directly rather than adding a third-party
# TOML dependency.
import tomllib


# Matches "${SOME_VAR_NAME}" anywhere inside a string.
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# The only values `track` is allowed to take. Centralized here so
# validation and (later) github.py agree on the same source of truth.
VALID_TRACK_STRATEGIES = {"release", "tag", "branch_commit"}

# The only values `release_channel` is allowed to take.
VALID_RELEASE_CHANNELS = {"stable", "beta", "alpha", "nightly", "prerelease"}

# Publisher names that main.py / publisher.py currently know how to
# dispatch to. Kept here (not just in publisher.py) so config validation
# can reject typos like `publish = ["telegrm"]` at load time.
KNOWN_PUBLISHERS = {"fdroid", "telegram"}


class ConfigError(Exception):
    """
    Raised whenever config.toml is missing, malformed, or fails validation.

    Kept as a single, specific exception type (rather than letting raw
    KeyError/ValueError/tomllib.TOMLDecodeError leak out) so main.py can
    catch configuration problems separately from runtime problems and
    print a clean, actionable error instead of a stack trace.
    """


def _substitute_env_vars(value: Any) -> Any:
    """
    Recursively replace "${VAR_NAME}" placeholders with environment
    variable values, anywhere they appear in strings, lists, or dicts.

    Args:
        value: A raw value from the parsed TOML data (str, list, dict, or
            a plain scalar like bool/int/float).

    Returns:
        The same structure, with every "${VAR_NAME}" string replaced by
        the corresponding environment variable's value.

    Raises:
        ConfigError: If a referenced environment variable is not set.
            Failing loudly here is deliberate: a silently-empty bot token
            would otherwise cause a confusing failure later in telegram.py.
    """
    if isinstance(value, str):

        def _replace(match: re.Match[str]) -> str:
            var_name = match.group(1)
            env_value = os.environ.get(var_name)
            if env_value is None:
                raise ConfigError(
                    f"config.toml references '${{{var_name}}}' but the "
                    f"environment variable '{var_name}' is not set. "
                    f"In GitHub Actions, set it as a repository secret and "
                    f"pass it to the job via `env:`."
                )
            return env_value

        return _ENV_VAR_PATTERN.sub(_replace, value)

    if isinstance(value, list):
        return [_substitute_env_vars(item) for item in value]

    if isinstance(value, dict):
        return {key: _substitute_env_vars(item) for key, item in value.items()}

    # Bools, ints, floats, None -- nothing to substitute.
    return value


@dataclass(frozen=True)
class RepositoryConfig:
    """Cosmetic info about the F-Droid repo itself (not an upstream app)."""

    name: str
    description: str = ""
    url: str = ""


@dataclass(frozen=True)
class TelegramConfig:
    """Global Telegram publishing settings, shared by all apps that opt in."""

    enabled: bool
    bot_token: str
    chat_id: str
    silent: bool = False
    disable_web_page_preview: bool = True
    max_upload_size_mb: int = 50
    message_template: str | None = None


@dataclass(frozen=True)
class FdroidConfig:
    """Global F-Droid repository settings."""

    enabled: bool
    repo_path: Path
    keep_versions: int = 3


@dataclass(frozen=True)
class LoggingConfig:
    """Console logging verbosity."""

    level: str = "INFO"


@dataclass(frozen=True)
class AppConfig:
    """
    Configuration for a single tracked application (one [[app]] block).

    This is the object that github.py, downloader.py, and publisher.py
    will pass around to identify "which app am I working on right now".
    """

    name: str
    github: str
    track: str
    release_channel: str
    architectures: list[str]
    asset_regex: str
    publish: list[str]
    exclude_prerelease: bool = True
    enabled: bool = True


@dataclass(frozen=True)
class Config:
    """
    The fully-loaded, validated ReleaseForge configuration.

    This is the single object main.py passes down into every other module
    -- nothing downstream re-reads config.toml or environment variables
    directly.
    """

    repository: RepositoryConfig
    telegram: TelegramConfig
    fdroid: FdroidConfig
    logging: LoggingConfig
    apps: list[AppConfig] = field(default_factory=list)

    def enabled_apps(self) -> list[AppConfig]:
        """Return only the apps that are switched on in config.toml."""
        return [app for app in self.apps if app.enabled]


def _require(data: dict[str, Any], key: str, context: str) -> Any:
    """
    Fetch a required key from a dict, raising a clear ConfigError if absent.

    Args:
        data: The dict to look up `key` in (e.g. a parsed [[app]] table).
        key: The required field name.
        context: A human-readable description of where this data came
            from (e.g. "[[app]] block #2"), used in the error message.

    Returns:
        The value at `data[key]`.

    Raises:
        ConfigError: If `key` is missing from `data`.
    """
    if key not in data:
        raise ConfigError(f"Missing required field '{key}' in {context}.")
    return data[key]


def _build_app_config(raw_app: dict[str, Any], index: int) -> AppConfig:
    """
    Validate and construct a single AppConfig from one [[app]] TOML table.

    Args:
        raw_app: The raw dict for this [[app]] block (env vars already
            substituted).
        index: Position of this block in the config, used only for
            friendlier error messages (e.g. "[[app]] block #3").

    Returns:
        A validated AppConfig.

    Raises:
        ConfigError: If any field is missing or has an invalid value.
    """
    context = f"[[app]] block #{index + 1}"

    name = _require(raw_app, "name", context)
    github = _require(raw_app, "github", context)
    track = _require(raw_app, "track", context)
    release_channel = _require(raw_app, "release_channel", context)
    architectures = _require(raw_app, "architectures", context)
    asset_regex = _require(raw_app, "asset_regex", context)
    publish = _require(raw_app, "publish", context)

    if track not in VALID_TRACK_STRATEGIES:
        raise ConfigError(
            f"{context} ('{name}'): invalid track '{track}'. "
            f"Must be one of: {sorted(VALID_TRACK_STRATEGIES)}."
        )

    if release_channel not in VALID_RELEASE_CHANNELS:
        raise ConfigError(
            f"{context} ('{name}'): invalid release_channel "
            f"'{release_channel}'. Must be one of: "
            f"{sorted(VALID_RELEASE_CHANNELS)}."
        )

    if "/" not in github or len(github.split("/")) != 2:
        raise ConfigError(
            f"{context} ('{name}'): 'github' must be in 'owner/repo' "
            f"form, got '{github}'."
        )

    unknown_publishers = set(publish) - KNOWN_PUBLISHERS
    if unknown_publishers:
        raise ConfigError(
            f"{context} ('{name}'): unknown publisher(s) "
            f"{sorted(unknown_publishers)}. Known publishers: "
            f"{sorted(KNOWN_PUBLISHERS)}."
        )

    try:
        re.compile(asset_regex)
    except re.error as exc:
        raise ConfigError(
            f"{context} ('{name}'): 'asset_regex' is not a valid regex: {exc}"
        ) from exc

    return AppConfig(
        name=name,
        github=github,
        track=track,
        release_channel=release_channel,
        architectures=list(architectures),
        asset_regex=asset_regex,
        publish=list(publish),
        exclude_prerelease=raw_app.get("exclude_prerelease", True),
        enabled=raw_app.get("enabled", True),
    )


def load_config(path: str | Path = "config.toml") -> Config:
    """
    Load, substitute secrets into, and validate config.toml.

    Args:
        path: Path to the TOML config file. Defaults to "config.toml" in
            the current working directory, which is where GitHub Actions
            will run main.py from (the repo root).

    Returns:
        A fully validated Config object.

    Raises:
        ConfigError: If the file is missing, is not valid TOML, or fails
            any validation rule (missing field, bad enum value, unknown
            publisher, invalid regex, missing env var, etc.).
    """
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"Config file not found: {config_path}")

    try:
        raw_text = config_path.read_bytes()
        raw_data: dict[str, Any] = tomllib.loads(raw_text.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Failed to parse {config_path}: {exc}") from exc

    # Resolve ${VAR} secrets everywhere before we look at any individual
    # field, so downstream code never has to think about substitution.
    data = _substitute_env_vars(raw_data)

    repo_raw = _require(data, "repository", "config.toml")
    repository = RepositoryConfig(
        name=_require(repo_raw, "name", "[repository]"),
        description=repo_raw.get("description", ""),
        url=repo_raw.get("url", ""),
    )

    telegram_raw = _require(data, "telegram", "config.toml")
    telegram = TelegramConfig(
        enabled=telegram_raw.get("enabled", False),
        bot_token=telegram_raw.get("bot_token", ""),
        chat_id=telegram_raw.get("chat_id", ""),
        silent=telegram_raw.get("silent", False),
        disable_web_page_preview=telegram_raw.get(
            "disable_web_page_preview", True
        ),
        max_upload_size_mb=telegram_raw.get("max_upload_size_mb", 50),
        message_template=telegram_raw.get("message_template"),
    )
    if telegram.enabled and not telegram.bot_token:
        raise ConfigError(
            "[telegram] is enabled but 'bot_token' resolved to an empty "
            "value. Check that the BOT_TOKEN secret is set."
        )

    fdroid_raw = _require(data, "fdroid", "config.toml")
    fdroid = FdroidConfig(
        enabled=fdroid_raw.get("enabled", False),
        repo_path=Path(fdroid_raw.get("repo_path", "./repo")),
        keep_versions=fdroid_raw.get("keep_versions", 3),
    )

    logging_raw = data.get("logging", {})
    logging_cfg = LoggingConfig(level=logging_raw.get("level", "INFO"))

    raw_apps = data.get("app", [])
    if not raw_apps:
        raise ConfigError(
            "No [[app]] blocks found in config.toml. Add at least one "
            "app to track."
        )

    apps = [
        _build_app_config(raw_app, index)
        for index, raw_app in enumerate(raw_apps)
    ]

    # Catch duplicate app names early -- state.py uses the app name as its
    # state.json key, so duplicates would silently overwrite each other's
    # version tracking.
    seen_names = set()
    for app in apps:
        if app.name in seen_names:
            raise ConfigError(
                f"Duplicate app name '{app.name}' in config.toml. "
                f"App names must be unique."
            )
        seen_names.add(app.name)

    return Config(
        repository=repository,
        telegram=telegram,
        fdroid=fdroid,
        logging=logging_cfg,
        apps=apps,
    )


if __name__ == "__main__":
    # Small manual smoke test: `python -m src.config` from the repo root
    # loads config.toml and prints a summary, without needing the rest of
    # the app. Handy while editing config.toml itself.
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Repository: {cfg.repository.name}")
    print(f"Telegram enabled: {cfg.telegram.enabled}")
    print(f"F-Droid enabled: {cfg.fdroid.enabled}")
    print(f"Apps configured: {len(cfg.apps)} "
          f"({len(cfg.enabled_apps())} enabled)")
    for app in cfg.apps:
        status = "enabled" if app.enabled else "disabled"
        print(f"  - {app.name} ({app.github}, {status})")
