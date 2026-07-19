"""Pydantic settings, .env handling, and the setup execution gate."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from pydantic import Field, HttpUrl, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

SECURE_MODE = 0o600

# Files tup keeps in its home directory (secrets, index, logs, MTProto auth).
_HOME_FILES = (".env", "registry.db", "tup.log", "tup-mtproto.session")


class SetupRequiredError(RuntimeError):
    """Raised when no valid configuration exists; the CLI should run `tup setup`."""


def config_dir() -> Path:
    """tup's home directory (~/.tup): .env, registry.db, logs, session, and the
    per-drive download cache all live here. Overridable via TUP_CONFIG_DIR."""
    override = os.environ.get("TUP_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path("~/.tup").expanduser()


def migrate_legacy_config(legacy: Path | None = None, target: Path | None = None) -> list[str]:
    """One-time move of files from older homes (~/.tui, ~/.config/tup) into ~/.tup.

    Moves tup's known files plus per-drive cache directories, never
    overwriting existing destinations; returns the names moved. A no-op once
    the migration has happened (or when the config dir is overridden without
    explicit paths, as in tests).
    """
    if target is None:
        target = config_dir()
    if legacy is not None:
        candidates = [legacy]
    else:
        if os.environ.get("TUP_CONFIG_DIR"):
            return []
        candidates = [Path("~/.tui").expanduser(), Path("~/.config/tup").expanduser()]
    moved: list[str] = []
    for candidate in candidates:
        if not candidate.is_dir() or candidate == target:
            continue
        for source in sorted(candidate.iterdir()):
            # Known files plus cache directories (named after chat ids).
            if source.is_file() and source.name not in _HOME_FILES:
                continue
            destination = target / source.name
            if destination.exists():
                continue
            target.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            moved.append(source.name)
    return moved


def env_file_path() -> Path:
    """The .env file to load: a local ./.env wins, else the per-user config file."""
    local = Path(".env").resolve()
    if local.is_file():
        return local
    return config_dir() / ".env"


def default_database_path() -> Path:
    return config_dir() / "registry.db"


def log_file_path() -> Path:
    return config_dir() / "tup.log"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: SecretStr = Field(..., description="Telegram Bot API Token")
    default_chat_id: str | None = Field(None, description="Default numeric ID or alias")
    default_chat_type: str = Field("group", pattern="^(group|user|channel)$")
    telegram_api_base_url: HttpUrl | None = Field(
        None, description="Local Bot API URL for 2GB limits"
    )
    telegram_api_id: int | None = Field(
        None, description="my.telegram.org api_id (enables MTProto uploads up to 2GB)"
    )
    telegram_api_hash: SecretStr | None = Field(
        None, description="my.telegram.org api_hash (enables MTProto uploads up to 2GB)"
    )
    max_retries: int = Field(3, ge=1, le=10)
    request_timeout: int = Field(120, ge=10, le=3600)
    database_path: Path = Field(default_factory=default_database_path)
    log_level: str = Field("INFO", pattern="^(DEBUG|INFO|WARNING|ERROR)$")

    @classmethod
    def load(cls) -> Settings:
        """Load settings from the resolved .env file, enforcing 0600 permissions.

        Raises SetupRequiredError when the file is missing or invalid so the
        CLI can direct the user to `tup setup`.
        """
        env_file = env_file_path()
        if not env_file.is_file():
            raise SetupRequiredError(
                f"No configuration found at {env_file}. Run [bold]tup setup[/bold] to configure."
            )
        ensure_secure_permissions(env_file)
        try:
            # _env_file is a documented pydantic-settings init kwarg missing from its stubs
            return cls(_env_file=env_file)  # type: ignore[call-arg]
        except ValidationError as exc:
            raise SetupRequiredError(
                f"Configuration at {env_file} is invalid: {exc.error_count()} error(s). "
                "Run [bold]tup setup[/bold] to reconfigure."
            ) from exc


def ensure_secure_permissions(path: Path) -> None:
    """Force 0600 on a secret-bearing file (repairs looser modes in place)."""
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != SECURE_MODE:
        os.chmod(path, SECURE_MODE)


def write_env_file(values: dict[str, str]) -> Path:
    """Write key=value pairs to the per-user .env with 0600 permissions."""
    target = config_dir() / ".env"
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key.upper()}={value}" for key, value in values.items() if value]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(target, SECURE_MODE)
    return target
