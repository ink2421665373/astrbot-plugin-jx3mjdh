import json
import re
from collections import Counter
from dataclasses import dataclass, replace

from .cards import Card, Table, Trend
from .labels import heart_icon, labeled
from .query import ChoiceMenu, QueryError, QueryTarget


MAX_EXPORT_BYTES = 4 * 1024 * 1024
NO_ID = {"", "0", "-1"}


@dataclass(frozen=True)
class ReplayPlayer:
    id: str
    local_id: str
    name: str
    label: str
    attack: tuple[int | None, ...]
    icon: str = ""


@dataclass(frozen=True)
class ReplayEvent:
    ms: int
    skill: str
    caster: str
    target: str
    kill: bool


@dataclass(frozen=True)
class ReplayExport:
    name: str
    content: bytes


@dataclass(frozen=True, eq=False)
class Replay:
    match_id: str
    arena: str
    filters: tuple[tuple[str, str], ...]
    players: tuple[ReplayPlayer, ...]
    events: tuple[ReplayEvent, ...]
    export: ReplayExport | None


@dataclass(frozen=True)
class ReplayView:
    replay: Replay
    player_id: str = ""
    related: bool = False
    kills: bool = False


def field_text(value) -> str:
    if isinstance(value, (str, int)):
        return " ".join(str(value).split())
    return ""


def parse_replay(
    raw: bytes, arena: str, filters: list[tuple[str, str]], mode: str,
    player_icons: dict[str, str] | None = None,
) -> Replay:
    try:
        document = json.loads(raw)
    except ValueError:
        raise QueryError("原站下载的回放文件无法读取。") from None
    if not isinstance(document, dict) or document.get("schema") != "jx3-ali.match-replay.v1":
        raise QueryError("原站回放导出格式有变化，暂时无法解析。")
    data = document.get("data")
    if not isinstance(data, dict):
        raise QueryError("这场对局暂无原站可用的回放数据。")
    match_id = field_text(document.get("matchId") or data.get("match_id"))
    if not re.fullmatch(r"[0-9]{1,24}", match_id):
        raise QueryError("无法识别原站回放编号。")
    source_mode = field_text(document.get("mode"))
    if source_mode in {"2v2", "3v3", "5v5"} and source_mode != mode:
        raise QueryError("原站导出的回放赛制和所选对局不同，请重新打开对局。")
    raw_players = data.get("players")
    player_icons = player_icons or {}
    players = []
    if isinstance(raw_players, list):
        for item in raw_players:
            if not isinstance(item, dict):
                continue
            local_id = field_text(item.get("role_id"))
            player_id = field_text(item.get("global_role_id")) or local_id
            label = field_text(item.get("role_name"))
            if player_id in NO_ID or not label:
                continue
            raw_attack = item.get("attack_trend") or []
            attack = tuple(
                value if type(value) is int and 0 <= value < 10**12 else None
                for value in raw_attack
            ) if isinstance(raw_attack, list) else ()
            short_name = label.rsplit("·", 1)[0].strip() or label
            icon = player_icons.get(label) or player_icons.get(short_name) or heart_icon(field_text(item.get("kungfu_name")))
            players.append(ReplayPlayer(player_id, local_id, short_name, label, attack, icon))
    names = Counter(player.name for player in players)
    players = [replace(player, name=player.label) if names[player.name] > 1 else player for player in players]
    raw_events = data.get("replay")
    events = []
    if isinstance(raw_events, list):
        for item in raw_events:
            if not isinstance(item, dict):
                continue
            raw_time = item.get("ts")
            if type(raw_time) is not int or raw_time < 0:
                continue
            skill = field_text(item.get("skillName")) or "未知技能"
            caster = field_text(item.get("casterId"))
            target = field_text(item.get("targetId"))
            is_kill = skill == "击杀" or field_text(item.get("skillId")) == "-1"
            events.append(ReplayEvent(raw_time, skill, caster, target, is_kill))
    if not events and not any(player.attack for player in players):
        raise QueryError("原站当前没有这场对局的技能流水或伤害趋势。")
    export = ReplayExport(f"jx3-replay-{mode}-{match_id}.json", raw) if len(raw) <= MAX_EXPORT_BYTES else None
    return Replay(
        match_id, arena, tuple(filters), tuple(players), tuple(sorted(events, key=lambda event: event.ms)),
        export,
    )


def replay_card(view: ReplayView) -> Card:
    replay = view.replay
    current = next((player for player in replay.players if player.id == view.player_id), None)
    participants = {}
    for player in replay.players:
        tag = labeled(player.name, source=player.icon)
        participants[player.id] = tag
        if player.local_id not in NO_ID:
            participants.setdefault(player.local_id, tag)
    kills = [event for event in replay.events if event.kill]
    selected = list(replay.events)
    role_counts = None
    if current:
        ids = {current.id, current.local_id} - NO_ID
        cast = [event for event in selected if event.caster in ids]
        received = [event for event in selected if event.target in ids and event.caster not in ids]
        role_counts = (len(cast), len(received))
        selected = sorted([*cast, *received], key=lambda event: event.ms) if view.related else cast
    if view.kills:
        selected = kills
    rows = []
    for event in selected:
        if event.target in NO_ID:
            target = "无目标"
        elif event.target == event.caster:
            target = "自身"
        else:
            target = participants.get(event.target, labeled("未知目标"))
        rows.append([
            f"{event.ms / 1000:.2f}s", participants.get(event.caster, labeled("未知角色")), event.skill, target,
        ])
    label = "击杀点" if view.kills else "技能流水"
    filters = [*replay.filters, ("回放编号", replay.match_id)]
    if current and not view.kills:
        filters.extend((
            ("玩家", labeled(current.label, source=current.icon)), ("筛选", "相关" if view.related else "出手"),
            ("出手", str(role_counts[0])), ("承受", str(role_counts[1])),
        ))
    filters.append(("记录数", f"{len(selected)} 条"))
    note = ""
    if not rows:
        note = "暂无击杀记录。" if view.kills else "这个筛选条件下没有技能记录。"
    if replay.export is None:
        note += "回放 JSON 超过 4 MB。可在网页回放页下载文件。"
    chart = replay_trend(current, replay.players) if not view.kills else None
    tables = [Table(label, ["时间", "施法者", "技能", "目标"], rows, compact=True)] if rows else []
    return Card(
        f"回放复盘 · {replay.arena}", label, filters, tables=tables, note=note,
        chart=chart, menu=replay_menu(view, len(kills)),
    )


def replay_trend(current: ReplayPlayer | None, players: tuple[ReplayPlayer, ...]) -> Trend | None:
    sources = (current,) if current else players
    lines = [(labeled(player.name, source=player.icon), player.attack) for player in sources if any(x is not None for x in player.attack)]
    if not lines:
        return None
    count = max(len(values) for _, values in lines)
    indices = list(range(count))
    if count > 240:
        indices = sorted({round(index * (count - 1) / 239) for index in range(240)})
    return Trend(
        tuple(str(index) for index in indices),
        tuple((label, tuple(values[index] if index < len(values) else None for index in indices)) for label, values in lines),
        axis_label="累计伤害 · 横轴为采样点", suffix="",
    )


def replay_menu(view: ReplayView, kill_count: int) -> ChoiceMenu:
    options = []
    if view.kills:
        options.append(("返回技能流水", replace(view, kills=False)))
    else:
        options.append((f"查看本场击杀点（{kill_count} 条）", replace(view, kills=True)))
        if view.player_id:
            mode = "出手" if view.related else "相关"
            options.append((f"切换为只看该玩家的{mode}记录", replace(view, related=not view.related)))
        if view.replay.players:
            choices = [("全部玩家", replace(view, player_id="", related=False))]
            choices.extend(
                (labeled(player.label, source=player.icon), replace(view, player_id=player.id, related=False))
                for player in sorted(view.replay.players, key=lambda player: player.name)
            )
            options.append(("按玩家筛选", ChoiceMenu("请选择要查看的玩家。", "回放流水", tuple(choices))))
    if view.replay.export is not None:
        options.append(("导出回放 JSON", QueryTarget("导出回放", view.replay.export)))
    return ChoiceMenu("请选择回放操作。", "回放流水", tuple(options))
