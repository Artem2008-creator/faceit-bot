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
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

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
SIDE_STYLE_MATCHES_LIMIT = 8
MIN_SIDE_STYLE_MATCHES = 3

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

# Роль в матче → типичная точка на карте (CT-удержание)
ROLE_SITE_BY_MAP: dict[str, dict[str, str]] = {
    "de_mirage": {
        "anchor": "B",
        "support": "B",
        "entry": "A",
        "lurker": "A",
        "igl": "Mid",
        "awper": "Mid",
        "awp": "Mid",
        "rifler": "Mid",
    },
    "de_dust2": {
        "anchor": "B",
        "support": "B",
        "entry": "B",
        "lurker": "A",
        "igl": "Mid",
        "awper": "A",
        "awp": "A",
        "rifler": "Mid",
    },
    "de_inferno": {
        "anchor": "B",
        "support": "B",
        "entry": "A",
        "lurker": "Mid",
        "igl": "Mid",
        "awper": "A",
        "awp": "A",
        "rifler": "Mid",
    },
    "de_nuke": {
        "anchor": "B",
        "support": "B",
        "entry": "A",
        "lurker": "Mid",
        "igl": "Mid",
        "awper": "A",
        "awp": "A",
        "rifler": "Mid",
    },
    "de_overpass": {
        "anchor": "B",
        "support": "B",
        "entry": "A",
        "lurker": "B",
        "igl": "Mid",
        "awper": "A",
        "awp": "A",
        "rifler": "Mid",
    },
    "de_ancient": {
        "anchor": "B",
        "support": "B",
        "entry": "A",
        "lurker": "B",
        "igl": "Mid",
        "awper": "Mid",
        "awp": "Mid",
        "rifler": "Mid",
    },
    "de_anubis": {
        "anchor": "B",
        "support": "B",
        "entry": "A",
        "lurker": "B",
        "igl": "Mid",
        "awper": "Mid",
        "awp": "Mid",
        "rifler": "Mid",
    },
    "de_vertigo": {
        "anchor": "B",
        "support": "B",
        "entry": "A",
        "lurker": "A",
        "igl": "Mid",
        "awper": "A",
        "awp": "A",
        "rifler": "Mid",
    },
    "de_cache": {
        "anchor": "B",
        "support": "B",
        "entry": "A",
        "lurker": "Mid",
        "igl": "Mid",
        "awper": "Mid",
        "awp": "Mid",
        "rifler": "Mid",
    },
    "de_train": {
        "anchor": "B",
        "support": "B",
        "entry": "A",
        "lurker": "B",
        "igl": "Mid",
        "awper": "A",
        "awp": "A",
        "rifler": "Mid",
    },
}

DEFAULT_ROLE_SITE = {
    "anchor": "B",
    "support": "B",
    "entry": "A",
    "lurker": "A",
    "igl": "Mid",
    "awper": "Mid",
    "awp": "Mid",
    "rifler": "Mid",
}

FALLBACK_SITES = ("B", "A", "Mid", "B", "A")

STAT_KD_KEYS = ("Average K/D Ratio", "K/D Ratio", "Average K/D", "kd_ratio")
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
STAT_PLANTS_KEYS = ("Bomb Plants", "Plants")
STAT_DEFUSES_KEYS = ("Bomb Defuses", "Defuses")
STAT_PISTOL_KILLS_KEYS = ("Pistol Kills",)
STAT_UTILITY_PER_ROUND_KEYS = ("Utility Usage per Round",)
STAT_FLASHES_PER_ROUND_KEYS = ("Flashes per Round in a Match", "Flashes per Round")

# Человекочитаемые названия зон для паттернов CT/T
MAP_ZONE_LABELS: dict[str, dict[str, str]] = {
    "de_mirage": {"A": "A", "B": "banana", "Mid": "mid"},
    "de_dust2": {"A": "Long/A", "B": "B", "Mid": "mid"},
    "de_inferno": {"A": "A", "B": "banana", "Mid": "mid"},
    "de_nuke": {"A": "A/yard", "B": "B/ramp", "Mid": "mid"},
    "de_overpass": {"A": "A", "B": "B", "Mid": "mid"},
    "de_ancient": {"A": "A", "B": "B", "Mid": "mid"},
    "de_anubis": {"A": "A", "B": "B", "Mid": "mid"},
    "de_vertigo": {"A": "A", "B": "B", "Mid": "mid"},
    "de_cache": {"A": "A", "B": "B", "Mid": "mid"},
    "de_train": {"A": "A", "B": "B", "Mid": "mid"},
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
    adr: float
    hs_pct: float


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
    user_win_rate: float
    verdict: str


@dataclass
class MapPoolAnalysis:
    recommendations: list[MapRecommendation]


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
class AnalysisResult:
    map_name: str
    map_key: str
    players: list[AnalyzedPlayer]
    team_avg: MapSkill
    weakest: AnalyzedPlayer
    strongest: AnalyzedPlayer
    map_pool: MapPoolAnalysis | None
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


def parse_map_skill(stats: dict[str, Any]) -> MapSkill | None:
    kd_raw = _first_present(stats, STAT_KD_KEYS)
    adr_raw = _first_present(stats, STAT_ADR_KEYS)
    hs_raw = _first_present(stats, STAT_HS_KEYS)

    if kd_raw is None and adr_raw is None and hs_raw is None:
        return None

    try:
        kd = _parse_float(kd_raw, default=0.0) if kd_raw is not None else 0.0
        adr = _parse_float(adr_raw, default=0.0) if adr_raw is not None else 0.0
        hs = _parse_float(hs_raw, default=0.0) if hs_raw is not None else 0.0
    except ValueError:
        return None

    return MapSkill(kd_ratio=kd, adr=adr, hs_pct=hs)


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


def average_map_skill(match_stats_list: list[dict[str, Any]]) -> MapSkill | None:
    skills: list[MapSkill] = []
    for stats in match_stats_list:
        skill = parse_map_skill(stats)
        if skill:
            skills.append(skill)
    if not skills:
        return None
    count = len(skills)
    return MapSkill(
        kd_ratio=sum(s.kd_ratio for s in skills) / count,
        adr=sum(s.adr for s in skills) / count,
        hs_pct=sum(s.hs_pct for s in skills) / count,
    )


def get_player_map_skill(service: FaceitService, player_id: str, map_key: str) -> MapSkill:
    recent = collect_recent_map_match_stats(service, player_id, map_key)
    skill = average_map_skill(recent)
    if skill and len(recent) >= MIN_MAP_MATCHES_FOR_SKILL:
        return skill

    stats = service.get_player_cs2_stats(player_id)
    segment_stats = get_map_segment_stats(stats, map_key)
    skill = parse_map_skill(segment_stats)
    if skill is None:
        raise ValueError(
            f"Нет статистики на карте {format_map_name(map_key)} "
            f"(нужны недавние матчи на этой карте)"
        )
    return skill


def role_to_site(roles: list[str] | None, map_key: str) -> str | None:
    if not roles:
        return None
    mapping = ROLE_SITE_BY_MAP.get(map_key, DEFAULT_ROLE_SITE)
    for role in roles:
        if not isinstance(role, str):
            continue
        site = mapping.get(role.strip().lower())
        if site:
            return site
    return None


def extract_player_match_stats(
    match_stats: dict[str, Any], player_id: str
) -> dict[str, Any]:
    for round_data in match_stats.get("rounds") or []:
        for team in round_data.get("teams") or []:
            for player in team.get("players") or []:
                if player.get("player_id") == player_id:
                    return player.get("player_stats") or {}
    return {}


def infer_site_from_recent_map_matches(
    service: FaceitService, player_id: str, map_key: str
) -> str | None:
    """
    Прокси heatmap: по последним матчам на этой карте оцениваем,
    где игрок чаще «сидит» в защите (plants/defuses → B, entry → A).
    """
    per_match = collect_recent_map_match_stats(service, player_id, map_key)
    if not per_match:
        return None

    zone_scores = {"A": 0.0, "B": 0.0, "Mid": 0.0}
    for pstats in per_match:
        entry = _parse_float(
            _first_present(
                pstats,
                ("Entry Count", "First Kills", "Entry Wins", "Entry Kills"),
            ),
            default=0.0,
        )
        plants = _parse_float(
            _first_present(pstats, ("Bomb Plants", "Plants")),
            default=0.0,
        )
        defuses = _parse_float(
            _first_present(pstats, ("Bomb Defuses", "Defuses")),
            default=0.0,
        )
        damage = _parse_float(
            _first_present(pstats, ("Damage", "ADR", "Average Damage / Round")),
            default=0.0,
        )

        zone_scores["B"] += plants * 3.0 + defuses * 2.5 + damage * 0.005
        zone_scores["A"] += entry * 4.0
        zone_scores["Mid"] += damage * 0.01 - entry * 0.5

    return max(zone_scores, key=zone_scores.get)


def detect_player_site(
    service: FaceitService,
    member: dict[str, Any],
    map_key: str,
    roster_index: int,
) -> tuple[str, str]:
    roles = member.get("roles")
    if isinstance(roles, str):
        roles = [roles]
    site = role_to_site(roles if isinstance(roles, list) else None, map_key)
    if site:
        return site, "роль"

    site = infer_site_from_recent_map_matches(
        service, member.get("player_id") or "", map_key
    )
    if site:
        return site, "heatmap"

    return FALLBACK_SITES[roster_index % len(FALLBACK_SITES)], "оценка"


def skill_sort_key(player: AnalyzedPlayer) -> tuple[float, float, float]:
    s = player.skill
    return (s.kd_ratio, s.adr, s.hs_pct)


def average_team_skill(players: list[AnalyzedPlayer]) -> MapSkill:
    count = len(players)
    return MapSkill(
        kd_ratio=sum(p.skill.kd_ratio for p in players) / count,
        adr=sum(p.skill.adr for p in players) / count,
        hs_pct=sum(p.skill.hs_pct for p in players) / count,
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
    opponent_win_rate: float, user_win_rate: float
) -> tuple[str, str]:
    gap = user_win_rate - opponent_win_rate

    if opponent_win_rate >= 60 and user_win_rate <= 40:
        return "ban", "100% бан"
    if opponent_win_rate >= 55 and user_win_rate <= 45 and gap <= -10:
        return "ban", "100% бан"

    if opponent_win_rate <= 30 and user_win_rate >= 55:
        return "pick", "уверенный пик"
    if opponent_win_rate <= 40 and user_win_rate >= 50 and gap >= 15:
        return "pick", "уверенный пик"

    return "decider", "50/50, зависит от формы"


def build_map_recommendations(
    opponent_pool: dict[str, dict[str, int]],
    user_pool: dict[str, dict[str, int]],
) -> list[MapRecommendation]:
    recommendations: list[MapRecommendation] = []

    for map_key, opp_data in opponent_pool.items():
        user_data = user_pool.get(map_key)
        if not user_data:
            continue
        if (
            opp_data["played"] < MIN_MAP_MATCHES_FOR_REC
            or user_data["played"] < MIN_MAP_MATCHES_FOR_REC
        ):
            continue

        opponent_win_rate = opp_data["wins"] / opp_data["played"] * 100
        user_win_rate = user_data["wins"] / user_data["played"] * 100
        action, verdict = classify_map_recommendation(
            opponent_win_rate, user_win_rate
        )
        recommendations.append(
            MapRecommendation(
                map_key=map_key,
                display_name=format_map_name(map_key),
                action=action,
                opponent_win_rate=opponent_win_rate,
                user_win_rate=user_win_rate,
                verdict=verdict,
            )
        )

    action_order = {"ban": 0, "pick": 1, "decider": 2}
    return sorted(
        recommendations,
        key=lambda rec: (
            action_order.get(rec.action, 3),
            -abs(rec.user_win_rate - rec.opponent_win_rate),
            rec.display_name,
        ),
    )


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
    recommendations = build_map_recommendations(opponent_pool, user_pool)

    if not recommendations:
        return None
    return MapPoolAnalysis(recommendations=recommendations)


def format_map_recommendations(analysis: MapPoolAnalysis) -> list[str]:
    action_labels = {
        "ban": "❌ BAN",
        "pick": "✅ PICK",
        "decider": "⚠️ DECIDER",
    }
    lines = ["", "🗺 Карты:"]

    for rec in analysis.recommendations:
        label = action_labels.get(rec.action, rec.action.upper())
        lines.extend(
            [
                f"{label} {rec.display_name}",
                f"— винрейт соперника: {rec.opponent_win_rate:.0f}%",
                f"— твой винрейт: {rec.user_win_rate:.0f}%",
                f"— вердикт: {rec.verdict}",
                "",
            ]
        )

    if lines[-1] == "":
        lines.pop()
    return lines


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

    player_sites = {player.player_id: player.site for player in players}
    site_counts = Counter(player.site for player in players)
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
    zone_pressure = {"A": 0.0, "B": 0.0, "Mid": 0.0}
    ct_push_by_site = {"A": 0.0, "B": 0.0, "Mid": 0.0}

    for bundle in bundles:
        rounds = float(bundle["rounds"])
        if rounds <= 0:
            continue
        total_rounds += rounds

        for player_id, stats in bundle["players"]:
            site = player_sites.get(player_id, "Mid")
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
        (ct_push_by_site.get(ct_push_site or "Mid", 0.0) / total_rounds) * 120.0,
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
    players: list[AnalyzedPlayer], team_avg: MapSkill
) -> tuple[str, AnalyzedPlayer]:
    """Точка для атаки и слабый якорь на ней."""
    by_site: dict[str, list[AnalyzedPlayer]] = {}
    for player in players:
        by_site.setdefault(player.site, []).append(player)

    avg_key = (team_avg.kd_ratio, team_avg.adr, team_avg.hs_pct)

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
    players: list[AnalyzedPlayer], team_avg: MapSkill
) -> AttackAdvice | None:
    if not players:
        return None

    target_site, anchor = pick_attack_advice(players, team_avg)
    stats = anchor.defense or AnchorDefenseStats(None, None, None, None)
    return AttackAdvice(
        site=target_site,
        anchor=anchor,
        stats=stats,
        verdict=generate_attack_verdict(stats),
    )


def format_attack_advice(advice: AttackAdvice) -> list[str]:
    stats = advice.stats
    lines = [f"🎯 ДАВИТЕ ТОЧКУ {advice.site}:"]

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

    for index, member in enumerate(roster):
        player_id = member.get("player_id")
        nickname = member.get("nickname") or "unknown"
        if not player_id:
            continue
        player_ids.append(player_id)
        try:
            skill = get_player_map_skill(service, player_id, map_key)
            site, site_source = detect_player_site(service, member, map_key, index)
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
    attack_advice = build_attack_advice(analyzed, team_avg)
    playstyle = analyze_team_playstyle(service, player_ids, analyzed, map_key)

    return AnalysisResult(
        map_name=map_name,
        map_key=map_key,
        players=analyzed,
        team_avg=team_avg,
        weakest=weakest,
        strongest=strongest,
        map_pool=map_pool,
        attack_advice=attack_advice,
        playstyle=playstyle,
    )


def build_report(result: AnalysisResult) -> str:
    avg = result.team_avg
    lines = [
        f"🔍 Карта: {result.map_name}",
        "",
        f"📊 Среднее по команде: K/D {avg.kd_ratio:.2f} | ADR {avg.adr:.0f} | "
        f"HS% {avg.hs_pct:.0f}%",
        "",
        "👥 Команда соперника:",
    ]

    for player in result.players:
        skill = player.skill
        emoji = player_tier_emoji(player, result.weakest, result.strongest)
        lines.append(
            f"{emoji} {player.nickname} — {player.site} "
            f"(K/D: {skill.kd_ratio:.2f}, ADR: {skill.adr:.0f}, HS%: {skill.hs_pct:.0f}%)"
        )

    if result.attack_advice:
        lines.extend([""] + format_attack_advice(result.attack_advice))

    if result.playstyle and (result.playstyle.ct or result.playstyle.t):
        lines.extend([""] + format_playstyle_analysis(result.playstyle))

    if result.map_pool and result.map_pool.recommendations:
        lines.extend(format_map_recommendations(result.map_pool))

    return "\n".join(lines)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    purge_inactive_users()
    touch_user(update.effective_user.id)
    await update.message.reply_text(
        "Привет! Я анализирую матчи Faceit CS2.\n\n"
        "1. Задай свой ник: /setnick ТвойНик\n"
        "2. Отправь ссылку на матч — проанализирую команду соперников.\n\n"
        "Сравнение по K/D, ADR и HS% на выбранной карте. "
        "Точка игрока — по роли в матче или по heatmap (последние игры на карте)."
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
        "Теперь отправь ссылку на матч Faceit CS2."
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
