"""Interactive questionary setup wizard: token validation and .env creation."""

from __future__ import annotations

import asyncio

import questionary
from pydantic import SecretStr
from rich.panel import Panel

from tup.config import Settings, write_env_file
from tup.progress import console
from tup.uploader import TupError, bot_session
from tup.utils import mask_token


async def _validate_token(token: str, base_url: str | None) -> str:
    """Live-validate the token via get_me(); returns the bot username."""
    settings = Settings.model_construct(
        telegram_bot_token=SecretStr(token),
        telegram_api_base_url=base_url,
        max_retries=3,
        request_timeout=120,
    )
    async with bot_session(settings) as bot:
        me = await bot.get_me()
    return me.username or me.first_name


def run_wizard() -> None:
    """Interactive first-run configuration; writes .env with 0600 permissions."""
    console.print(Panel("Welcome to [bold]tup[/bold] setup", border_style="blue"))

    token = questionary.password("Telegram Bot API token (from @BotFather):").ask()
    if not token:
        raise TupError("Setup cancelled: a bot token is required.")

    base_url = questionary.text(
        "Local Bot API server URL (empty for api.telegram.org):", default=""
    ).ask()
    base_url = base_url.strip() or None

    try:
        username = asyncio.run(_validate_token(token.strip(), base_url))
    except TupError:
        raise
    except Exception as exc:
        raise TupError(
            f"Token validation failed: {exc}",
            hint="Double-check the token with @BotFather and your network connection.",
        ) from exc
    console.print(f"✅ Token {mask_token(token)} is valid — bot [bold]@{username}[/bold]")

    default_chat = questionary.text(
        "Default chat/drive ID (optional, can be an alias later):", default=""
    ).ask()
    chat_type = questionary.select(
        "Default chat type:", choices=["group", "user", "channel"], default="group"
    ).ask()
    api_id = questionary.text(
        "my.telegram.org api_id (required for uploads — get it at "
        "https://my.telegram.org → API development tools):"
    ).ask()
    if not api_id or not api_id.strip():
        raise TupError(
            "Setup cancelled: api_id is required.",
            hint="Uploads run over MTProto, which needs my.telegram.org credentials.",
        )
    api_hash = questionary.password("my.telegram.org api_hash:").ask() or ""
    if not api_hash.strip():
        raise TupError("Setup cancelled: api_hash is required.")

    values = {
        "telegram_bot_token": token.strip(),
        "default_chat_id": (default_chat or "").strip(),
        "default_chat_type": chat_type or "group",
        "telegram_api_base_url": base_url or "",
        "telegram_api_id": (api_id or "").strip(),
        "telegram_api_hash": api_hash.strip(),
    }
    target = write_env_file(values)
    console.print(f"✅ Configuration written to [bold]{target}[/bold] (permissions 0600)")
