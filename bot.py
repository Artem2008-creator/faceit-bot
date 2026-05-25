"""
Telegram-бот: анализ слабого игрока в матче Faceit CS2 по ссылке на матч.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from user_nicks import UserNickStore

# --- Настройки (подставьте свои значения) ---
BOT_TOKEN = "8991957878:AAFbRWDYwMCZhL3PSkVbVSGJ-VQF6rNWn60"
FACEIT_API_KEY = "3fc0c069-7c26-46bb-a478-bed79cb95894"

FACEIT_API_BASE = "https://open.faceit.com/data/v4"
REQUEST_TIMEOUT = 20
HEALTH_PORT = 10000
RECENT_MAP_MATCHES_LIMIT = 15
GAME_STATS_FETCH_LIMIT = 40
MIN_MAP_MATCHES_FOR_SKILL = 3

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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

user_nick_store = UserNickStore()


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
class AnalyzedPlayer:
    nickname: str
    player_id: str
    skill: MapSkill
    site: str
    site_source: str


class FaceitService:
    def __init__(self, api_key: str) -> None:
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._match_stats_cache: dict[str, dict[str, Any]] = {}

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
        return self._request("GET", f"/players/{player_id}/stats/cs2")

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


def analyze_opponents(
    service: FaceitService,
    match_id: str,
    user_nickname: str,
) -> tuple[str, AnalyzedPlayer, AnalyzedPlayer]:
    match = service.get_match(match_id)
    map_key = resolve_match_map(service, match_id, match)
    map_name = format_map_name(map_key)
    roster = get_opponent_roster(match, user_nickname)

    analyzed: list[AnalyzedPlayer] = []
    for index, member in enumerate(roster):
        player_id = member.get("player_id")
        nickname = member.get("nickname") or "unknown"
        if not player_id:
            continue
        try:
            skill = get_player_map_skill(service, player_id, map_key)
            site, site_source = detect_player_site(service, member, map_key, index)
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
            )
        )

    if not analyzed:
        raise ValueError(
            "Не удалось получить статистику соперников на этой карте. "
            "Возможно, у них мало игр на ней."
        )

    weakest = min(analyzed, key=skill_sort_key)
    strongest = max(analyzed, key=skill_sort_key)
    return map_name, weakest, strongest


def build_report(
    map_name: str,
    weakest: AnalyzedPlayer,
    strongest: AnalyzedPlayer,
) -> str:
    ws, ss = weakest.skill, strongest.skill
    return (
        f"🔍 Карта: {map_name}\n"
        f"🛑 Самый слабый: {weakest.nickname} "
        f"(K/D: {ws.kd_ratio:.2f}, ADR: {ws.adr:.0f}, HS%: {ws.hs_pct:.0f}%) "
        f"— точка {weakest.site}\n"
        f"🟢 Самый сильный: {strongest.nickname} "
        f"(K/D: {ss.kd_ratio:.2f}, ADR: {ss.adr:.0f}, HS%: {ss.hs_pct:.0f}%) "
        f"— точка {strongest.site}\n"
        f"🎯 Совет: Атакуй точку {weakest.site} (слабый: {weakest.nickname})."
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
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

    user_nick_store.set(update.effective_user.id, nickname)
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

    user_nickname = user_nick_store.get(update.effective_user.id)
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
        map_name, weakest, strongest = analyze_opponents(
            service, match_id, user_nickname
        )
        return build_report(map_name, weakest, strongest)

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
