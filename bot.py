"""
Telegram-бот: анализ слабого игрока в матче Faceit CS2 по ссылке на матч.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

import faceit
import requests
from faceit.exceptions import APIError, NotFoundError
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# --- Настройки (подставьте свои значения) ---
BOT_TOKEN = "8991957878:AAFbRWDYwMCZhL3PSkVbVSGJ-VQF6rNWn60"
FACEIT_API_KEY = "a09ba37e-31db-4566-9611-abce349660aa"

FACEIT_API_BASE = "https://open.faceit.com/data/v4"
REQUEST_TIMEOUT = 20

# ID матча Faceit: префикс 1- и UUID
MATCH_ID_PATTERN = re.compile(
    r"(1-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)

# Человекочитаемые названия карт
MAP_DISPLAY_NAMES: dict[str, str] = {
    "de_mirage": "Mirage",
    "de_dust2": "Dust2",
    "de_inferno": "Inferno",
    "de_nuke": "Nuke",
    "de_overpass": "Overpass",
    "de_ancient": "Ancient",
    "de_anubis": "Anubis",
    "de_vertigo": "Vertigo",
    "de_cache": "Cache",
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


@dataclass
class AnalyzedPlayer:
    """Игрок соперника с показателями для сравнения."""

    nickname: str
    player_id: str
    elo: int
    win_rate: float


class FaceitService:
    """Работа с Faceit: HTTP через requests, профили — через библиотеку faceit."""

    def __init__(self, api_key: str) -> None:
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._data = faceit.SyncDataResource(api_key)

    def __enter__(self) -> FaceitService:
        self._data.__enter__()
        return self

    def __exit__(self, *args: object) -> None:
        self._data.__exit__(*args)

    def _request(self, method: str, path: str) -> dict[str, Any]:
        """Вызов Data API Faceit через requests."""
        url = f"{FACEIT_API_BASE}{path}"
        try:
            response = requests.request(
                method,
                url,
                headers=self._headers,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ConnectionError(
                "Faceit API недоступен. Проверьте интернет или повторите позже."
            ) from exc

        if response.status_code == 404:
            raise NotFoundError()
        if response.status_code >= 400:
            detail = response.text[:200] or f"HTTP {response.status_code}"
            raise APIError(message=detail)

        return response.json()

    def get_match(self, match_id: str) -> dict[str, Any]:
        return self._request("GET", f"/matches/{match_id}")

    def get_match_stats(self, match_id: str) -> dict[str, Any]:
        return self._request("GET", f"/matches/{match_id}/stats")

    def get_player_elo_and_winrate(self, player_id: str) -> tuple[int, float]:
        """ELO и общий винрейт CS2 из профиля через библиотеку faceit."""
        player = self._data.players.get(player_id)
        cs2 = player.games.get(faceit.GameID.CS2)
        if cs2 is None:
            raise ValueError(f"У игрока {player.nickname} нет привязки CS2")

        stats = self._data.players.stats(player.id, faceit.GameID.CS2)
        return int(cs2.elo), float(stats.lifetime.win_rate)


def extract_match_id(text: str) -> str | None:
    """Извлекает match_id из ссылки Faceit или из текста с ID."""
    match = MATCH_ID_PATTERN.search(text.strip())
    return match.group(1) if match else None


def parse_user_message(text: str) -> tuple[str | None, str | None]:
    """
    Разбирает сообщение: ссылка/ID матча и опционально никнейм пользователя.
    Формат: «ссылка» или «ссылка мой_ник» — ник нужен, чтобы выбрать команду соперника.
    """
    parts = text.strip().split()
    if not parts:
        return None, None

    match_id = extract_match_id(parts[0])
    nickname = parts[1] if len(parts) > 1 else None
    return match_id, nickname


def format_map_name(raw_map: str | None) -> str:
    if not raw_map:
        return "Неизвестно"
    key = raw_map.strip().lower()
    if key in MAP_DISPLAY_NAMES:
        return MAP_DISPLAY_NAMES[key]
    if key.startswith("de_"):
        return key[3:].replace("_", " ").title()
    return raw_map.replace("_", " ").title()


def get_map_from_stats(stats: dict[str, Any]) -> str | None:
    rounds = stats.get("rounds") or []
    if not rounds:
        return None
    round_stats = rounds[0].get("round_stats") or {}
    return round_stats.get("Map") or round_stats.get("map")


def get_opponent_roster(
    match: dict[str, Any],
    user_nickname: str | None,
) -> list[dict[str, Any]]:
    """Возвращает ростер команды соперника."""
    teams: dict[str, Any] = match.get("teams") or {}
    if len(teams) < 2:
        raise ValueError("В матче не найдены две команды")

    team_list = list(teams.values())

    if user_nickname:
        user_lower = user_nickname.lower()
        my_index: int | None = None
        for index, team in enumerate(team_list):
            roster = team.get("roster") or []
            if any(
                (player.get("nickname") or "").lower() == user_lower
                for player in roster
            ):
                my_index = index
                break
        if my_index is None:
            raise ValueError(
                f"Игрок «{user_nickname}» не найден в этом матче. "
                "Проверьте ник или отправьте только ссылку."
            )
        opponent_index = 1 - my_index if len(team_list) == 2 else (my_index + 1) % len(
            team_list
        )
        return team_list[opponent_index].get("roster") or []

    # Без ника: вторая команда в ответе API считается соперником
    return team_list[1].get("roster") or []


def analyze_opponents(
    service: FaceitService,
    match_id: str,
    user_nickname: str | None,
) -> tuple[str, AnalyzedPlayer, AnalyzedPlayer]:
    match = service.get_match(match_id)
    stats_payload = service.get_match_stats(match_id)
    map_name = format_map_name(get_map_from_stats(stats_payload))

    roster = get_opponent_roster(match, user_nickname)
    if not roster:
        raise ValueError("Состав команды соперника пуст")

    analyzed: list[AnalyzedPlayer] = []
    for member in roster:
        player_id = member.get("player_id")
        nickname = member.get("nickname") or "unknown"
        if not player_id:
            continue
        try:
            elo, win_rate = service.get_player_elo_and_winrate(player_id)
        except (NotFoundError, APIError, ValueError) as exc:
            logger.warning("Пропуск игрока %s: %s", nickname, exc)
            continue
        analyzed.append(
            AnalyzedPlayer(
                nickname=nickname,
                player_id=player_id,
                elo=elo,
                win_rate=win_rate,
            )
        )

    if not analyzed:
        raise ValueError("Не удалось получить статистику ни одного игрока соперника")

    weakest = min(analyzed, key=lambda p: (p.win_rate, p.elo))
    strongest = max(analyzed, key=lambda p: (p.win_rate, p.elo))
    return map_name, weakest, strongest


def build_report(
    map_name: str,
    weakest: AnalyzedPlayer,
    strongest: AnalyzedPlayer,
) -> str:
    return (
        f"🔍 Карта: {map_name}\n"
        f"🛑 Самый слабый: {weakest.nickname} "
        f"(ELO: {weakest.elo}, винрейт: {weakest.win_rate:.0f}%)\n"
        f"🟢 Самый сильный: {strongest.nickname} "
        f"(ELO: {strongest.elo}, винрейт: {strongest.win_rate:.0f}%)\n"
        f"🎯 Совет: Атакуй точку, которую держит {weakest.nickname}."
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "Привет! Отправь ссылку на матч Faceit CS2.\n\n"
        "Формат:\n"
        "• только ссылка — анализ второй команды в матче;\n"
        "• ссылка и твой ник — анализ команды соперников.\n\n"
        "Пример:\n"
        "https://www.faceit.com/ru/cs2/room/1/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx\n"
        "или та же ссылка и через пробел твой никнейм Faceit."
    )


async def handle_match_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    if not FACEIT_API_KEY:
        await update.message.reply_text(
            "❌ Не задан FACEIT_API_KEY в bot.py. Получите ключ на developers.faceit.com"
        )
        return

    match_id, user_nickname = parse_user_message(update.message.text)
    if not match_id:
        await update.message.reply_text(
            "❌ Неверная ссылка. Пришлите URL матча Faceit CS2 "
            "(например, faceit.com/.../cs2/room/1/...)."
        )
        return

    status_message = await update.message.reply_text("⏳ Анализирую матч...")

    def run_analysis() -> str:
        with FaceitService(FACEIT_API_KEY) as service:
            map_name, weakest, strongest = analyze_opponents(
                service,
                match_id,
                user_nickname,
            )
            return build_report(map_name, weakest, strongest)

    try:
        report = await asyncio.to_thread(run_analysis)
        await status_message.edit_text(report)
    except NotFoundError:
        await status_message.edit_text(
            "❌ Матч не найден. Проверьте ссылку и что матч уже создан на Faceit."
        )
    except ConnectionError as exc:
        await status_message.edit_text(f"❌ {exc}")
    except APIError as exc:
        await status_message.edit_text(
            f"❌ Ошибка Faceit API [{exc.status_code}]: {exc.message}"
        )
    except ValueError as exc:
        await status_message.edit_text(f"❌ {exc}")
    except Exception:
        logger.exception("Неожиданная ошибка при анализе матча")
        await status_message.edit_text(
            "❌ Произошла непредвиденная ошибка. Попробуйте позже."
        )


def build_application() -> Application:
    """Создаёт Application и регистрирует обработчики."""
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_match_link)
    )
    return application


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Укажите BOT_TOKEN в начале файла bot.py")

    application = build_application()
    logger.info("Бот запущен (long polling)")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

