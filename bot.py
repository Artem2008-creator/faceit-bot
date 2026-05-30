"""
Telegram-бот: анализ слабого игрока в матче Faceit CS2 по ссылке на матч.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# telegram_user_id → faceit nickname (только в памяти, сбрасывается при перезапуске)
user_nicks: dict[int, str] = {}
user_last_active: dict[int, float] = {}
NICK_INACTIVE_DAYS = 7

# --- Настройки (подставьте свои значения) ---
BOT_TOKEN = "8991957878:AAFbRWDYwMCZhL3PSkVbVSGJ-VQF6rNWn60"
FACEIT_API_KEY = "3fc0c069-7c26-46bb-a478-bed79cb95894"

FACEIT_API_BASE = "https://open.faceit.com/data/v4"
REQUEST_TIMEOUT = 20
HEALTH_PORT = 10000
RECENT_MAP_MATCHES_LIMIT = 15
GAME_STATS_FETCH_LIMIT = 40
MIN_MAP_MATCHES_FOR_SKILL = 3
MAP_POOL_MATCHES_LIMIT = 20
MIN_MAP_MATCHES_FOR_REC = 3
MAP_POOL_TOP_BANS = 2
MAP_POOL_TOP_PICKS = 2
SIDE_STYLE_MATCHES_LIMIT = 8
MIN_SIDE_STYLE_MATCHES = 3
CONFIDENCE_MIN_MATCHES = 10
CONFIDENCE_MIN_PERCENT = 30
CONFIDENCE_MAX_PERCENT = 95
CONFIDENCE_BASE_PERCENT = 85
CONFIDENCE_PENALTY_PER_PLAYER = 13
PROGRESS_OPTIONS = (15, 50, 100)
PROGRESS_MIN_MATCHES = 2
PROGRESS_ELO_PER_WIN = 25
PROGRESS_CALLBACK_PREFIX = "progress:"

# Средние показатели игроков Faceit по уровню (skill level 1–10)
SKILL_LEVEL_BENCHMARKS: dict[int, dict[str, float]] = {
    1: {"kd": 0.72, "avg_kills": 13.5, "hs_pct": 40.0, "win_rate": 47.0},
    2: {"kd": 0.78, "avg_kills": 14.0, "hs_pct": 42.0, "win_rate": 48.0},
    3: {"kd": 0.85, "avg_kills": 14.5, "hs_pct": 44.0, "win_rate": 49.0},
    4: {"kd": 0.92, "avg_kills": 15.0, "hs_pct": 46.0, "win_rate": 50.0},
    5: {"kd": 0.98, "avg_kills": 15.5, "hs_pct": 47.0, "win_rate": 50.0},
    6: {"kd": 1.02, "avg_kills": 16.0, "hs_pct": 48.0, "win_rate": 51.0},
    7: {"kd": 1.08, "avg_kills": 16.5, "hs_pct": 49.0, "win_rate": 51.5},
    8: {"kd": 1.14, "avg_kills": 17.0, "hs_pct": 50.0, "win_rate": 52.0},
    9: {"kd": 1.20, "avg_kills": 17.5, "hs_pct": 51.0, "win_rate": 52.5},
    10: {"kd": 1.28, "avg_kills": 18.5, "hs_pct": 52.0, "win_rate": 53.0},
}

FACEIT_PLAYSTYLE_ROLE_RU: dict[str, str] = {
    "entry_fragger": "Первое касание",
    "entry": "Первое касание",
    "entryfragger": "Первое касание",
    "support": "Поддержка",
    "awper": "Снайпер",
    "awp": "Снайпер",
    "clutcher": "Клатчер",
    "lurker": "Скрытная игра",
    "igl": "Капитан",
    "roamer": "Свободный игрок",
    "anchor": "Опорник",
    "rifler": "Стрелок",
}

MATCH_ID_PATTERN = re.compile(
    r"(1-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)

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
    "de_train": "Train",
}

# Карты без позиций — только общая статистика
MAPS_WITHOUT_POSITIONS = frozenset({"de_overpass", "de_dust2"})

# Допустимые позиции на карте (CS2 callouts)
MAP_VALID_POSITIONS: dict[str, tuple[str, ...]] = {
    "de_mirage": ("A anchor", "B anchor", "Short", "Connector", "Window"),
    "de_inferno": ("A anchor", "B anchor", "Pit/Balcony", "Apps"),
    "de_ancient": ("A anchor", "B anchor", "Cave", "Mid"),
    "de_nuke": ("A anchor", "Outside", "Ramp", "Rotation", "Main"),
    "de_anubis": ("A anchor", "B anchor", "Mid", "Connector"),
    "de_cache": ("A anchor", "Short", "Mid", "B anchor"),
}

# Синонимы из Faceit API (roles / heatmap keys) → каноническое имя
POSITION_ALIASES: dict[str, dict[str, str]] = {
    "de_mirage": {
        "a": "A anchor",
        "a anchor": "A anchor",
        "a site": "A anchor",
        "b": "B anchor",
        "b anchor": "B anchor",
        "b site": "B anchor",
        "short": "Short",
        "connector": "Connector",
        "con": "Connector",
        "window": "Window",
        "win": "Window",
    },
    "de_inferno": {
        "a": "A anchor",
        "a anchor": "A anchor",
        "b": "B anchor",
        "b anchor": "B anchor",
        "pit": "Pit/Balcony",
        "balcony": "Pit/Balcony",
        "pit balcony": "Pit/Balcony",
        "pit/balcony": "Pit/Balcony",
        "apps": "Apps",
        "apartments": "Apps",
        "app": "Apps",
    },
    "de_ancient": {
        "a": "A anchor",
        "a anchor": "A anchor",
        "b": "B anchor",
        "b anchor": "B anchor",
        "cave": "Cave",
        "mid": "Mid",
        "middle": "Mid",
    },
    "de_nuke": {
        "a": "A anchor",
        "a anchor": "A anchor",
        "a site": "A anchor",
        "outside": "Outside",
        "yard": "Outside",
        "ramp": "Ramp",
        "rotation": "Rotation",
        "rot": "Rotation",
        "haven": "Rotation",
        "main": "Main",
        "main hall": "Main",
    },
    "de_anubis": {
        "a": "A anchor",
        "a anchor": "A anchor",
        "b": "B anchor",
        "b anchor": "B anchor",
        "mid": "Mid",
        "middle": "Mid",
        "connector": "Connector",
        "con": "Connector",
    },
    "de_cache": {
        "a": "A anchor",
        "a anchor": "A anchor",
        "short": "Short",
        "mid": "Mid",
        "middle": "Mid",
        "b": "B anchor",
        "b anchor": "B anchor",
    },
}

# Тактическая роль Faceit → возможные позиции (выбор только при наличии heatmap)
TACTICAL_ROLE_CANDIDATES: dict[str, dict[str, tuple[str, ...]]] = {
    "de_mirage": {
        "anchor": ("A anchor", "B anchor"),
        "support": ("B anchor", "Connector", "Window"),
        "entry": ("Short", "A anchor", "Connector"),
        "lurker": ("Connector", "Short", "Window"),
        "awper": ("Window", "A anchor", "Connector"),
        "awp": ("Window", "A anchor", "Connector"),
        "rifler": ("Connector", "Short", "A anchor"),
        "igl": ("Connector", "Window"),
    },
    "de_inferno": {
        "anchor": ("A anchor", "B anchor"),
        "support": ("B anchor", "Apps", "Pit/Balcony"),
        "entry": ("Apps", "A anchor"),
        "lurker": ("Apps", "Pit/Balcony"),
        "awper": ("A anchor", "Pit/Balcony"),
        "awp": ("A anchor", "Pit/Balcony"),
        "rifler": ("Apps", "A anchor", "B anchor"),
        "igl": ("Apps", "A anchor"),
    },
    "de_ancient": {
        "anchor": ("A anchor", "B anchor"),
        "support": ("B anchor", "Cave", "Mid"),
        "entry": ("A anchor", "Cave"),
        "lurker": ("Cave", "Mid"),
        "awper": ("Mid", "A anchor"),
        "awp": ("Mid", "A anchor"),
        "rifler": ("Mid", "Cave"),
        "igl": ("Mid",),
    },
    "de_nuke": {
        "anchor": ("A anchor", "Ramp", "Main"),
        "support": ("Outside", "Ramp", "Rotation"),
        "entry": ("Outside", "Ramp"),
        "lurker": ("Outside", "Rotation"),
        "awper": ("Outside", "A anchor"),
        "awp": ("Outside", "A anchor"),
        "rifler": ("Outside", "Ramp", "Main"),
        "igl": ("Outside", "Rotation"),
    },
    "de_anubis": {
        "anchor": ("A anchor", "B anchor"),
        "support": ("B anchor", "Connector", "Mid"),
        "entry": ("A anchor", "Mid"),
        "lurker": ("Connector", "Mid"),
        "awper": ("Mid", "A anchor"),
        "awp": ("Mid", "A anchor"),
        "rifler": ("Mid", "Connector"),
        "igl": ("Mid", "Connector"),
    },
    "de_cache": {
        "anchor": ("A anchor", "B anchor"),
        "support": ("B anchor", "Mid"),
        "entry": ("Short", "A anchor"),
        "lurker": ("Short", "Mid"),
        "awper": ("Mid", "A anchor"),
        "awp": ("Mid", "A anchor"),
        "rifler": ("Mid", "Short"),
        "igl": ("Mid",),
    },
}

# Позиция → зона для атаки / паттернов (A, B, MID)
POSITION_ATTACK_SITE: dict[str, dict[str, str]] = {
    "de_mirage": {
        "A anchor": "A",
        "Short": "A",
        "Connector": "A",
        "Window": "MID",
        "B anchor": "B",
    },
    "de_inferno": {
        "A anchor": "A",
        "Pit/Balcony": "A",
        "Apps": "B",
        "B anchor": "B",
    },
    "de_ancient": {
        "A anchor": "A",
        "Cave": "B",
        "B anchor": "B",
        "Mid": "MID",
    },
    "de_nuke": {
        "A anchor": "A",
        "Outside": "A",
        "Ramp": "B",
        "Rotation": "B",
        "Main": "B",
    },
    "de_anubis": {
        "A anchor": "A",
        "B anchor": "B",
        "Mid": "MID",
        "Connector": "MID",
    },
    "de_cache": {
        "A anchor": "A",
        "Short": "A",
        "Mid": "MID",
        "B anchor": "B",
    },
}

HEATMAP_STAT_HINTS = ("%", "kill", "time", "round", "damage", "death", "activity")
HEATMAP_CONTAINER_KEYS = (
    "heatmap",
    "Heatmap",
    "position_heatmap",
    "Position Heatmap",
    "zones",
    "Zones",
    "position",
    "Position",
)
MEMBER_ROLE_KEYS = ("roles", "role", "game_roles", "positions")
MEMBER_HEATMAP_KEYS = HEATMAP_CONTAINER_KEYS
MIN_HEATMAP_CONFIDENCE_RATIO = 1.25

STAT_KD_KEYS = ("Average K/D Ratio", "K/D Ratio", "Average K/D", "kd_ratio")
STAT_KILLS_KEYS = ("Kills",)
STAT_AVG_KILLS_KEYS = ("Average Kills", "Avg Kills")
STAT_ADR_KEYS = ("ADR", "Average ADR", "Average Damage / Round", "Average Damage per Round")
STAT_HS_KEYS = ("Average Headshots %", "Headshots %", "Headshot %", "HS %")
STAT_WIN_RATE_KEYS = ("Win Rate %",)
STAT_1V2_WIN_KEYS = ("1v2 Win Rate",)
STAT_1V1_WIN_KEYS = ("1v1 Win Rate",)
STAT_ENTRY_RATE_KEYS = ("Entry Rate", "Match Entry Rate")
STAT_ENTRY_SUCCESS_KEYS = ("Entry Success Rate", "Match Entry Success Rate")
STAT_TOTAL_ROUNDS_EXT_KEYS = ("Total Rounds with extended stats", "Rounds")
STAT_TOTAL_ENTRY_COUNT_KEYS = ("Total Entry Count", "Entry Count")
STAT_TOTAL_ENTRY_WINS_KEYS = ("Total Entry Wins", "Entry Wins")
STAT_FIRST_DEATH_KEYS = (
    "First Death Rate",
    "Opening Death Rate",
    "First Deaths %",
    "First Death Percent",
)
STAT_FLASH_VULN_KEYS = (
    "Flash Death Rate",
    "Deaths After Flash %",
    "Flash Vulnerability",
    "Deaths While Flashed %",
)
STAT_MATCH_ROUNDS_KEYS = ("Rounds",)
STAT_MATCH_DEATHS_KEYS = ("Deaths",)
STAT_MATCH_FIRST_KILLS_KEYS = ("First Kills",)
STAT_HS_MATCH_KEYS = ("Headshots %", "Average Headshots %", "Headshot %", "HS %")
STAT_1V1_WINS_KEYS = ("1v1Wins", "1v1 Wins")
STAT_1V1_COUNT_KEYS = ("1v1Count", "1v1 Count")
STAT_1V2_WINS_KEYS = ("1v2Wins", "1v2 Wins")
STAT_1V2_COUNT_KEYS = ("1v2Count", "1v2 Count")
STAT_MATCH_ASSISTS_KEYS = ("Assists",)
STAT_SNIPER_KILLS_KEYS = ("Sniper Kills",)
STAT_FLASH_ASSISTS_KEYS = ("Flash Assists", "Flash assists", "Flash Assists Count")
STAT_MAP_MATCHES_KEYS = ("Total Matches", "Matches", "Total matches")
STAT_PLANTS_KEYS = ("Bomb Plants", "Plants")
STAT_DEFUSES_KEYS = ("Bomb Defuses", "Defuses")
STAT_PISTOL_KILLS_KEYS = ("Pistol Kills",)
STAT_UTILITY_PER_ROUND_KEYS = ("Utility Usage per Round",)
STAT_FLASHES_PER_ROUND_KEYS = ("Flashes per Round in a Match", "Flashes per Round")

# Человекочитаемые названия зон для паттернов CT/T
MAP_ZONE_LABELS: dict[str, dict[str, str]] = {
    "de_mirage": {"A": "A", "B": "banana", "MID": "mid"},
    "de_dust2": {"A": "Long/A", "B": "B", "MID": "mid"},
    "de_inferno": {"A": "A", "B": "banana", "MID": "mid"},
    "de_nuke": {"A": "yard/outside", "B": "ramp/main", "MID": "mid"},
    "de_overpass": {"A": "A", "B": "B", "MID": "mid"},
    "de_ancient": {"A": "A", "B": "B", "MID": "mid"},
    "de_anubis": {"A": "A", "B": "B", "MID": "mid"},
    "de_vertigo": {"A": "A", "B": "B", "MID": "mid"},
    "de_cache": {"A": "A", "B": "B", "MID": "mid"},
    "de_train": {"A": "A", "B": "B", "MID": "mid"},
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def touch_user(user_id: int) -> None:
    user_last_active[user_id] = time.time()


def purge_inactive_users() -> None:
    """Удаляет ники пользователей, неактивных дольше NICK_INACTIVE_DAYS."""
    cutoff = time.time() - NICK_INACTIVE_DAYS * 24 * 3600
    expired = [uid for uid, ts in user_last_active.items() if ts < cutoff]
    for uid in expired:
        user_nicks.pop(uid, None)
        user_last_active.pop(uid, None)
    if expired:
        logger.info("Удалены неактивные ники: %s", expired)


class FaceitAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class FaceitNotFoundError(FaceitAPIError):
    pass


@dataclass
class MapSkill:
    kd_ratio: float
    avg_kills: float


@dataclass
class AnchorDefenseStats:
    hold_rating: float | None
    success_defend: float | None
    first_death_percent: float | None
    flash_vulnerability: float | None


@dataclass
class AttackAdvice:
    site: str
    anchor: "AnalyzedPlayer"
    stats: AnchorDefenseStats
    verdict: str


@dataclass
class AnalyzedPlayer:
    nickname: str
    player_id: str
    skill: MapSkill
    site: str
    site_source: str
    defense: AnchorDefenseStats | None = None


@dataclass
class MapRecommendation:
    map_key: str
    display_name: str
    action: str
    opponent_win_rate: float
    user_win_rate: float | None
    verdict: str


@dataclass
class MapPoolAnalysis:
    bans: list[MapRecommendation]
    picks: list[MapRecommendation]
    deciders: list[MapRecommendation]


@dataclass
class SidePlaystyle:
    side: str
    lines: list[str]


@dataclass
class TeamPlaystyleAnalysis:
    map_display_name: str
    ct: SidePlaystyle | None = None
    t: SidePlaystyle | None = None


@dataclass
class AggregatedPlaystyleMetrics:
    match_count: int
    total_rounds: float
    entry_rate: float
    first_kills_per_round: float
    plants_per_round: float
    defuses_per_round: float
    utility_per_round: float
    pistol_kills_per_round: float
    avg_round_seconds: float
    fast_round_pct: float
    execute_tendency: float
    default_tendency: float
    split_tendency: float
    zone_pressure: dict[str, float]
    stack_site: str | None
    stack_players: int
    ct_push_site: str | None
    ct_push_pct: float


@dataclass
class ConfidenceScore:
    percent: int
    total_matches: int
    low_match_players: int


@dataclass
class AnalysisResult:
    map_name: str
    map_key: str
    players: list[AnalyzedPlayer]
    team_avg: MapSkill
    weakest: AnalyzedPlayer
    strongest: AnalyzedPlayer
    map_pool: MapPoolAnalysis | None
    confidence: ConfidenceScore
    attack_advice: AttackAdvice | None = None
    playstyle: TeamPlaystyleAnalysis | None = None


class FaceitService:
    def __init__(self, api_key: str) -> None:
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._match_stats_cache: dict[str, dict[str, Any]] = {}
        self._player_cs2_stats_cache: dict[str, dict[str, Any]] = {}

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None) -> Any:
        url = f"{FACEIT_API_BASE}{path}"
        try:
            response = requests.request(
                method,
                url,
                headers=self._headers,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ConnectionError(
                "Faceit API недоступен. Проверьте интернет или повторите позже."
            ) from exc

        if response.status_code == 404:
            raise FaceitNotFoundError("Ресурс не найден", status_code=404)
        if response.status_code == 401:
            raise FaceitAPIError(
                "Неверный API-ключ Faceit. Проверьте FACEIT_API_KEY.",
                status_code=401,
            )
        if response.status_code >= 400:
            detail = response.text[:200] or f"HTTP {response.status_code}"
            raise FaceitAPIError(detail, status_code=response.status_code)

        return response.json()

    def get_match(self, match_id: str) -> dict[str, Any]:
        return self._request("GET", f"/matches/{match_id}")

    def get_match_stats(self, match_id: str) -> dict[str, Any]:
        if match_id not in self._match_stats_cache:
            self._match_stats_cache[match_id] = self._request(
                "GET", f"/matches/{match_id}/stats"
            )
        return self._match_stats_cache[match_id]

    def get_player_cs2_stats(self, player_id: str) -> dict[str, Any]:
        if player_id not in self._player_cs2_stats_cache:
            self._player_cs2_stats_cache[player_id] = self._request(
                "GET", f"/players/{player_id}/stats/cs2"
            )
        return self._player_cs2_stats_cache[player_id]

    def get_player_game_stats(
        self, player_id: str, *, offset: int = 0, limit: int = GAME_STATS_FETCH_LIMIT
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/players/{player_id}/games/cs2/stats",
            params={"offset": offset, "limit": limit},
        )

    def lookup_player_by_nickname(self, nickname: str) -> dict[str, Any]:
        return self._request(
            "GET",
            "/players",
            params={"nickname": nickname, "game": "cs2"},
        )

    def get_player_history(
        self,
        player_id: str,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/players/{player_id}/history",
            params={"game": "cs2", "offset": offset, "limit": limit},
        )


def _first_present(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in data and data[key] is not None and data[key] != "":
            return data[key]
    return None


def _parse_float(value: Any, default: float | None = None) -> float:
    if value is None:
        if default is not None:
            return default
        raise ValueError("пустое значение")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("%", "").replace(",", ".")
    if not text:
        if default is not None:
            return default
        raise ValueError("пустое значение")
    return float(text)


def _parse_optional_stat(stats: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    raw = _first_present(stats, keys)
    if raw is None:
        return None
    try:
        return _parse_float(raw)
    except ValueError:
        return None


def _as_percent(value: float) -> float:
    return value * 100 if value <= 1 else value


def normalize_map_key(raw_map: str | None) -> str | None:
    if not raw_map:
        return None
    key = raw_map.strip().lower()
    if key in MAP_DISPLAY_NAMES:
        return key
    for map_key, display in MAP_DISPLAY_NAMES.items():
        if display.lower() == key:
            return map_key
    if key.startswith("de_"):
        return key
    return f"de_{key.replace(' ', '_')}"


def format_map_name(raw_map: str | None) -> str:
    if not raw_map:
        return "Неизвестно"
    key = normalize_map_key(raw_map) or raw_map.strip().lower()
    if key in MAP_DISPLAY_NAMES:
        return MAP_DISPLAY_NAMES[key]
    if key.startswith("de_"):
        return key[3:].replace("_", " ").title()
    return raw_map.replace("_", " ").title()


def extract_match_id(text: str) -> str | None:
    match = MATCH_ID_PATTERN.search(text.strip())
    return match.group(1) if match else None


def get_map_from_match(match: dict[str, Any]) -> str | None:
    voting = match.get("voting")
    if not voting:
        return None
    maps_block = voting.get("map") or voting.get("maps")
    if not maps_block:
        return None
    pick = maps_block.get("pick")
    if isinstance(pick, list) and pick:
        return str(pick[0])
    if isinstance(pick, str):
        return pick
    entities = maps_block.get("entities") or []
    if entities:
        entity = entities[0]
        if isinstance(entity, dict):
            return (
                entity.get("game_map_name")
                or entity.get("name")
                or entity.get("guid")
            )
        return str(entity)
    return None


def get_map_from_stats(stats: dict[str, Any]) -> str | None:
    rounds = stats.get("rounds") or []
    if not rounds:
        return None
    round_stats = rounds[0].get("round_stats") or {}
    return round_stats.get("Map") or round_stats.get("map")


def resolve_match_map(service: FaceitService, match_id: str, match: dict[str, Any]) -> str:
    raw_map = get_map_from_match(match)
    if not raw_map:
        try:
            raw_map = get_map_from_stats(service.get_match_stats(match_id))
        except (FaceitNotFoundError, FaceitAPIError):
            pass
    map_key = normalize_map_key(raw_map)
    if not map_key:
        raise ValueError("Не удалось определить карту матча")
    return map_key


def get_opponent_roster(match: dict[str, Any], user_nickname: str) -> list[dict[str, Any]]:
    teams: dict[str, Any] = match.get("teams") or {}
    if len(teams) < 2:
        raise ValueError("В матче не найдены две команды")

    team_list = list(teams.values())
    user_lower = user_nickname.lower()
    my_index: int | None = None

    for index, team in enumerate(team_list):
        roster = team.get("roster") or []
        if any(
            (player.get("nickname") or "").lower() == user_lower for player in roster
        ):
            my_index = index
            break

    if my_index is None:
        raise ValueError(
            f"Игрок «{user_nickname}» не найден в этом матче. "
            "Проверьте ник командой /setnick."
        )

    opponent_index = 1 - my_index if len(team_list) == 2 else (my_index + 1) % len(
        team_list
    )
    roster = team_list[opponent_index].get("roster") or []
    if not roster:
        raise ValueError("Состав команды соперника пуст")
    return roster


def get_user_from_match(
    match: dict[str, Any], user_nickname: str
) -> dict[str, Any] | None:
    user_lower = user_nickname.lower()
    for team in (match.get("teams") or {}).values():
        for player in team.get("roster") or []:
            if (player.get("nickname") or "").lower() == user_lower:
                return player
    return None


def get_map_segment_stats(stats_payload: dict[str, Any], map_key: str) -> dict[str, Any]:
    for segment in stats_payload.get("segments") or []:
        if (segment.get("type") or "").lower() != "map":
            continue
        label = normalize_map_key(segment.get("label"))
        if label == map_key:
            return segment.get("stats") or {}
    return {}


def parse_map_skill_from_match(stats: dict[str, Any]) -> tuple[float, float] | None:
    kd_raw = _first_present(stats, STAT_KD_KEYS)
    kills_raw = _first_present(stats, STAT_KILLS_KEYS)

    if kd_raw is None and kills_raw is None:
        return None

    try:
        kd = _parse_float(kd_raw, default=0.0) if kd_raw is not None else 0.0
        kills = _parse_float(kills_raw, default=0.0) if kills_raw is not None else 0.0
    except ValueError:
        return None

    return kd, kills


def parse_map_skill_from_segment(stats: dict[str, Any]) -> MapSkill | None:
    kd_raw = _first_present(stats, STAT_KD_KEYS)
    avg_kills_raw = _first_present(stats, STAT_AVG_KILLS_KEYS)

    if kd_raw is None and avg_kills_raw is None:
        return None

    try:
        kd = _parse_float(kd_raw, default=0.0) if kd_raw is not None else 0.0
        avg_kills = (
            _parse_float(avg_kills_raw, default=0.0) if avg_kills_raw is not None else 0.0
        )
    except ValueError:
        return None

    return MapSkill(kd_ratio=kd, avg_kills=avg_kills)


def collect_recent_map_match_stats(
    service: FaceitService, player_id: str, map_key: str
) -> list[dict[str, Any]]:
    """Статистика игрока из последних матчей на указанной карте."""
    try:
        payload = service.get_player_game_stats(player_id)
    except (FaceitNotFoundError, FaceitAPIError):
        return []

    per_match: list[dict[str, Any]] = []
    for item in payload.get("items") or []:
        if len(per_match) >= RECENT_MAP_MATCHES_LIMIT:
            break
        match_id = item.get("match_id")
        item_stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}

        if match_id:
            try:
                mstats = service.get_match_stats(match_id)
            except (FaceitNotFoundError, FaceitAPIError):
                continue
            if normalize_map_key(get_map_from_stats(mstats)) != map_key:
                continue
            pstats = extract_player_match_stats(mstats, player_id)
            if pstats:
                per_match.append(pstats)

    return per_match


def get_player_map_match_count(
    service: FaceitService, player_id: str, map_key: str
) -> int:
    """Количество матчей игрока на карте (из cs2 stats или истории game stats)."""
    try:
        cs2_stats = service.get_player_cs2_stats(player_id)
    except (FaceitNotFoundError, FaceitAPIError):
        cs2_stats = None

    if cs2_stats:
        map_stats = get_map_segment_stats(cs2_stats, map_key)
        raw = _first_present(map_stats, STAT_MAP_MATCHES_KEYS) if map_stats else None
        if raw is not None:
            try:
                return max(0, int(_parse_float(raw)))
            except ValueError:
                pass

    try:
        payload = service.get_player_game_stats(player_id, limit=GAME_STATS_FETCH_LIMIT)
    except (FaceitNotFoundError, FaceitAPIError):
        return 0

    count = 0
    for item in payload.get("items") or []:
        item_stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
        if normalize_map_key(item_stats.get("Map") or item_stats.get("map")) == map_key:
            count += 1
    return count


def compute_confidence_score(player_map_counts: list[int]) -> ConfidenceScore:
    total_matches = sum(player_map_counts)
    low_match_players = sum(
        1 for count in player_map_counts if count < CONFIDENCE_MIN_MATCHES
    )

    base = min(
        CONFIDENCE_MAX_PERCENT,
        max(CONFIDENCE_BASE_PERCENT, CONFIDENCE_BASE_PERCENT + total_matches // 18),
    )
    if low_match_players == 0:
        percent = base
    else:
        percent = max(
            CONFIDENCE_MIN_PERCENT,
            base - low_match_players * CONFIDENCE_PENALTY_PER_PLAYER,
        )

    return ConfidenceScore(
        percent=percent,
        total_matches=total_matches,
        low_match_players=low_match_players,
    )


def _low_match_players_warning(count: int) -> str:
    if count == 1:
        return "⚠️ У одного игрока менее 10 игр на карте."
    if count == 2:
        return "⚠️ У двух игроков менее 10 игр на карте."
    return f"⚠️ У {count} игроков менее 10 игр на карте."


def format_confidence(confidence: ConfidenceScore) -> list[str]:
    lines = [f"📊 Надёжность анализа: {confidence.percent}%"]
    if confidence.low_match_players == 0:
        lines.append(f"(основано на {confidence.total_matches} матчах игроков)")
    else:
        lines.append(_low_match_players_warning(confidence.low_match_players))
    lines.append("")
    return lines


def average_map_skill(match_stats_list: list[dict[str, Any]]) -> MapSkill | None:
    kd_values: list[float] = []
    kills_values: list[float] = []

    for stats in match_stats_list:
        parsed = parse_map_skill_from_match(stats)
        if parsed is None:
            continue
        kd_values.append(parsed[0])
        kills_values.append(parsed[1])

    if not kills_values:
        return None

    return MapSkill(
        kd_ratio=sum(kd_values) / len(kd_values),
        avg_kills=sum(kills_values) / len(kills_values),
    )


def get_player_map_skill(service: FaceitService, player_id: str, map_key: str) -> MapSkill:
    recent = collect_recent_map_match_stats(service, player_id, map_key)
    skill = average_map_skill(recent)
    if skill and len(recent) >= MIN_MAP_MATCHES_FOR_SKILL:
        return skill

    stats = service.get_player_cs2_stats(player_id)
    segment_stats = get_map_segment_stats(stats, map_key)
    skill = parse_map_skill_from_segment(segment_stats)
    if skill is None:
        raise ValueError(
            f"Нет статистики на карте {format_map_name(map_key)} "
            f"(нужны недавние матчи на этой карте)"
        )
    return skill


def _normalize_position_token(text: str) -> str:
    return re.sub(r"[\s_/\-]+", " ", str(text).strip().lower())


def _canonical_position(map_key: str, raw: str) -> str | None:
    positions = MAP_VALID_POSITIONS.get(map_key)
    if not positions or not raw:
        return None

    token = _normalize_position_token(raw)
    by_lower = {position.lower(): position for position in positions}
    if token in by_lower:
        return by_lower[token]

    alias = POSITION_ALIASES.get(map_key, {}).get(token)
    if alias:
        return alias

    for alias_key, position in POSITION_ALIASES.get(map_key, {}).items():
        if alias_key in token or token in alias_key:
            return position

    return None


def _extract_member_roles(member: dict[str, Any]) -> list[str]:
    collected: list[str] = []
    for key in MEMBER_ROLE_KEYS:
        raw = member.get(key)
        if isinstance(raw, str) and raw.strip():
            collected.append(raw.strip())
        elif isinstance(raw, list):
            for item in raw:
                if isinstance(item, str) and item.strip():
                    collected.append(item.strip())
    return collected


def _resolve_nuke_rotation_main(
    heatmap_scores: dict[str, float], stats: dict[str, Any]
) -> str | None:
    rotation = heatmap_scores.get("Rotation", 0.0)
    main = heatmap_scores.get("Main", 0.0)
    if rotation <= 0 and main <= 0:
        return None

    haven_signal = 0.0
    main_signal = 0.0
    for key, value in stats.items():
        key_l = str(key).lower()
        try:
            num = _parse_float(value)
        except (TypeError, ValueError):
            continue
        if "haven" in key_l:
            haven_signal += num
        if key_l == "main" or " main " in f" {key_l} ":
            main_signal += num

    if haven_signal > main_signal * 1.1:
        return "Rotation"
    if main_signal > haven_signal * 1.1:
        return "Main"
    return "Rotation" if rotation >= main else "Main"


def extract_heatmap_scores(stats: dict[str, Any], map_key: str) -> dict[str, float]:
    """Зоны из heatmap/position полей Faceit API (сегменты и матч-статы)."""
    if map_key not in MAP_VALID_POSITIONS or not stats:
        return {}

    scores: dict[str, float] = {}
    aliases = POSITION_ALIASES.get(map_key, {})

    def add_score(position: str, value: Any) -> None:
        try:
            amount = _parse_float(value)
        except (TypeError, ValueError):
            return
        if amount <= 0:
            return
        scores[position] = scores.get(position, 0.0) + amount

    def absorb_mapping(mapping: dict[str, Any]) -> None:
        for key, value in mapping.items():
            position = _canonical_position(map_key, str(key))
            if position:
                add_score(position, value)
            elif isinstance(value, dict):
                absorb_mapping(value)

    for container_key in HEATMAP_CONTAINER_KEYS:
        raw = stats.get(container_key)
        if isinstance(raw, dict):
            absorb_mapping(raw)

    for key, value in stats.items():
        if value is None:
            continue
        key_l = str(key).lower()
        position = _canonical_position(map_key, str(key))
        if not position:
            continue
        if any(hint in key_l for hint in HEATMAP_STAT_HINTS) or "heat" in key_l:
            add_score(position, value)
            continue
        for alias in aliases:
            if alias in key_l and any(hint in key_l for hint in HEATMAP_STAT_HINTS):
                add_score(position, value)
                break

    if map_key == "de_nuke" and ("Rotation" in scores or "Main" in scores):
        resolved = _resolve_nuke_rotation_main(scores, stats)
        if resolved:
            combined = scores.get("Rotation", 0.0) + scores.get("Main", 0.0)
            if combined > 0:
                scores[resolved] = combined

    return scores


def _heatmap_winner(heatmap_scores: dict[str, float]) -> str | None:
    if not heatmap_scores:
        return None
    ranked = sorted(heatmap_scores.items(), key=lambda item: item[1], reverse=True)
    best_position, best_value = ranked[0]
    if best_value <= 0:
        return None
    if len(ranked) == 1:
        return best_position
    second_value = ranked[1][1]
    if second_value <= 0 or best_value >= second_value * MIN_HEATMAP_CONFIDENCE_RATIO:
        return best_position
    return None


def _pick_role_candidates_with_heatmap(
    roles: list[str], map_key: str, heatmap_scores: dict[str, float]
) -> str | None:
    role_map = TACTICAL_ROLE_CANDIDATES.get(map_key, {})
    for role in roles:
        candidates = role_map.get(_normalize_position_token(role))
        if not candidates:
            continue
        if len(candidates) == 1:
            only = candidates[0]
            if not heatmap_scores:
                continue
            if heatmap_scores.get(only, 0.0) > 0:
                return only
            continue
        filtered = {
            position: heatmap_scores[position]
            for position in candidates
            if heatmap_scores.get(position, 0.0) > 0
        }
        if not filtered:
            continue
        return max(filtered, key=filtered.get)
    return None


def collect_faceit_heatmap_scores(
    service: FaceitService, player_id: str, map_key: str
) -> dict[str, float]:
    combined: dict[str, float] = {}

    def merge(scores: dict[str, float]) -> None:
        for position, value in scores.items():
            combined[position] = combined.get(position, 0.0) + value

    if player_id:
        try:
            cs2_stats = service.get_player_cs2_stats(player_id)
            merge(extract_heatmap_scores(get_map_segment_stats(cs2_stats, map_key), map_key))
            for segment in cs2_stats.get("segments") or []:
                if normalize_map_key(segment.get("label")) != map_key:
                    continue
                seg_type = (segment.get("type") or "").lower()
                if seg_type in ("heatmap", "position", "zone", "map"):
                    merge(
                        extract_heatmap_scores(segment.get("stats") or {}, map_key)
                    )
        except (FaceitNotFoundError, FaceitAPIError):
            pass

        for pstats in collect_recent_map_match_stats(service, player_id, map_key):
            merge(extract_heatmap_scores(pstats, map_key))

    return combined


def position_attack_site(map_key: str, position: str) -> str:
    if not position or position == "?":
        return ""
    return POSITION_ATTACK_SITE.get(map_key, {}).get(position, "")


def detect_player_position(
    service: FaceitService,
    member: dict[str, Any],
    map_key: str,
) -> tuple[str, str]:
    """Позиция только из Faceit API (roles, heatmap). Dust2/Overpass — без позиций."""
    if map_key in MAPS_WITHOUT_POSITIONS:
        return "", ""

    player_id = member.get("player_id") or ""
    roles = _extract_member_roles(member)

    for role in roles:
        direct = _canonical_position(map_key, role)
        if direct:
            return direct, "роль"

    heatmap_scores = collect_faceit_heatmap_scores(service, player_id, map_key)
    for key in MEMBER_HEATMAP_KEYS:
        raw = member.get(key)
        if isinstance(raw, dict):
            for position, value in extract_heatmap_scores(raw, map_key).items():
                heatmap_scores[position] = heatmap_scores.get(position, 0.0) + value

    heatmap_position = _heatmap_winner(heatmap_scores)
    if heatmap_position:
        return heatmap_position, "heatmap"

    role_pick = _pick_role_candidates_with_heatmap(roles, map_key, heatmap_scores)
    if role_pick:
        return role_pick, "роль+heatmap"

    return "?", ""


def extract_player_match_stats(
    match_stats: dict[str, Any], player_id: str
) -> dict[str, Any]:
    for round_data in match_stats.get("rounds") or []:
        for team in round_data.get("teams") or []:
            for player in team.get("players") or []:
                if player.get("player_id") == player_id:
                    return player.get("player_stats") or {}
    return {}


def skill_sort_key(player: AnalyzedPlayer) -> tuple[float, float]:
    s = player.skill
    return (s.kd_ratio, s.avg_kills)


def average_team_skill(players: list[AnalyzedPlayer]) -> MapSkill:
    count = len(players)
    return MapSkill(
        kd_ratio=sum(p.skill.kd_ratio for p in players) / count,
        avg_kills=sum(p.skill.avg_kills for p in players) / count,
    )


def player_tier_emoji(
    player: AnalyzedPlayer,
    weakest: AnalyzedPlayer,
    strongest: AnalyzedPlayer,
) -> str:
    if player.player_id == weakest.player_id:
        return "🔴"
    if player.player_id == strongest.player_id:
        return "🟢"
    return "🟡"


def format_player_stats_line(
    nickname: str, position: str, kd: float, avg_kills: float
) -> str:
    stats = f"(K/D: {kd:.2f}, AVG: {avg_kills:.0f})"
    if not position:
        return f"{nickname} {stats}"
    if position == "?":
        return f"{nickname} — ? {stats}"
    return f"{nickname} — {position} {stats}"


def format_main_threat(player: AnalyzedPlayer) -> list[str]:
    skill = player.skill
    return [
        "",
        "⚠️ ГЛАВНАЯ УГРОЗА:",
        format_player_stats_line(
            player.nickname, player.site, skill.kd_ratio, skill.avg_kills
        ),
    ]


def parse_match_win(stats: dict[str, Any]) -> bool | None:
    result = _first_present(stats, ("Result", "result", "Win"))
    if result is None:
        return None
    if isinstance(result, bool):
        return result
    text = str(result).strip().lower()
    if text in ("1", "win", "w", "true", "yes"):
        return True
    if text in ("0", "loss", "l", "false", "no"):
        return False
    try:
        return int(float(text)) == 1
    except ValueError:
        return None


def _match_key_from_game_item(item: dict[str, Any], player_id: str) -> str:
    match_id = item.get("match_id")
    if match_id:
        return str(match_id)
    stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
    match_id = stats.get("Match Id") or stats.get("match_id")
    if match_id:
        return str(match_id)
    return (
        f"{player_id}:{stats.get('Created At')}:"
        f"{stats.get('Map') or stats.get('map')}"
    )


def collect_recent_game_stat_items(
    service: FaceitService,
    player_ids: list[str],
    *,
    limit: int = MAP_POOL_MATCHES_LIMIT,
) -> list[dict[str, Any]]:
    """Последние уникальные матчи из /players/{id}/games/cs2/stats."""
    seen: set[str] = set()
    ordered: list[dict[str, Any]] = []

    for player_id in player_ids:
        if len(ordered) >= limit:
            break
        try:
            payload = service.get_player_game_stats(player_id, limit=limit)
        except (FaceitNotFoundError, FaceitAPIError):
            continue
        for item in payload.get("items") or []:
            if len(ordered) >= limit:
                break
            match_key = _match_key_from_game_item(item, player_id)
            if match_key in seen:
                continue
            seen.add(match_key)
            ordered.append(item)

    return ordered


def build_map_winrates_from_game_items(
    items: list[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    """map_key → {wins, played} из items game stats."""
    pool: dict[str, dict[str, int]] = {}

    for item in items:
        stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
        map_key = normalize_map_key(stats.get("Map") or stats.get("map"))
        if not map_key:
            continue

        won = parse_match_win(stats)
        if won is None:
            continue

        bucket = pool.setdefault(map_key, {"wins": 0, "played": 0})
        bucket["played"] += 1
        if won:
            bucket["wins"] += 1

    return pool


def classify_map_recommendation(
    opponent_win_rate: float, user_win_rate: float | None
) -> tuple[str, str] | None:
    if user_win_rate is None:
        return None

    gap = user_win_rate - opponent_win_rate
    if gap <= -20:
        return "ban", "уверенный бан"
    if gap >= 10:
        return "pick", "твоя карта"
    return "decider", "равные шансы"


def build_map_recommendations(
    opponent_pool: dict[str, dict[str, int]],
    user_pool: dict[str, dict[str, int]],
) -> MapPoolAnalysis:
    candidates: list[MapRecommendation] = []

    for map_key, opp_data in opponent_pool.items():
        if opp_data["played"] < MIN_MAP_MATCHES_FOR_REC:
            continue

        opponent_win_rate = opp_data["wins"] / opp_data["played"] * 100
        user_data = user_pool.get(map_key)
        user_sufficient = (
            user_data is not None and user_data["played"] >= MIN_MAP_MATCHES_FOR_REC
        )
        user_win_rate: float | None = None
        if user_sufficient and user_data is not None:
            user_win_rate = user_data["wins"] / user_data["played"] * 100

        classified = classify_map_recommendation(opponent_win_rate, user_win_rate)
        if classified is None:
            if not user_sufficient:
                candidates.append(
                    MapRecommendation(
                        map_key=map_key,
                        display_name=format_map_name(map_key),
                        action="decider",
                        opponent_win_rate=opponent_win_rate,
                        user_win_rate=None,
                        verdict="недостаточно данных",
                    )
                )
            continue

        action, verdict = classified
        candidates.append(
            MapRecommendation(
                map_key=map_key,
                display_name=format_map_name(map_key),
                action=action,
                opponent_win_rate=opponent_win_rate,
                user_win_rate=user_win_rate,
                verdict=verdict,
            )
        )

    def gap(rec: MapRecommendation) -> float:
        return (rec.user_win_rate or 0.0) - rec.opponent_win_rate

    bans = sorted(
        (rec for rec in candidates if rec.action == "ban"),
        key=gap,
    )[:MAP_POOL_TOP_BANS]
    picks = sorted(
        (rec for rec in candidates if rec.action == "pick"),
        key=gap,
        reverse=True,
    )[:MAP_POOL_TOP_PICKS]
    deciders = sorted(
        (rec for rec in candidates if rec.action == "decider"),
        key=lambda rec: abs(gap(rec)),
    )

    return MapPoolAnalysis(bans=bans, picks=picks, deciders=deciders)


def _format_user_win_rate_line(rec: MapRecommendation) -> str:
    if rec.user_win_rate is None:
        return "— твой винрейт: недостаточно данных"
    return f"— твой винрейт: {rec.user_win_rate:.0f}%"


def _format_map_rec_lines(prefix: str, rec: MapRecommendation) -> list[str]:
    return [
        f"{prefix} {rec.display_name}",
        f"— винрейт соперника: {rec.opponent_win_rate:.0f}%",
        _format_user_win_rate_line(rec),
        f"— вердикт: {rec.verdict}",
        "",
    ]


def format_map_recommendations(analysis: MapPoolAnalysis) -> list[str]:
    if not analysis.bans and not analysis.picks and not analysis.deciders:
        return []

    lines = ["", "🗺 BAN/PICK:", ""]

    for rec in analysis.bans:
        lines.extend(_format_map_rec_lines("❌ БАНИТЬ", rec))
    for rec in analysis.picks:
        lines.extend(_format_map_rec_lines("✅ ПИКАТЬ", rec))
    for rec in analysis.deciders:
        lines.extend(
            [
                f"🎯 ДЕСАЙДЕР: {rec.display_name}",
                f"— винрейт соперника: {rec.opponent_win_rate:.0f}%",
                _format_user_win_rate_line(rec),
                f"— вердикт: {rec.verdict}",
                "",
            ]
        )

    if lines[-1] == "":
        lines.pop()
    return lines


def analyze_map_pool(
    service: FaceitService,
    opponent_player_ids: list[str],
    user_player_id: str,
) -> MapPoolAnalysis | None:
    """Винрейт по картам за последние MAP_POOL_MATCHES_LIMIT матчей."""
    opponent_items = collect_recent_game_stat_items(service, opponent_player_ids)
    user_items = collect_recent_game_stat_items(service, [user_player_id])

    if not opponent_items or not user_items:
        return None

    opponent_pool = build_map_winrates_from_game_items(opponent_items)
    user_pool = build_map_winrates_from_game_items(user_items)
    analysis = build_map_recommendations(opponent_pool, user_pool)

    if not analysis.bans and not analysis.picks and not analysis.deciders:
        return None
    return analysis


def extract_opponent_team_match_stats(
    match_stats: dict[str, Any], opponent_player_ids: set[str]
) -> tuple[float, list[tuple[str, dict[str, Any]]]]:
    """Раунды матча и player_stats соперников (player_id, stats)."""
    rounds = 0.0
    players: list[tuple[str, dict[str, Any]]] = []

    for round_data in match_stats.get("rounds") or []:
        round_stats = round_data.get("round_stats") or {}
        rounds = _parse_float(
            _first_present(round_stats, STAT_MATCH_ROUNDS_KEYS), default=0.0
        )
        for team in round_data.get("teams") or []:
            for player in team.get("players") or []:
                player_id = player.get("player_id")
                if player_id in opponent_player_ids:
                    stats = player.get("player_stats") or {}
                    if stats:
                        players.append((player_id, stats))

    return rounds, players


def collect_opponent_map_match_bundles(
    service: FaceitService,
    opponent_player_ids: list[str],
    map_key: str,
    *,
    limit: int = SIDE_STYLE_MATCHES_LIMIT,
) -> list[dict[str, Any]]:
    """Последние матчи соперника на карте с полной match stats."""
    opponent_ids = set(opponent_player_ids)
    seen: set[str] = set()
    bundles: list[dict[str, Any]] = []

    for player_id in opponent_player_ids:
        if len(bundles) >= limit:
            break
        try:
            payload = service.get_player_game_stats(player_id, limit=GAME_STATS_FETCH_LIMIT)
        except (FaceitNotFoundError, FaceitAPIError):
            continue

        for item in payload.get("items") or []:
            if len(bundles) >= limit:
                break
            item_stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
            if normalize_map_key(item_stats.get("Map") or item_stats.get("map")) != map_key:
                continue

            match_key = _match_key_from_game_item(item, player_id)
            if match_key in seen:
                continue

            try:
                match_stats = service.get_match_stats(match_key)
            except (FaceitNotFoundError, FaceitAPIError):
                continue

            rounds, team_stats = extract_opponent_team_match_stats(
                match_stats, opponent_ids
            )
            if rounds <= 0 or len(team_stats) < 3:
                continue

            seen.add(match_key)
            bundles.append(
                {
                    "rounds": rounds,
                    "players": team_stats,
                }
            )

    return bundles


def _zone_label(map_key: str, site: str) -> str:
    return MAP_ZONE_LABELS.get(map_key, {}).get(site, site)


def _estimate_avg_round_seconds(
    entry_rate: float,
    first_kills_per_round: float,
    plants_per_round: float,
    utility_per_round: float,
) -> float:
    seconds = (
        72.0
        - entry_rate * 48.0
        - first_kills_per_round * 6.0
        - plants_per_round * 14.0
        + max(0.0, utility_per_round - 0.25) * 8.0
    )
    return max(24.0, min(88.0, seconds))


def _estimate_fast_round_pct(
    entry_rate: float, first_kills_per_round: float, avg_round_seconds: float
) -> float:
    return min(
        85.0,
        max(
            12.0,
            entry_rate * 90.0
            + first_kills_per_round * 45.0
            + max(0.0, 45.0 - avg_round_seconds) * 1.4,
        ),
    )


def aggregate_playstyle_metrics(
    bundles: list[dict[str, Any]],
    players: list[AnalyzedPlayer],
    map_key: str,
) -> AggregatedPlaystyleMetrics | None:
    if not bundles:
        return None

    player_sites = {
        player.player_id: position_attack_site(map_key, player.site)
        for player in players
    }
    site_counts = Counter(
        site for site in player_sites.values() if site in ("A", "B", "MID")
    )
    stack_site, stack_players = None, 0
    for site, count in site_counts.items():
        if count >= 4:
            stack_site, stack_players = site, count
            break

    total_rounds = 0.0
    total_entry = 0.0
    total_first_kills = 0.0
    total_plants = 0.0
    total_defuses = 0.0
    total_utility = 0.0
    total_pistol = 0.0
    zone_pressure = {"A": 0.0, "B": 0.0, "MID": 0.0}
    ct_push_by_site = {"A": 0.0, "B": 0.0, "MID": 0.0}

    for bundle in bundles:
        rounds = float(bundle["rounds"])
        if rounds <= 0:
            continue
        total_rounds += rounds

        for player_id, stats in bundle["players"]:
            site = player_sites.get(player_id) or ""
            if site not in zone_pressure:
                continue
            entry = _parse_optional_stat(stats, STAT_TOTAL_ENTRY_COUNT_KEYS) or 0.0
            first_kills = _parse_optional_stat(stats, STAT_MATCH_FIRST_KILLS_KEYS) or 0.0
            plants = _parse_optional_stat(stats, STAT_PLANTS_KEYS) or 0.0
            defuses = _parse_optional_stat(stats, STAT_DEFUSES_KEYS) or 0.0
            utility = (
                (_parse_optional_stat(stats, STAT_UTILITY_PER_ROUND_KEYS) or 0.0)
                * rounds
            )
            pistol = _parse_optional_stat(stats, STAT_PISTOL_KILLS_KEYS) or 0.0

            total_entry += entry
            total_first_kills += first_kills
            total_plants += plants
            total_defuses += defuses
            total_utility += utility
            total_pistol += pistol

            zone_pressure[site] += entry * 2.0 + plants * 3.0 + first_kills * 1.5
            ct_push_by_site[site] += entry + first_kills * 0.5

    if total_rounds <= 0:
        return None

    entry_rate = total_entry / total_rounds
    first_kills_per_round = total_first_kills / total_rounds
    plants_per_round = total_plants / total_rounds
    defuses_per_round = total_defuses / total_rounds
    utility_per_round = total_utility / total_rounds
    pistol_kills_per_round = total_pistol / total_rounds

    avg_round_seconds = _estimate_avg_round_seconds(
        entry_rate, first_kills_per_round, plants_per_round, utility_per_round
    )
    fast_round_pct = _estimate_fast_round_pct(
        entry_rate, first_kills_per_round, avg_round_seconds
    )

    execute_tendency = min(
        1.0,
        entry_rate * 1.6 + utility_per_round * 0.9 + plants_per_round * 2.5,
    )
    default_tendency = max(
        0.0,
        min(
            1.0,
            (1.0 - execute_tendency) * 0.85
            + (0.25 if entry_rate < 0.14 else 0.0)
            + (0.15 if utility_per_round < 0.22 else 0.0),
        ),
    )

    zone_total = sum(zone_pressure.values()) or 1.0
    zone_shares = {site: value / zone_total for site, value in zone_pressure.items()}
    split_tendency = 1.0 - max(zone_shares.values())

    ct_push_site = max(ct_push_by_site, key=ct_push_by_site.get) if ct_push_by_site else None
    ct_push_pct = min(
        85.0,
        (ct_push_by_site.get(ct_push_site or "MID", 0.0) / total_rounds) * 120.0,
    )

    return AggregatedPlaystyleMetrics(
        match_count=len(bundles),
        total_rounds=total_rounds,
        entry_rate=entry_rate,
        first_kills_per_round=first_kills_per_round,
        plants_per_round=plants_per_round,
        defuses_per_round=defuses_per_round,
        utility_per_round=utility_per_round,
        pistol_kills_per_round=pistol_kills_per_round,
        avg_round_seconds=avg_round_seconds,
        fast_round_pct=fast_round_pct,
        execute_tendency=execute_tendency,
        default_tendency=default_tendency,
        split_tendency=split_tendency,
        zone_pressure=zone_pressure,
        stack_site=stack_site,
        stack_players=stack_players,
        ct_push_site=ct_push_site,
        ct_push_pct=ct_push_pct,
    )


def _dominant_attack_zone(metrics: AggregatedPlaystyleMetrics) -> str:
    if not metrics.zone_pressure:
        return "B"
    return max(metrics.zone_pressure, key=metrics.zone_pressure.get)


def generate_ct_playstyle_lines(
    metrics: AggregatedPlaystyleMetrics, map_key: str
) -> list[str]:
    lines: list[str] = []

    if metrics.ct_push_pct >= 40 and metrics.ct_push_site:
        label = _zone_label(map_key, metrics.ct_push_site)
        lines.append(
            f"— Часто пушат {label} ({metrics.ct_push_pct:.0f}% раундов)"
        )

    if metrics.stack_site and metrics.stack_players >= 4:
        label = _zone_label(map_key, metrics.stack_site)
        lines.append(
            f"— Любят stack {label} ({metrics.stack_players} игрока) "
            "после проигранного раунда"
        )

    if metrics.default_tendency >= 0.55:
        lines.append("— Слабый default: долго раскачиваются, мало информации")
    elif metrics.entry_rate < 0.13 and metrics.utility_per_round < 0.24:
        lines.append("— Пассивная защита: мало ранних выходов и инфо-утилити")

    if metrics.defuses_per_round >= 0.08:
        zone = _dominant_attack_zone(metrics)
        label = _zone_label(map_key, zone)
        lines.append(f"— Часто играют от retake на {label}")

    return lines


def generate_t_playstyle_lines(
    metrics: AggregatedPlaystyleMetrics, map_key: str
) -> list[str]:
    lines: list[str] = []
    attack_zone = _dominant_attack_zone(metrics)
    label = _zone_label(map_key, attack_zone)

    if metrics.fast_round_pct >= 40 or metrics.avg_round_seconds <= 38:
        lines.append(
            f"— Быстрые выходы на {label} "
            f"(среднее время раунда: {metrics.avg_round_seconds:.0f} сек)"
        )

    if metrics.split_tendency < 0.38:
        lines.append("— Мало split'ов: редко делят точки")

    if (
        metrics.pistol_kills_per_round >= 0.10
        and attack_zone == "B"
        and metrics.fast_round_pct >= 35
    ):
        lines.append(f"— После эко любят rush {label} с P90")
    elif metrics.execute_tendency >= 0.52:
        lines.append(
            f"— Execute-стиль на {label}: "
            f"{'мало дефолта' if metrics.default_tendency >= 0.45 else 'быстрые тактики'}"
        )
    elif metrics.default_tendency >= 0.55:
        lines.append("— Часто играют от дефолта, execute включают поздно")

    return lines


def analyze_team_playstyle(
    service: FaceitService,
    opponent_player_ids: list[str],
    players: list[AnalyzedPlayer],
    map_key: str,
) -> TeamPlaystyleAnalysis | None:
    bundles = collect_opponent_map_match_bundles(
        service, opponent_player_ids, map_key
    )
    if len(bundles) < MIN_SIDE_STYLE_MATCHES:
        return None

    metrics = aggregate_playstyle_metrics(bundles, players, map_key)
    if metrics is None:
        return None

    map_display_name = format_map_name(map_key)
    ct_lines = generate_ct_playstyle_lines(metrics, map_key)
    t_lines = generate_t_playstyle_lines(metrics, map_key)

    if not ct_lines and not t_lines:
        return None

    return TeamPlaystyleAnalysis(
        map_display_name=map_display_name,
        ct=SidePlaystyle("CT", ct_lines) if ct_lines else None,
        t=SidePlaystyle("T", t_lines) if t_lines else None,
    )


def format_playstyle_analysis(analysis: TeamPlaystyleAnalysis) -> list[str]:
    lines: list[str] = []

    if analysis.ct and analysis.ct.lines:
        lines.extend(
            [f"🧠 КАК ОНИ ИГРАЮТ ЗА CT (на {analysis.map_display_name}):"]
            + analysis.ct.lines
            + [""]
        )

    if analysis.t and analysis.t.lines:
        lines.extend(
            [f"🧠 КАК ОНИ ИГРАЮТ ЗА T (на {analysis.map_display_name}):"]
            + analysis.t.lines
        )

    if lines and lines[-1] == "":
        lines.pop()
    return lines


def pick_attack_advice(
    players: list[AnalyzedPlayer],
    team_avg: MapSkill,
    map_key: str,
) -> tuple[str, AnalyzedPlayer]:
    """Точка для атаки и слабый якорь на ней."""
    if map_key in MAPS_WITHOUT_POSITIONS:
        return "", min(players, key=skill_sort_key)

    by_site: dict[str, list[AnalyzedPlayer]] = {}
    for player in players:
        attack_site = position_attack_site(map_key, player.site)
        if attack_site not in ("A", "B", "MID"):
            continue
        by_site.setdefault(attack_site, []).append(player)

    if not by_site:
        return "", min(players, key=skill_sort_key)

    avg_key = (team_avg.kd_ratio, team_avg.avg_kills)

    def site_score(site: str) -> float:
        members = by_site[site]
        return sum(skill_sort_key(p)[0] for p in members) / len(members)

    target_site = min(by_site.keys(), key=site_score)
    weak_players = [
        p
        for p in by_site[target_site]
        if skill_sort_key(p) < avg_key
    ]
    if not weak_players:
        weak_players = [min(by_site[target_site], key=skill_sort_key)]

    anchor = min(weak_players, key=skill_sort_key)
    return target_site, anchor


def compute_hold_rating(map_stats: dict[str, Any]) -> float | None:
    """Рейтинг удержания позиции (0–1) по статистике защиты на карте."""
    components: list[tuple[float, float]] = []

    v2 = _parse_optional_stat(map_stats, STAT_1V2_WIN_KEYS)
    if v2 is not None:
        components.append((v2, 0.40))

    v1 = _parse_optional_stat(map_stats, STAT_1V1_WIN_KEYS)
    if v1 is not None:
        components.append((v1, 0.25))

    entry_rate = _parse_optional_stat(map_stats, STAT_ENTRY_RATE_KEYS)
    if entry_rate is not None:
        anchor_factor = 1.0 - min(entry_rate * 2.0, 1.0)
        components.append((anchor_factor, 0.20))

    kd = _parse_optional_stat(map_stats, STAT_KD_KEYS)
    if kd is not None:
        components.append((min(kd / 1.5, 1.0), 0.15))

    if not components:
        return None

    total_weight = sum(weight for _, weight in components)
    rating = sum(value * weight for value, weight in components) / total_weight
    return round(rating, 2)


def parse_success_defend(map_stats: dict[str, Any]) -> float | None:
    """Процент успешных защит (Win Rate % на карте из cs2 stats)."""
    win_rate = _parse_optional_stat(map_stats, STAT_WIN_RATE_KEYS)
    if win_rate is None:
        return None
    return round(_as_percent(win_rate), 0)


def compute_first_death_percent(
    map_stats: dict[str, Any], recent_matches: list[dict[str, Any]]
) -> float | None:
    """Как часто игрок умирает первым в раунде (opening deaths)."""
    direct = _first_present(map_stats, STAT_FIRST_DEATH_KEYS)
    if direct is not None:
        return round(_as_percent(_parse_float(direct)), 0)

    total_rounds = 0.0
    opening_deaths = 0.0
    for pstats in recent_matches:
        rounds = _parse_optional_stat(pstats, STAT_MATCH_ROUNDS_KEYS)
        if not rounds or rounds <= 0:
            continue
        entry = _parse_optional_stat(pstats, STAT_TOTAL_ENTRY_COUNT_KEYS) or 0.0
        entry_wins = _parse_optional_stat(pstats, STAT_TOTAL_ENTRY_WINS_KEYS) or 0.0
        first_kills = _parse_optional_stat(pstats, STAT_MATCH_FIRST_KILLS_KEYS) or 0.0
        deaths = _parse_optional_stat(pstats, STAT_MATCH_DEATHS_KEYS) or 0.0

        failed_entry = max(0.0, entry - entry_wins)
        passive_opening = max(0.0, deaths - first_kills - entry_wins) * 0.35
        total_rounds += rounds
        opening_deaths += failed_entry + passive_opening

    if total_rounds >= 5:
        return round(min(100.0, opening_deaths / total_rounds * 100), 0)

    ext_rounds = _parse_optional_stat(map_stats, STAT_TOTAL_ROUNDS_EXT_KEYS)
    entry_count = _parse_optional_stat(map_stats, STAT_TOTAL_ENTRY_COUNT_KEYS)
    entry_wins = _parse_optional_stat(map_stats, STAT_TOTAL_ENTRY_WINS_KEYS)
    if ext_rounds and ext_rounds > 0 and entry_count is not None and entry_wins is not None:
        rate = max(0.0, entry_count - entry_wins) / ext_rounds
        deaths = _parse_optional_stat(map_stats, STAT_MATCH_DEATHS_KEYS) or 0.0
        rounds = _parse_optional_stat(map_stats, STAT_MATCH_ROUNDS_KEYS) or ext_rounds
        if rounds > 0:
            rate = min(1.0, rate + max(0.0, deaths / rounds - rate) * 0.35)
        return round(rate * 100, 0)

    return None


def compute_flash_vulnerability(map_stats: dict[str, Any]) -> float | None:
    """Доля смертей после ослепления (если есть в данных API)."""
    direct = _first_present(map_stats, STAT_FLASH_VULN_KEYS)
    if direct is not None:
        return round(_as_percent(_parse_float(direct)), 0)

    entry_success = _parse_optional_stat(map_stats, STAT_ENTRY_SUCCESS_KEYS)
    v2_win = _parse_optional_stat(map_stats, STAT_1V2_WIN_KEYS)
    if entry_success is not None and v2_win is not None:
        vuln = (1.0 - entry_success) * (1.0 - v2_win) * 100
        return round(min(100.0, max(0.0, vuln)), 0)

    return None


def get_player_defense_stats(
    service: FaceitService,
    player_id: str,
    map_key: str,
) -> AnchorDefenseStats:
    try:
        cs2_stats = service.get_player_cs2_stats(player_id)
    except (FaceitNotFoundError, FaceitAPIError):
        return AnchorDefenseStats(None, None, None, None)

    map_stats = get_map_segment_stats(cs2_stats, map_key)
    if not map_stats:
        return AnchorDefenseStats(None, None, None, None)

    recent = collect_recent_map_match_stats(service, player_id, map_key)
    return AnchorDefenseStats(
        hold_rating=compute_hold_rating(map_stats),
        success_defend=parse_success_defend(map_stats),
        first_death_percent=compute_first_death_percent(map_stats, recent),
        flash_vulnerability=compute_flash_vulnerability(map_stats),
    )


def generate_attack_verdict(stats: AnchorDefenseStats) -> str:
    weak_hold = stats.hold_rating is not None and stats.hold_rating < 0.55
    low_defend = stats.success_defend is not None and stats.success_defend < 45
    high_flash = stats.flash_vulnerability is not None and stats.flash_vulnerability >= 50
    high_first_death = (
        stats.first_death_percent is not None and stats.first_death_percent >= 30
    )

    if (weak_hold or low_defend) and high_flash:
        return "слабый якорь, заходит с флешками."
    if (weak_hold or low_defend) and high_first_death:
        return "слабый якорь, давите быстрым пиком."
    if weak_hold or low_defend:
        return "слабый якорь, можно давить числом."
    if high_first_death:
        return "часто умирает первым — пробуйте дефолт и трейдите."
    if high_flash:
        return "уязвим к флешкам — выжигайте перед заходом."
    return "самое слабое звено на точке."


def build_attack_advice(
    players: list[AnalyzedPlayer],
    team_avg: MapSkill,
    map_key: str,
) -> AttackAdvice | None:
    if not players:
        return None

    target_site, anchor = pick_attack_advice(players, team_avg, map_key)
    stats = anchor.defense or AnchorDefenseStats(None, None, None, None)
    return AttackAdvice(
        site=target_site,
        anchor=anchor,
        stats=stats,
        verdict=generate_attack_verdict(stats),
    )


def format_attack_advice(advice: AttackAdvice) -> list[str]:
    stats = advice.stats
    if advice.site:
        header = f"🎯 ДАВИТЕ ТОЧКУ {advice.site}:"
    else:
        header = "🎯 СЛАБОЕ ЗВЕНО:"
    lines = [header]

    hold_suffix = (
        f" (hold rating: {stats.hold_rating:.2f})"
        if stats.hold_rating is not None
        else ""
    )
    lines.append(f"— Anchor: {advice.anchor.nickname}{hold_suffix}")

    if stats.success_defend is not None:
        lines.append(f"— Успешных защит: {stats.success_defend:.0f}%")

    if stats.first_death_percent is not None:
        lines.append(f"— Умирает первым: в {stats.first_death_percent:.0f}% раундов")

    if stats.flash_vulnerability is not None:
        lines.append(
            f"— Боится флешек: {stats.flash_vulnerability:.0f}% "
            "смертей после ослепления"
        )

    lines.append(f"— Вердикт: {advice.verdict}")
    return lines


def analyze_opponents(
    service: FaceitService,
    match_id: str,
    user_nickname: str,
) -> AnalysisResult:
    match = service.get_match(match_id)
    map_key = resolve_match_map(service, match_id, match)
    map_name = format_map_name(map_key)
    roster = get_opponent_roster(match, user_nickname)
    user_member = get_user_from_match(match, user_nickname)
    user_player_id = (user_member or {}).get("player_id") if user_member else None

    analyzed: list[AnalyzedPlayer] = []
    player_ids: list[str] = []
    player_map_counts: list[int] = []

    for member in roster:
        player_id = member.get("player_id")
        nickname = member.get("nickname") or "unknown"
        if not player_id:
            continue
        player_ids.append(player_id)
        player_map_counts.append(get_player_map_match_count(service, player_id, map_key))
        try:
            skill = get_player_map_skill(service, player_id, map_key)
            site, site_source = detect_player_position(service, member, map_key)
            defense = get_player_defense_stats(service, player_id, map_key)
        except (FaceitNotFoundError, FaceitAPIError, ValueError) as exc:
            logger.warning("Пропуск игрока %s: %s", nickname, exc)
            continue
        analyzed.append(
            AnalyzedPlayer(
                nickname=nickname,
                player_id=player_id,
                skill=skill,
                site=site,
                site_source=site_source,
                defense=defense,
            )
        )

    if not analyzed:
        raise ValueError(
            "Не удалось получить статистику соперников на этой карте. "
            "Возможно, у них мало игр на ней."
        )

    analyzed.sort(key=skill_sort_key)
    weakest = analyzed[0]
    strongest = analyzed[-1]
    team_avg = average_team_skill(analyzed)
    map_pool = (
        analyze_map_pool(service, player_ids, user_player_id)
        if user_player_id
        else None
    )
    attack_advice = build_attack_advice(analyzed, team_avg, map_key)
    playstyle = analyze_team_playstyle(service, player_ids, analyzed, map_key)
    confidence = compute_confidence_score(player_map_counts)

    return AnalysisResult(
        map_name=map_name,
        map_key=map_key,
        players=analyzed,
        team_avg=team_avg,
        weakest=weakest,
        strongest=strongest,
        map_pool=map_pool,
        confidence=confidence,
        attack_advice=attack_advice,
        playstyle=playstyle,
    )


def build_report(result: AnalysisResult) -> str:
    avg = result.team_avg
    lines = format_confidence(result.confidence) + [
        f"🔍 Карта: {result.map_name}",
        "",
        f"📊 Среднее по команде: K/D {avg.kd_ratio:.2f} | AVG {avg.avg_kills:.0f}",
        "",
        "👥 Команда соперника:",
    ]

    for player in result.players:
        skill = player.skill
        emoji = player_tier_emoji(player, result.weakest, result.strongest)
        lines.append(
            f"{emoji} "
            + format_player_stats_line(
                player.nickname, player.site, skill.kd_ratio, skill.avg_kills
            )
        )

    lines.extend(format_main_threat(result.strongest))

    if result.attack_advice:
        lines.extend([""] + format_attack_advice(result.attack_advice))

    if result.playstyle and (result.playstyle.ct or result.playstyle.t):
        lines.extend([""] + format_playstyle_analysis(result.playstyle))

    if result.map_pool and (
        result.map_pool.bans or result.map_pool.picks or result.map_pool.deciders
    ):
        lines.extend(format_map_recommendations(result.map_pool))

    return "\n".join(lines)


@dataclass
class PeriodStats:
    kd: float
    avg_kills: float
    win_rate: float
    wins: int
    played: int
    first_kills_avg: float
    hs_pct: float
    entry_win_rate: float | None
    clutch_1vx_win_rate: float | None
    opening_death_rate: float
    early_attack_impact: float


@dataclass
class ProgressReport:
    style: str
    form_score: float
    older: PeriodStats
    recent: PeriodStats
    current_elo: int | None
    elo_delta: int | None
    improved: list[str]
    declined: list[str]
    growth_zones: list[str]
    requested_count: int
    match_count: int
    partial_note: str | None = None


def _trend_arrow(old: float, new: float, *, higher_is_better: bool = True) -> str:
    if abs(new - old) < 1e-6:
        return "➡️"
    improved = new > old if higher_is_better else new < old
    return "↗️" if improved else "↘️"


def _game_stats_item_stats(item: dict[str, Any]) -> dict[str, Any]:
    stats = item.get("stats")
    return stats if isinstance(stats, dict) else {}


def _period_stats_from_items(items: list[dict[str, Any]]) -> PeriodStats | None:
    if not items:
        return None

    kd_values: list[float] = []
    kills_values: list[float] = []
    first_kills_values: list[float] = []
    hs_values: list[float] = []
    opening_death_rates: list[float] = []
    early_impact_values: list[float] = []
    entry_wins_total = 0.0
    entry_count_total = 0.0
    clutch_wins_total = 0.0
    clutch_count_total = 0.0
    wins = 0
    played = 0

    for item in items:
        stats = _game_stats_item_stats(item)
        played += 1
        parsed = parse_map_skill_from_match(stats)
        if parsed:
            kd_values.append(parsed[0])
            kills_values.append(parsed[1])

        first_kills = _parse_optional_stat(stats, STAT_MATCH_FIRST_KILLS_KEYS) or 0.0
        first_kills_values.append(first_kills)

        hs = _parse_optional_stat(stats, STAT_HS_MATCH_KEYS)
        if hs is not None:
            hs_values.append(_as_percent(hs))

        entry = _parse_optional_stat(stats, STAT_TOTAL_ENTRY_COUNT_KEYS) or 0.0
        entry_wins = _parse_optional_stat(stats, STAT_TOTAL_ENTRY_WINS_KEYS) or 0.0
        entry_wins_total += entry_wins
        entry_count_total += entry

        v1_wins = _parse_optional_stat(stats, STAT_1V1_WINS_KEYS) or 0.0
        v1_count = _parse_optional_stat(stats, STAT_1V1_COUNT_KEYS) or 0.0
        v2_wins = _parse_optional_stat(stats, STAT_1V2_WINS_KEYS) or 0.0
        v2_count = _parse_optional_stat(stats, STAT_1V2_COUNT_KEYS) or 0.0
        clutch_wins_total += v1_wins + v2_wins
        clutch_count_total += v1_count + v2_count

        deaths = _parse_optional_stat(stats, STAT_MATCH_DEATHS_KEYS) or 0.0
        rounds = _parse_optional_stat(stats, STAT_MATCH_ROUNDS_KEYS) or 0.0
        if rounds > 0:
            failed_entry = max(0.0, entry - entry_wins)
            passive_opening = max(0.0, deaths - first_kills - entry_wins) * 0.35
            opening_death_rates.append((failed_entry + passive_opening) / rounds)

        early_impact_values.append(first_kills + entry_wins)

        won = parse_match_win(stats)
        if won:
            wins += 1

    if not kills_values:
        return None

    entry_win_rate: float | None = None
    if entry_count_total > 0:
        entry_win_rate = entry_wins_total / entry_count_total

    clutch_1vx_win_rate: float | None = None
    if clutch_count_total > 0:
        clutch_1vx_win_rate = clutch_wins_total / clutch_count_total

    return PeriodStats(
        kd=sum(kd_values) / len(kd_values),
        avg_kills=sum(kills_values) / len(kills_values),
        win_rate=wins / played * 100 if played else 0.0,
        wins=wins,
        played=played,
        first_kills_avg=sum(first_kills_values) / len(first_kills_values),
        hs_pct=sum(hs_values) / len(hs_values) if hs_values else 0.0,
        entry_win_rate=entry_win_rate,
        clutch_1vx_win_rate=clutch_1vx_win_rate,
        opening_death_rate=(
            sum(opening_death_rates) / len(opening_death_rates)
            if opening_death_rates
            else 0.0
        ),
        early_attack_impact=sum(early_impact_values) / len(early_impact_values),
    )


def _normalize_playstyle_token(value: str) -> str:
    return re.sub(r"[\s\-]+", "_", value.strip().lower())


def _playstyle_role_from_token(token: str) -> str | None:
    key = _normalize_playstyle_token(token)
    if key in FACEIT_PLAYSTYLE_ROLE_RU:
        return FACEIT_PLAYSTYLE_ROLE_RU[key]
    for alias, label in FACEIT_PLAYSTYLE_ROLE_RU.items():
        if alias in key or key in alias:
            return label
    return None


def _extract_playstyle_from_mapping(mapping: dict[str, Any]) -> str | None:
    for field in ("role", "playstyle", "main_role", "primary_role", "type", "name"):
        raw = mapping.get(field)
        if isinstance(raw, str):
            label = _playstyle_role_from_token(raw)
            if label:
                return label
    return None


def extract_faceit_playstyle_role(cs2_stats: dict[str, Any]) -> str | None:
    """Роль из /players/{id}/stats/cs2: player_skills, role, playstyle."""
    for key in ("role", "playstyle"):
        raw = cs2_stats.get(key)
        if isinstance(raw, str):
            label = _playstyle_role_from_token(raw)
            if label:
                return label

    player_skills = cs2_stats.get("player_skills")
    if isinstance(player_skills, dict):
        label = _extract_playstyle_from_mapping(player_skills)
        if label:
            return label
        for value in player_skills.values():
            if isinstance(value, dict):
                label = _extract_playstyle_from_mapping(value)
                if label:
                    return label
            elif isinstance(value, str):
                label = _playstyle_role_from_token(value)
                if label:
                    return label

    for segment in cs2_stats.get("segments") or []:
        seg_type = (segment.get("type") or "").lower()
        if seg_type not in ("role", "playstyle", "player_skills", "skills"):
            continue
        stats = segment.get("stats")
        if isinstance(stats, dict):
            label = _extract_playstyle_from_mapping(stats)
            if label:
                return label

    return None


def infer_playstyle_from_match_stats(items: list[dict[str, Any]]) -> str | None:
    """Эвристика по матчам, если роли в API нет."""
    if not items:
        return None

    first_kills_total = 0.0
    entry_total = 0.0
    entry_wins_total = 0.0
    sniper_total = 0.0
    flash_assists_total = 0.0
    clutch_wins = 0.0
    clutch_count = 0.0
    count = 0

    for item in items:
        stats = _game_stats_item_stats(item)
        count += 1
        first_kills_total += (
            _parse_optional_stat(stats, STAT_MATCH_FIRST_KILLS_KEYS) or 0.0
        )
        entry = _parse_optional_stat(stats, STAT_TOTAL_ENTRY_COUNT_KEYS) or 0.0
        entry_wins = _parse_optional_stat(stats, STAT_TOTAL_ENTRY_WINS_KEYS) or 0.0
        entry_total += entry
        entry_wins_total += entry_wins
        sniper_total += _parse_optional_stat(stats, STAT_SNIPER_KILLS_KEYS) or 0.0
        flash_assists_total += (
            _parse_optional_stat(stats, STAT_FLASH_ASSISTS_KEYS) or 0.0
        )
        clutch_wins += (_parse_optional_stat(stats, STAT_1V1_WINS_KEYS) or 0.0) + (
            _parse_optional_stat(stats, STAT_1V2_WINS_KEYS) or 0.0
        )
        clutch_count += (_parse_optional_stat(stats, STAT_1V1_COUNT_KEYS) or 0.0) + (
            _parse_optional_stat(stats, STAT_1V2_COUNT_KEYS) or 0.0
        )

    if count == 0:
        return None

    first_kills_avg = first_kills_total / count
    entry_avg = entry_total / count
    sniper_avg = sniper_total / count
    flash_assists_avg = flash_assists_total / count
    entry_win_rate = entry_wins_total / entry_total if entry_total > 0 else 0.0
    clutch_win_rate = clutch_wins / clutch_count if clutch_count >= 3 else 0.0

    scores: list[tuple[float, str]] = []
    if first_kills_avg >= 2.2 or (entry_avg >= 3.5 and entry_win_rate >= 0.48):
        scores.append((first_kills_avg * 2.0 + entry_win_rate * 5.0, "Первое касание"))
    if sniper_avg >= 4.0:
        scores.append((sniper_avg, "Снайпер"))
    if clutch_win_rate >= 0.42:
        scores.append((clutch_win_rate * 10.0, "Клатчер"))
    if flash_assists_avg >= 0.35 or (
        flash_assists_avg >= 0.2 and first_kills_avg < 1.8
    ):
        scores.append((flash_assists_avg * 8.0, "Поддержка"))

    if not scores:
        return None

    scores.sort(key=lambda item: item[0], reverse=True)
    best_score, best_label = scores[0]
    if len(scores) > 1 and scores[1][0] >= best_score * 0.85:
        return None
    if best_score < 2.5:
        return None
    return best_label


def compute_player_playstyle(
    cs2_stats: dict[str, Any], items: list[dict[str, Any]]
) -> str:
    api_role = extract_faceit_playstyle_role(cs2_stats)
    if api_role:
        return api_role
    inferred = infer_playstyle_from_match_stats(items)
    if inferred:
        return inferred
    return "Не определён"


def resolve_skill_level(player: dict[str, Any], current_elo: int | None) -> int:
    cs2_game = (player.get("games") or {}).get("cs2") or {}
    try:
        level = int(_parse_float(cs2_game.get("skill_level"), default=0))
    except (TypeError, ValueError):
        level = 0
    if 1 <= level <= 10:
        return level
    if current_elo is not None:
        elo_brackets = (500, 751, 901, 1051, 1201, 1351, 1531, 1751, 2001, 2251)
        for index, threshold in enumerate(elo_brackets, start=1):
            if current_elo < threshold:
                return index
        return 10
    return 5


def get_level_benchmarks(
    cs2_stats: dict[str, Any], skill_level: int
) -> dict[str, float]:
    """Бенчмарки уровня: из API (если есть) или таблица по skill level."""
    defaults = SKILL_LEVEL_BENCHMARKS.get(
        skill_level, SKILL_LEVEL_BENCHMARKS[5]
    ).copy()

    containers: list[dict[str, Any]] = []
    if isinstance(cs2_stats.get("player_skills"), dict):
        containers.append(cs2_stats["player_skills"])
    for segment in cs2_stats.get("segments") or []:
        seg_type = (segment.get("type") or "").lower()
        if seg_type in ("benchmark", "benchmarks", "peer", "level", "elo"):
            stats = segment.get("stats")
            if isinstance(stats, dict):
                containers.append(stats)

    key_map = {
        "kd": ("kd", "k/d", "average k/d ratio", "average_kd"),
        "avg_kills": ("avg", "avg_kills", "average kills", "kills"),
        "hs_pct": ("hs", "headshot", "hs_pct", "average headshots"),
        "win_rate": ("win", "win_rate", "win rate"),
    }

    for container in containers:
        for target, aliases in key_map.items():
            for alias in aliases:
                for key, value in container.items():
                    if alias not in str(key).lower():
                        continue
                    try:
                        parsed = _parse_float(value)
                    except (TypeError, ValueError):
                        continue
                    if target == "hs_pct":
                        parsed = _as_percent(parsed)
                    elif target == "win_rate":
                        parsed = _as_percent(parsed)
                    defaults[target] = parsed
                    break

    return defaults


def compute_form_score(
    recent: PeriodStats,
    benchmarks: dict[str, float],
) -> float:
    """Форма vs средние показатели игроков того же уровня Faceit."""
    score = 5.0
    beats = 0
    compared = 0

    kd_bench = benchmarks["kd"]
    compared += 1
    if recent.kd >= kd_bench * 1.2:
        score += 2.0
        beats += 1
    elif recent.kd <= kd_bench * 0.8:
        score -= 2.0

    avg_bench = benchmarks["avg_kills"]
    compared += 1
    if recent.avg_kills >= avg_bench * 1.2:
        score += 2.0
        beats += 1
    elif recent.avg_kills <= avg_bench * 0.8:
        score -= 2.0

    if recent.hs_pct > 0:
        compared += 1
        hs_bench = benchmarks["hs_pct"]
        if recent.hs_pct >= hs_bench * 1.15 or recent.hs_pct >= hs_bench + 4:
            score += 1.0
            beats += 1
        elif recent.hs_pct <= hs_bench * 0.85 and recent.hs_pct <= hs_bench - 4:
            score -= 1.0

    compared += 1
    wr_bench = benchmarks["win_rate"]
    if recent.win_rate >= wr_bench + 5:
        score += 1.0
        beats += 1
    elif recent.win_rate <= wr_bench - 5:
        score -= 1.0

    peer_ratio = beats / compared if compared else 0.5
    if peer_ratio >= 0.75:
        score = max(8.0, score)
    elif peer_ratio <= 0.25:
        score = min(4.0, score)
        score = max(2.0, score)
    else:
        score = max(4.5, min(7.0, score))

    return max(1.0, min(10.0, score))


def build_progress_changes(
    older: PeriodStats, recent: PeriodStats
) -> tuple[list[str], list[str]]:
    """Прогресс и просадка без винрейта матчей, с понятными формулировками."""
    improved: list[tuple[float, str]] = []
    declined: list[tuple[float, str]] = []

    avg_delta = recent.avg_kills - older.avg_kills
    if avg_delta >= 0.5:
        improved.append(
            (
                avg_delta,
                f"✅ Стал лучше размениваться: AVG вырос на {avg_delta:.1f} "
                "убийств за игру",
            )
        )
    elif avg_delta <= -0.5:
        declined.append(
            (
                -avg_delta,
                f"⚠️ Просадка: Размен: было {older.avg_kills:.1f} → "
                f"стало {recent.avg_kills:.1f} убийств за игру",
            )
        )

    if recent.hs_pct > 0 and older.hs_pct > 0:
        hs_delta = recent.hs_pct - older.hs_pct
        if hs_delta >= 3:
            improved.append(
                (
                    hs_delta,
                    f"✅ Улучшилась стрельба: HS% вырос на {hs_delta:.0f}%",
                )
            )
        elif hs_delta <= -3:
            declined.append(
                (
                    -hs_delta,
                    f"⚠️ Просадка: Стрельба: было {older.hs_pct:.0f}% → "
                    f"стало {recent.hs_pct:.0f}%",
                )
            )

    kd_delta = recent.kd - older.kd
    if kd_delta >= 0.08:
        improved.append(
            (
                kd_delta,
                f"✅ Улучшился K/D: вырос на {kd_delta:.2f} "
                f"({older.kd:.2f} → {recent.kd:.2f})",
            )
        )
    elif kd_delta <= -0.08:
        declined.append(
            (
                -kd_delta,
                f"⚠️ Просадка: K/D: было {older.kd:.2f} → стало {recent.kd:.2f}",
            )
        )

    if (
        recent.clutch_1vx_win_rate is not None
        and older.clutch_1vx_win_rate is not None
    ):
        old_pct = older.clutch_1vx_win_rate * 100
        new_pct = recent.clutch_1vx_win_rate * 100
        if new_pct - old_pct >= 8:
            improved.append(
                (
                    new_pct - old_pct,
                    f"✅ Улучшились клатчи: winrate в 1vX вырос на "
                    f"{new_pct - old_pct:.0f}%",
                )
            )
        elif old_pct - new_pct >= 8:
            declined.append(
                (
                    old_pct - new_pct,
                    f"⚠️ Просадка: Клатчи: winrate в 1vX упал с "
                    f"{old_pct:.0f}% → {new_pct:.0f}%",
                )
            )

    if recent.entry_win_rate is not None and older.entry_win_rate is not None:
        old_pct = older.entry_win_rate * 100
        new_pct = recent.entry_win_rate * 100
        if new_pct - old_pct >= 8:
            improved.append(
                (
                    new_pct - old_pct,
                    f"✅ Улучшилось первое касание: winrate вырос на "
                    f"{new_pct - old_pct:.0f}%",
                )
            )
        elif old_pct - new_pct >= 8:
            declined.append(
                (
                    old_pct - new_pct,
                    f"⚠️ Просадка: Первое касание: было {old_pct:.0f}% → "
                    f"стало {new_pct:.0f}%",
                )
            )

    fk_delta = recent.first_kills_avg - older.first_kills_avg
    if fk_delta >= 0.3:
        improved.append(
            (
                fk_delta,
                f"✅ Больше opening kills: +{fk_delta:.1f} за матч",
            )
        )
    elif fk_delta <= -0.3:
        declined.append(
            (
                -fk_delta,
                f"⚠️ Просадка: Opening kills: было {older.first_kills_avg:.1f} → "
                f"стало {recent.first_kills_avg:.1f}",
            )
        )

    improved.sort(key=lambda item: item[0], reverse=True)
    declined.sort(key=lambda item: item[0], reverse=True)
    return (
        [line for _, line in improved[:2]],
        [line for _, line in declined[:2]],
    )


def find_growth_zones(
    older: PeriodStats,
    recent: PeriodStats,
    lifetime_hs_pct: float | None,
) -> list[str]:
    """1–2 самые проблемные зоны роста по навыкам."""
    issues: list[tuple[float, str]] = []

    if (
        recent.clutch_1vx_win_rate is not None
        and older.clutch_1vx_win_rate is not None
    ):
        drop = (older.clutch_1vx_win_rate - recent.clutch_1vx_win_rate) * 100
        if drop >= 8:
            old_pct = older.clutch_1vx_win_rate * 100
            new_pct = recent.clutch_1vx_win_rate * 100
            issues.append(
                (
                    drop,
                    f"Клатчи: winrate в 1vX упал с {old_pct:.0f}% → {new_pct:.0f}%",
                )
            )
        elif recent.clutch_1vx_win_rate < 0.35:
            issues.append(
                (
                    35 - recent.clutch_1vx_win_rate * 100,
                    f"Клатчи: низкий winrate в 1vX ({recent.clutch_1vx_win_rate * 100:.0f}%)",
                )
            )

    opening_rise = (recent.opening_death_rate - older.opening_death_rate) * 100
    if opening_rise >= 5 or recent.opening_death_rate >= 0.22:
        severity = max(opening_rise, recent.opening_death_rate * 100 - 15)
        issues.append(
            (
                severity,
                "Позиционная игра: частые смерти в первые секунды раунда за CT",
            )
        )

    if recent.entry_win_rate is not None:
        if older.entry_win_rate is not None:
            entry_drop = (older.entry_win_rate - recent.entry_win_rate) * 100
            if entry_drop >= 8:
                old_pct = older.entry_win_rate * 100
                new_pct = recent.entry_win_rate * 100
                issues.append(
                    (
                        entry_drop,
                        f"Первое касание: было {old_pct:.0f}% → стало {new_pct:.0f}%",
                    )
                )
        if recent.entry_win_rate < 0.45:
            issues.append(
                (
                    45 - recent.entry_win_rate * 100,
                    f"Первое касание: низкий winrate ({recent.entry_win_rate * 100:.0f}%)",
                )
            )

    tempo_drop = older.early_attack_impact - recent.early_attack_impact
    if tempo_drop >= 0.4:
        issues.append(
            (
                tempo_drop * 10,
                "Темп игры: мало импакта в первые 30 сек раунда в атаке",
            )
        )

    benchmark_hs = lifetime_hs_pct if lifetime_hs_pct else 50.0
    if recent.hs_pct > 0 and recent.hs_pct < benchmark_hs - 8:
        issues.append(
            (
                benchmark_hs - recent.hs_pct,
                f"Стрельба: низкий HS% ({recent.hs_pct:.0f}% vs ~{benchmark_hs:.0f}% "
                "для твоего уровня)",
            )
        )

    if not issues:
        return []

    issues.sort(key=lambda item: item[0], reverse=True)
    return [message for _, message in issues[:2]]


def collect_player_game_stat_items(
    service: FaceitService, player_id: str, limit: int
) -> list[dict[str, Any]]:
    """Последние матчи из /players/{id}/games/cs2/stats (с пагинацией)."""
    collected: list[dict[str, Any]] = []
    offset = 0

    while len(collected) < limit:
        page_limit = min(100, limit - len(collected))
        payload = service.get_player_game_stats(
            player_id, offset=offset, limit=page_limit
        )
        batch = list(payload.get("items") or [])
        if not batch:
            break
        collected.extend(batch)
        offset += len(batch)
        if len(batch) < page_limit:
            break

    return collected[:limit]


def progress_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            f"📈 Последние {count}",
            callback_data=f"{PROGRESS_CALLBACK_PREFIX}{count}",
        )
        for count in PROGRESS_OPTIONS
    ]
    return InlineKeyboardMarkup([buttons])


def build_user_progress(
    service: FaceitService, nickname: str, requested_matches: int
) -> ProgressReport:
    player = service.lookup_player_by_nickname(nickname)
    player_id = player.get("player_id")
    if not player_id:
        raise ValueError(f"Игрок «{nickname}» не найден на Faceit.")

    cs2_stats = service.get_player_cs2_stats(player_id)
    service.get_player_history(player_id, limit=requested_matches)
    lifetime_hs_raw = _first_present(cs2_stats.get("lifetime") or {}, STAT_HS_KEYS)
    lifetime_hs_pct: float | None = None
    if lifetime_hs_raw is not None:
        try:
            lifetime_hs_pct = _as_percent(_parse_float(lifetime_hs_raw))
        except ValueError:
            lifetime_hs_pct = None

    items = collect_player_game_stat_items(service, player_id, requested_matches)
    available = len(items)

    if available < PROGRESS_MIN_MATCHES:
        raise ValueError(
            f"Недостаточно матчей для анализа (есть {available}, "
            f"нужно минимум {PROGRESS_MIN_MATCHES})."
        )

    partial_note: str | None = None
    if available < requested_matches:
        partial_note = (
            f"У тебя всего {available} матчей. Показаны все доступные данные."
        )

    half = available // 2
    recent_items = items[:half]
    older_items = items[half:available]

    recent = _period_stats_from_items(recent_items)
    older = _period_stats_from_items(older_items)
    if recent is None or older is None:
        raise ValueError("Не удалось собрать статистику из последних матчей.")

    cs2_game = (player.get("games") or {}).get("cs2") or {}
    current_elo_raw = cs2_game.get("faceit_elo")
    current_elo: int | None = None
    if current_elo_raw is not None:
        try:
            current_elo = int(_parse_float(current_elo_raw))
        except ValueError:
            current_elo = None

    elo_delta: int | None = None
    if current_elo is not None:
        elo_delta = (recent.wins - older.wins) * PROGRESS_ELO_PER_WIN

    skill_level = resolve_skill_level(player, current_elo)
    benchmarks = get_level_benchmarks(cs2_stats, skill_level)
    improved, declined = build_progress_changes(older, recent)
    form_score = compute_form_score(recent, benchmarks)
    style = compute_player_playstyle(cs2_stats, items)
    growth_zones = find_growth_zones(older, recent, lifetime_hs_pct)

    return ProgressReport(
        style=style,
        form_score=form_score,
        older=older,
        recent=recent,
        current_elo=current_elo,
        elo_delta=elo_delta,
        improved=improved,
        declined=declined,
        growth_zones=growth_zones,
        requested_count=requested_matches,
        match_count=available,
        partial_note=partial_note,
    )


def format_progress_report(report: ProgressReport) -> str:
    lines = [
        f"📊 Анализ за последние {report.requested_count} матчей",
    ]
    if report.partial_note:
        lines.append(report.partial_note)
    lines.extend(
        [
            "",
            "📈 ТВОЙ ПРОГРЕСС",
            "",
            f"🏅 Стиль: {report.style} | Форма: {report.form_score:.1f}/10",
            "",
            f"📊 Последние {report.match_count} матчей:",
            (
                f"K/D {_trend_arrow(report.older.kd, report.recent.kd)} "
                f"{report.older.kd:.2f} → {report.recent.kd:.2f}"
            ),
            (
                f"AVG {_trend_arrow(report.older.avg_kills, report.recent.avg_kills)} "
                f"{report.older.avg_kills:.0f} → {report.recent.avg_kills:.0f}"
            ),
        ]
    )

    if report.elo_delta is not None:
        arrow = _trend_arrow(0, float(report.elo_delta))
        sign = "+" if report.elo_delta > 0 else ""
        lines.append(f"ELO {arrow} {sign}{report.elo_delta}")
    elif report.current_elo is not None:
        lines.append(f"ELO ➡️ {report.current_elo} (текущий)")

    lines.extend(["", "🔥 Прогресс:"])
    if report.improved:
        lines.extend(report.improved)
    else:
        lines.append("✅ Заметного прогресса не найдено")

    lines.append("")
    if report.declined:
        lines.extend(report.declined)
    else:
        lines.append("⚠️ Просадок не найдено")

    lines.extend(["", "🎯 ЗОНА РОСТА:"])
    if report.growth_zones:
        for zone in report.growth_zones:
            lines.append(f"• {zone}")
    else:
        lines.append("• — явных слабых зон не найдено")

    return "\n".join(lines)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    purge_inactive_users()
    touch_user(update.effective_user.id)
    await update.message.reply_text(
        "Привет! Я анализирую матчи Faceit CS2.\n\n"
        "1. Задай свой ник: /setnick ТвойНик\n"
        "2. Отправь ссылку на матч — проанализирую команду соперников.\n"
        "3. /progress — прогресс за 15, 50 или 100 матчей (кнопки).\n\n"
        "Сравнение по K/D и AVG на выбранной карте. "
        "Позиция — по роли/heatmap Faceit; если данных нет — «?». "
        "На Dust2 и Overpass позиции не показываются."
    )


async def setnick_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not context.args:
        await update.message.reply_text(
            "Укажи ник: /setnick ТвойНикFaceit\n"
            "Пример: /setnick s1mple"
        )
        return

    nickname = " ".join(context.args).strip()
    if not nickname:
        await update.message.reply_text("Ник не может быть пустым.")
        return

    user_id = update.effective_user.id
    purge_inactive_users()
    user_nicks[user_id] = nickname
    touch_user(user_id)
    await update.message.reply_text(
        f"✅ Ник сохранён: {nickname}\n"
        "Теперь отправь ссылку на матч Faceit CS2 или /progress."
    )


async def progress_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    if not FACEIT_API_KEY:
        await update.message.reply_text("❌ Не задан FACEIT_API_KEY в bot.py.")
        return

    user_id = update.effective_user.id
    purge_inactive_users()
    touch_user(user_id)

    if not user_nicks.get(user_id):
        await update.message.reply_text(
            "❌ Сначала укажи свой Faceit-ник командой:\n"
            "/setnick ТвойНик"
        )
        return

    await update.message.reply_text(
        "Выбери период для анализа прогресса:",
        reply_markup=progress_keyboard(),
    )


async def progress_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not query.from_user:
        return

    if not query.data.startswith(PROGRESS_CALLBACK_PREFIX):
        return

    try:
        requested_matches = int(query.data.removeprefix(PROGRESS_CALLBACK_PREFIX))
    except ValueError:
        await query.answer("Неверный выбор", show_alert=True)
        return

    if requested_matches not in PROGRESS_OPTIONS:
        await query.answer("Неверный период", show_alert=True)
        return

    user_id = query.from_user.id
    purge_inactive_users()
    touch_user(user_id)

    user_nickname = user_nicks.get(user_id)
    if not user_nickname:
        await query.answer("Сначала укажи ник: /setnick", show_alert=True)
        return

    await query.answer()
    await query.edit_message_text("⏳ Считаю прогресс...")

    def run_progress() -> str:
        service = FaceitService(FACEIT_API_KEY)
        report = build_user_progress(service, user_nickname, requested_matches)
        return format_progress_report(report)

    try:
        text = await asyncio.to_thread(run_progress)
        await query.edit_message_text(text)
    except FaceitNotFoundError:
        await query.edit_message_text(
            "❌ Игрок не найден на Faceit. Проверь ник командой /setnick."
        )
    except ConnectionError as exc:
        await query.edit_message_text(f"❌ {exc}")
    except FaceitAPIError as exc:
        code = f" [{exc.status_code}]" if exc.status_code else ""
        await query.edit_message_text(f"❌ Ошибка Faceit API{code}: {exc.message}")
    except ValueError as exc:
        await query.edit_message_text(f"❌ {exc}")
    except Exception:
        logger.exception("Неожиданная ошибка при расчёте прогресса")
        await query.edit_message_text(
            "❌ Произошла непредвиденная ошибка. Попробуйте позже."
        )


async def handle_match_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text or not update.effective_user:
        return

    if not FACEIT_API_KEY:
        await update.message.reply_text("❌ Не задан FACEIT_API_KEY в bot.py.")
        return

    user_id = update.effective_user.id
    purge_inactive_users()
    touch_user(user_id)

    user_nickname = user_nicks.get(user_id)
    if not user_nickname:
        await update.message.reply_text(
            "❌ Сначала укажи свой Faceit-ник командой:\n"
            "/setnick ТвойНик"
        )
        return

    match_id = extract_match_id(update.message.text)
    if not match_id:
        await update.message.reply_text(
            "❌ Неверная ссылка. Пришлите URL матча Faceit CS2 "
            "(faceit.com/.../cs2/room/1-...)."
        )
        return

    status_message = await update.message.reply_text("⏳ Анализирую матч...")

    def run_analysis() -> str:
        service = FaceitService(FACEIT_API_KEY)
        result = analyze_opponents(service, match_id, user_nickname)
        return build_report(result)

    try:
        report = await asyncio.to_thread(run_analysis)
        await status_message.edit_text(report)
    except FaceitNotFoundError:
        await status_message.edit_text(
            "❌ Матч не найден. Проверьте ссылку и что матч уже создан на Faceit."
        )
    except ConnectionError as exc:
        await status_message.edit_text(f"❌ {exc}")
    except FaceitAPIError as exc:
        code = f" [{exc.status_code}]" if exc.status_code else ""
        await status_message.edit_text(f"❌ Ошибка Faceit API{code}: {exc.message}")
    except ValueError as exc:
        await status_message.edit_text(f"❌ {exc}")
    except Exception:
        logger.exception("Неожиданная ошибка при анализе матча")
        await status_message.edit_text(
            "❌ Произошла непредвиденная ошибка. Попробуйте позже."
        )


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        logger.debug("Health %s - %s", self.address_string(), format % args)


def run_health_server() -> None:
    server = HTTPServer(("0.0.0.0", HEALTH_PORT), HealthHandler)
    logger.info("HTTP health check на порту %s (GET / → OK)", HEALTH_PORT)
    server.serve_forever()


def build_application() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("setnick", setnick_command))
    application.add_handler(CommandHandler("progress", progress_command))
    application.add_handler(CallbackQueryHandler(progress_callback))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_match_link)
    )
    return application


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Укажите BOT_TOKEN в начале файла bot.py")

    health_thread = threading.Thread(
        target=run_health_server,
        name="health-http",
        daemon=True,
    )
    health_thread.start()

    application = build_application()
    logger.info("Бот запущен (long polling)")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
