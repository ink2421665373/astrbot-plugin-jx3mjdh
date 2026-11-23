import json
import re
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .cards import Card, Table
from .data import ApiError, DataClient, field, match_document, percent, payload
from .labels import heart_icon, heart_label, label_parts, labeled, lineup_label
from .query import REVIEW_SUMMARY, ChoiceMenu, HistoryFilter, QueryError, QueryTarget
from .replays import ReplayView, parse_replay, replay_card

RESULT_TONES = {
    "胜": "win", "胜利": "win", "win": "win",
    "负": "loss", "败": "loss", "失败": "loss", "lose": "loss", "loss": "loss",
    "平": "draw", "平局": "draw", "draw": "draw",
}


@dataclass(frozen=True)
class RecentMatch:
    id: str
    time: str
    duration: str
    score: str

    def label(self) -> str:
        return f"{self.time} · {self.duration} · {self.score}"


@dataclass(frozen=True)
class Role:
    name: str
    server: str
    mode: str
    role_id: str = ""
    global_id: str = ""
    person_id: str = ""
    zone: str = ""


@dataclass(frozen=True)
class MatchQuery:
    mode: str
    match: RecentMatch
    player: tuple[int, int] | None = None
    replay: bool = False
    arena: str = ""


@dataclass(frozen=True)
class RecordQuery:
    origin: Role | tuple
    filters: HistoryFilter = HistoryFilter()
    cursor: int = 0
    offset: int = 0
    anchor: RecentMatch | None = None
    shown: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RoleLineups:
    role: Role
    school: str
    matches: tuple[RecentMatch, ...]


@dataclass
class RoleProfile:
    role: Role
    document: dict
    profile: dict

    @property
    def heart(self) -> str:
        return self.profile.get("kungfu") or self.profile.get("career") or ""


def match_samples(ids, mode: str = "3v3") -> ChoiceMenu | None:
    valid = tuple(dict.fromkeys(str(value) for value in ids if re.fullmatch(r"[0-9]{1,24}", str(value))))
    if not valid:
        return None
    return ChoiceMenu("请选择一场实战对局。", "对局详情", tuple(
        (f"样例对局 {match_id}", MatchQuery(mode, RecentMatch(match_id, "—", "—", "—"))) for match_id in valid
    ))


def role_menu(entries, mode: str, request: RecordQuery, action: str) -> ChoiceMenu:
    options = {}
    for entry in entries:
        role = Role(
            entry.get("roleName") or entry.get("name") or "",
            entry.get("server") or "", mode,
            field(entry, "roleid", "") or field(entry, "roleId", ""),
            field(entry, "globalRoleId", "") or field(entry, "global_role_id", ""),
            field(entry, "personId", ""), field(entry, "zone", ""),
        )
        if role.name and role.server:
            heart = field(entry, "kungfu", "") or field(entry, "career", "")
            options[role] = label_parts(labeled(role.name, heart), f" · {role.server}",
                                        " · " if heart else "", heart_label(heart) if heart else "")
    if not options:
        raise QueryError("原站没有返回可选择的角色。请核对区服和角色名。")
    return ChoiceMenu("请选择要查询的角色。", action, tuple(
        (label, role if request == RecordQuery(request.origin) else replace(request, origin=role))
        for role, label in options.items()
    ))


def resolved_role(role: Role, document: dict, profile: dict) -> Role:
    identity = document.get("resolvedRole") or {}
    fields = {}
    for attr, source in (
        ("role_id", "roleid"), ("global_id", "global_role_id"), ("person_id", "personId"), ("zone", "zone"),
    ):
        fields[attr] = getattr(role, attr) or field(identity, source, "") or field(profile, source, "")
    return replace(role, name=identity.get("roleName") or profile.get("name") or role.name,
                   server=identity.get("server") or profile.get("server") or role.server, **fields)


def record_body(role: Role, cursor: int = 0) -> dict:
    fields = {"roleName": role.name, "server": role.server, "mode": role.mode, "cursor": cursor}
    fields.update({key: value for key, value in (
        ("globalRoleId", role.global_id), ("roleid", role.role_id),
        ("personId", role.person_id), ("zone", role.zone),
    ) if value})
    return fields


async def recover_recent(client: DataClient, role: Role) -> dict:
    retry = {**record_body(role), "onlyMatches": True}
    if not retry.get("globalRoleId") and not (retry.get("roleid") and retry["server"]):
        return {}
    try:
        result = await client.get("player/home-page", body=retry, ttl=240)
    except QueryError:
        return {}
    if result.get("success") is False or result.get("error"):
        return {}
    return result


async def resolve_profile(client: DataClient, request: RecordQuery, action: str) -> RoleProfile | ChoiceMenu:
    origin = request.origin
    if isinstance(origin, Role):
        role = origin
    else:
        server, name, mode = origin
        role = Role(name, "" if server == "全服搜索" else server, mode)
        if name.isdecimal() and 4 <= len(name) <= 24:
            lookup = payload(await client.get("player/person-num", body={"personNum": name}))
            accounts = lookup.get("accounts", [])
            return role_menu(
                ({**entry, "personId": account.get("personId")} for account in accounts
                 for entry in account.get("roles", []) if not role.server or entry.get("server") == role.server),
                role.mode, request, action,
            )
    try:
        document = await client.get("player/home-page", body=record_body(role), ttl=240)
    except ApiError as exc:
        if exc.status not in (400, 404, 409, 422) or not exc.details:
            raise
        document = exc.details
    if document.get("candidates"):
        return role_menu(document["candidates"], role.mode, request, action)
    if document.get("renamed") and document.get("newName"):
        return role_menu(({
            "roleName": document["newName"], "server": document.get("newServer") or role.server,
            "globalRoleId": document.get("globalRoleId") or document.get("global_role_id"),
        },), role.mode, request, action)
    profile = document.get("profile")
    if not isinstance(profile, dict):
        raise QueryError("原站没有返回角色详情。请核对区服和完整角色名，或用推栏 ID 查询。")
    return RoleProfile(resolved_role(role, document, profile), document, profile)


async def record(client: DataClient, origin: Role | tuple | RecordQuery) -> Card | ChoiceMenu:
    request = origin if isinstance(origin, RecordQuery) else RecordQuery(origin)
    result = await resolve_profile(client, request, "战绩")
    if isinstance(result, ChoiceMenu):
        return result
    profile, resolved, document = result.profile, result.role, result.document
    heart = result.heart
    name = field(profile, "name", resolved.name)
    mode = resolved.mode
    filters = [("模式", mode)]
    stats = next((row for row in (profile.get("stats") or []) if row.get("mode") == mode), {})
    for key, label in (("total", "总场次"), ("score", "当前推栏分"), ("mvp", "总 MVP")):
        if key in stats:
            filters.append((label, field(stats, key)))
    if stats.get("total") and isinstance(stats.get("wins"), int):
        filters.append(("胜率", percent(stats["wins"] / stats["total"])))
    if highest := profile.get("seasonHighestScore"):
        filters.append(("赛季最高（估算）", str(highest)))
    has_season_matches = isinstance(stats.get("total"), int) and stats["total"] > 0
    if not document.get("data") and not request.cursor and has_season_matches:
        document = await recover_recent(client, resolved) or document
    start, end = request.filters.start, request.filters.end
    if start or end:
        filters.append(("日期", start if start == end else f"{start or '最早'} 至 {end or '最新'}"))
    entries, continuation = await history_page(client, request, resolved, document)
    matches = [match for match, _ in entries]
    rows = [[str(i), match.time, match.duration, result, match.score]
            for i, (match, result) in enumerate(entries, 1)]
    cell_colors = {}
    for index, (_, result) in enumerate(entries):
        tone = RESULT_TONES.get(result.strip()) or RESULT_TONES.get(result.strip().casefold())
        if tone:
            cell_colors[(index, 3)] = tone
    options = [(entry.label(), MatchQuery(mode, entry)) for entry in matches]
    if continuation:
        options.append(("继续查更早战绩", QueryTarget("战绩", continuation)))
    if matches:
        options.append(("统计本页常用配队", QueryTarget("角色配队", RoleLineups(resolved, heart, tuple(matches[:10])))))
        options.append(("多场战绩复盘", QueryTarget(REVIEW_SUMMARY, replace(request, origin=resolved))))
    options.append(("查看角色总览", QueryTarget("角色总览", resolved)))
    menu = ChoiceMenu("请选择对局或继续查询。", "对局详情", tuple(options))
    subtitle = label_parts(resolved.server, " · ", heart_label(heart)) if heart else resolved.server
    note = f"本页 {len(matches)} 场" if matches else "暂无近期对局。"
    if not matches and (start or end or request.offset or request.cursor):
        note = "本批战绩中没有符合条件的对局。" if continuation else "未找到符合条件的更多对局。"
    elif not matches and has_season_matches:
        note = "已有赛季统计，暂无近期对局。"
    return Card(labeled(name, heart), subtitle, filters, ["编号", "时间", "耗时", "结果", "战后积分"], rows,
                menu=menu, note=note, cell_colors=cell_colors)


async def history_page(client: DataClient, request: RecordQuery, role: Role, first: dict) -> tuple[list, RecordQuery | None]:
    cursor, offset = request.cursor, request.offset
    bounds = request.filters
    matches = {}
    anchor = request.anchor
    anchor_time = history_timestamp(anchor.time) if anchor else ""

    def continuation(cursor: int, offset: int) -> RecordQuery:
        previous = next(reversed(matches.values()))[0] if matches else anchor
        return replace(request, origin=role, cursor=cursor, offset=offset, anchor=previous,
                       shown=request.shown.union(matches))

    for _ in range(3):
        current = first if not cursor else await client.get(
            "player/home-page", body={**record_body(role, cursor), "onlyMatches": True}, ttl=240,
        )
        entries = current.get("data") or []
        if anchor and cursor == request.cursor:
            # An inserted or removed match can move the original offset.
            offset = next((i + 1 for i, entry in enumerate(entries)
                           if field(entry, "match_id", "") == anchor.id), 0)
        next_cursor = current.get("next_cursor")
        more = current.get("has_more") and isinstance(next_cursor, int) and next_cursor > cursor
        options = []
        dates = []
        for index, entry in enumerate(entries[offset:], offset):
            match_id = field(entry, "match_id", "")
            if not re.fullmatch(r"[0-9]{1,24}", match_id) or match_id in request.shown or match_id in matches:
                continue
            stamp = field(entry, "time")
            normalized = history_timestamp(stamp)
            if normalized and anchor_time and normalized > anchor_time:
                continue
            day = stamp[:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                dates.append(day)
            if (bounds.start or bounds.end) and (
                not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)
                or bounds.start and day < bounds.start or bounds.end and day > bounds.end
            ):
                continue
            options.append((index, RecentMatch(match_id, stamp, field(entry, "duration"), field(entry, "score")),
                            field(entry, "result")))
        if bounds.start and dates and min(dates) < bounds.start:
            more = False
        for index, match, result in options:
            if match.id in matches:
                continue
            if len(matches) >= bounds.limit:
                return list(matches.values()), continuation(cursor, index)
            matches[match.id] = (match, result)
        if not more:
            return list(matches.values()), None
        if len(matches) >= bounds.limit:
            # Resume from the page that contained the anchor before advancing the upstream cursor.
            return list(matches.values()), continuation(cursor, len(entries))
        cursor, offset = next_cursor, 0
    return list(matches.values()), continuation(cursor, offset)


def history_timestamp(value: str) -> str:
    stamp = value[:19].replace("T", " ")
    return stamp if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", stamp) else ""


async def match_payload(client: DataClient, mode: str, match_id: str) -> dict:
    response = await client.get("match/detail", body={"match_id": match_id, "mode": mode}, ttl=300)
    return match_document(response, "match/detail", match_id)


async def role_lineups(client: DataClient, request: RoleLineups) -> Card:
    games, wins = Counter(), Counter()
    for entry in request.matches:
        try:
            data = await match_payload(client, request.role.mode, entry.id)
        except ApiError as exc:
            if exc.status == 429:
                break
            continue
        except QueryError:
            continue
        for side in data.get("teams") or []:
            players = side.get("players") or []
            if any(player.get("name") == request.role.name and player.get("server") == request.role.server for player in players):
                names = [player.get("career") or player.get("school") or "未知心法" for player in players]
                lineup = " ".join(sorted(names))
                games[lineup] += 1
                wins[lineup] += side.get("result") == "win"
                break
    rows = [[lineup_label(lineup), str(count), str(wins[lineup]), percent(wins[lineup]/count, 0)]
            for lineup, count in games.most_common()]
    return Card(label_parts(labeled(request.role.name, request.school), " · 常用配队"),
                "按当前战绩页的前 10 场公开对局计算", [("赛制", request.role.mode),
                ("有效样本", f"{sum(games.values())}/{len(request.matches)} 场")],
                ["阵容", "场次", "胜场", "胜率"], rows,
                note="" if rows else "可用数据不足，暂时无法识别该角色的阵容。")


def team_title(team: dict, index: int) -> str:
    result = {"win": "胜利", "lose": "失败", "loss": "失败", "draw": "平局"}.get(team.get("result"), "")
    return " · ".join(filter(None, [field(team, "name", f"队伍 {index + 1}"), result]))


def participant_label(player: dict, mvp: bool = False):
    return labeled(field(player, "name") + (" · MVP" if mvp and player.get("mvp") else ""),
                   player.get("career") or player.get("_kungfu") or player.get("school") or "")


async def match_detail(client: DataClient, request: MatchQuery) -> Card:
    params = {"match_id": request.match.id, "mode": request.mode}
    if request.replay:
        response = await client.get("match/replay", body=params, ttl=300)
        raw = match_document(response, "match/replay", request.match.id)
        exported = json.dumps({
            "schema": "jx3-ali.match-replay.v1", "matchId": request.match.id, "mode": request.mode,
            "source": "xoyo.GET_MATCH_REPLAY.data", "exportedAt": datetime.now(timezone.utc).isoformat(), "data": raw,
        }, ensure_ascii=False).encode("utf8")
        filters = [("赛制", request.mode), ("时间", request.match.time), ("时长", request.match.duration)]
        return replay_card(ReplayView(parse_replay(exported, request.arena or "名剑大会", filters, request.mode)))
    data = await match_payload(client, request.mode, request.match.id)
    arena = field(data, "map", "名剑大会")
    filters = [
        ("赛制", request.mode), ("时间", field(data, "start_time", request.match.time)),
        ("时长", field(data, "duration", request.match.duration)), ("查询角色战后分", request.match.score),
    ]
    teams = data.get("teams") or []
    options = []
    tables = []
    for side, team in enumerate(teams):
        title = team_title(team, side)
        rows = []
        for index, player in enumerate(team.get("players") or []):
            if not player.get("name"):
                continue
            if request.player == (side, index):
                return player_detail(player, title, arena, filters[:3])
            school = field(player, "career", field(player, "school", ""))
            stats = player.get("stats") or {}
            metrics = {m.get("label"): field(m, "value") for m in player.get("metrics") or []}
            key_stats = " / ".join(f"{key} {metrics[key]}" for key in ("关键伤害", "关键治疗") if key in metrics)
            rows.append([
                participant_label(player, mvp=True), heart_label(school) if school else "—",
                field(stats, "damage"), field(stats, "heal"), key_stats or "—",
                metrics.get("本场濒危", metrics.get("濒危", "—")),
            ])
            options.append((label_parts(participant_label(player), f" · {title}"),
                            replace(request, player=(side, index))))
        if rows:
            tables.append(Table(title, ["角色", "心法", "DPS", "HPS", "关键伤害／治疗", "濒危"], rows))
    if request.player:
        raise QueryError("原站这场对局没有返回所选角色。")
    if not tables:
        raise QueryError("原站没有展示这场对局的阵容。")
    replay_match = replace(request.match, time=filters[1][1], duration=filters[2][1])
    options.append(("回放复盘", replace(request, replay=True, arena=arena, match=replay_match)))
    return Card(f"对局详情 · {arena}", "双方阵容与对局数据", filters, tables=tables,
                menu=ChoiceMenu("请选择角色详情或回放复盘。", "对局详情", tuple(options)))


def player_detail(player: dict, side: str, arena: str, filters: list) -> Card:
    school = field(player, "career", field(player, "school", ""))
    tables = []
    for title, source in (("数据统计", "metrics"), ("基础属性", "qualities")):
        rows = [[field(entry, "label"), field(entry, "value")] for entry in player.get(source) or []]
        if rows:
            paired = [rows[i] + (rows[i + 1] if i + 1 < len(rows) else ["", ""]) for i in range(0, len(rows), 2)]
            tables.append(Table(title, ["指标", "数值", "指标", "数值"], paired))
    groups = []
    for title, key in (("奇穴", "talents"), ("装备", "equipments")):
        items = [{"name": field(item, "name"), "icon": field(item, "icon", "")} for item in player.get(key) or []]
        if items:
            groups.append((title, items))
    if not (tables or groups):
        raise QueryError("原站没有展示该角色的详细数据。")
    stats = player.get("stats") or {}
    specs = [("心法", heart_label(school)), ("区服", field(player, "server")), ("装分", field(player, "score")),
             ("JJC", field(stats, "jcc")), ("推栏分", field(stats, "tl")), ("角色胜率", field(player, "winRate"))]
    return Card(label_parts(participant_label(player), " · 本场详细数据"), f"{arena} · {side}",
                [*filters, *specs], tables=tables, groups=groups)
