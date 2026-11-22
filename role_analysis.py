from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from time import monotonic

from .cards import RADAR_GRADES, Card, Radar, Table, Trend
from .data import ApiError, DataClient, field, percent
from .labels import heart_label, label_parts, labeled, lineup_label
from .matches import (
    MatchQuery, RecentMatch, RecordQuery, Role, history_page, match_payload, recover_recent, resolve_profile,
)
from .query import REVIEW_SUMMARY, ChoiceMenu, QueryError, QueryTarget
from .rankings import MODES, heart_name


@dataclass(frozen=True)
class RadarSelection:
    role: Role
    heart: str = ""


def number(value) -> int | None:
    text = str(value)
    return int(text) if re.fullmatch(r"[0-9]{1,9}", text) else None


def win_rate(wins: int | None, total: int | None) -> str:
    return percent(wins / total) if wins is not None and total and 0 <= wins <= total else "—"


async def role_overview(client: DataClient, origin: Role | tuple | RadarSelection) -> Card | ChoiceMenu:
    selection = origin if isinstance(origin, RadarSelection) else None
    request = RecordQuery(selection.role if selection else origin)
    result = await resolve_profile(client, request, "角色总览")
    if isinstance(result, ChoiceMenu):
        return result
    role, profile = result.role, result.profile
    stats = {row.get("mode"): row for row in profile.get("stats") or [] if isinstance(row, dict)}
    grouped = profile.get("radarGroups") or {}
    legacy = profile.get("radar") or {}
    options, rows = [], []
    radars = {}
    for mode in MODES:
        summary = stats.get(mode, {})
        heart = heart_name(summary.get("kungfu"))
        wins, total = number(summary.get("wins")), number(summary.get("total"))
        rows.append([mode, heart_label(heart) if heart else "—", field(summary, "score"),
                     field(summary, "total"), field(summary, "wins"), win_rate(wins, total),
                     field(summary, "mvp"), str(number(summary.get("rank")) or "—")])
        scoped = replace(role, mode=mode)
        options.append((f"查看 {mode} 战绩", QueryTarget("战绩", scoped)))
        records = [item for item in grouped.get(mode) or [] if isinstance(item, dict) and item.get("items")]
        if not records and legacy.get(mode):
            records = [{"kungfu": heart, "items": legacy[mode]}]
        for group in records:
            spec = heart_name(group.get("kungfu")) or heart
            radars[(mode, spec)] = group

    selected = (role.mode, selection.heart if selection else "")
    if not selected[1]:
        preferred = heart_name(stats.get(role.mode, {}).get("kungfu"))
        selected = next((key for key in radars if key == (role.mode, preferred)),
                        next((key for key in radars if key[0] == role.mode), selected))
    radar, detail, note = None, None, f"暂无 {role.mode} 的角色雷达。"
    group = radars.get(selected)
    if group:
        spec = selected[1]
        title = label_parts(f"{role.mode} · ", heart_label(spec), " · 角色雷达") if spec else f"{role.mode} · 角色雷达"
        metrics, axes = [], []
        for item in group["items"]:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            name, grade = str(item["name"]), field(item, "grade", "").upper()
            raw = item.get("value")
            shown = field(item, "label", field(item, "value"))
            if name in {"控制成功率", "躲控成功率", "战斗效率"} and isinstance(raw, (int, float)):
                shown = percent(raw, 2)
            rank = item.get("ranking")
            rank = str(int(rank)) if isinstance(rank, (int, float)) and 0 < rank < 1e9 else "—"
            metrics.append([name, shown, grade or "—", rank])
            if grade in RADAR_GRADES:
                axes.append((name, grade))
        if len(axes) >= 3:
            radar = Radar(title, tuple(axes[:8]))
        if metrics:
            note = ""
            count = number(group.get("total"))
            subtitle = f"{role.mode} 指标" + (f" · {count} 场" if count else "")
            title = label_parts("雷达指标 · ", heart_label(spec)) if spec else "雷达指标"
            detail = Table(title, ["指标", "数值", "评级", "名次"], metrics, subtitle, compact=True)
    choices = tuple((label_parts(f"{mode} · ", heart_label(heart)) if heart else mode,
                     RadarSelection(replace(role, mode=mode), heart))
                    for mode, heart in radars if (mode, heart) != selected)
    if choices:
        options.append(("切换雷达赛制或心法", ChoiceMenu("请选择赛制和心法。", "角色总览", choices)))
    options.append((f"复盘 {role.mode} 最近战绩", QueryTarget(REVIEW_SUMMARY, RecordQuery(role))))
    filters = [("区服", role.server)]
    if result.heart:
        filters.append(("当前心法", heart_label(heart_name(result.heart))))
    if profile.get("camp"):
        filters.append(("阵营", field(profile, "camp")))
    return Card(label_parts(labeled(role.name, result.heart), " · 角色总览"), "名剑大会 · 跨赛制数据", filters,
                ["赛制", "心法", "推栏分", "场次", "胜场", "胜率", "MVP", "名次"], rows, radar=radar,
                tables=[detail] if detail else [], note=note,
                menu=ChoiceMenu("请选择战绩、雷达或多场复盘。", "角色总览", tuple(options)))


DETAIL_BATCH = 10
DETAIL_SECONDS = 28
SCORE = re.compile(r"^\s*([0-9]{1,6})(?=\s|[（(+＋\-－]|$)")
OUTCOMES = {"胜": "win", "胜利": "win", "负": "loss", "败": "loss", "失败": "loss",
            "平": "draw", "平局": "draw", "win": "win", "lose": "loss", "loss": "loss", "draw": "draw"}
QUEUE_LABELS = {"全部": "全部", "组排": "疑似组排", "散排": "散排候选", "未知": "未知"}


@dataclass(frozen=True)
class Peer:
    name: str
    server: str
    heart: str


@dataclass(frozen=True)
class GameDetails:
    outcome: str
    lineup: tuple[str, ...]
    peers: tuple[Peer, ...]
    heart: str
    talents: tuple[tuple[str, str], ...]
    match_id: str


@dataclass(frozen=True)
class ReviewSource:
    args: Role | tuple | RecordQuery | ReviewBatch
    action: str = "战绩复盘"


@dataclass(frozen=True)
class ReviewBatch:
    request: RecordQuery
    role: Role
    heart: str
    matches: tuple[tuple[RecentMatch, str], ...]
    offset: int = 0
    details: tuple[GameDetails, ...] = ()
    older: RecordQuery | None = None
    failed: tuple[int, ...] = ()
    source: ReviewSource | None = None


@dataclass(kw_only=True)
class ReviewCard(Card):
    batch: ReviewBatch


def same_role(player: dict, role: Role) -> bool:
    global_id = field(player, "global_role_id", "") or field(player, "globalRoleId", "")
    if global_id and role.global_id:
        return global_id == role.global_id
    server = field(player, "server", "")
    if server != role.server:
        return False
    role_id = field(player, "roleid", "") or field(player, "roleId", "")
    if role_id and role.role_id:
        return role_id == role.role_id
    name = field(player, "name", "")
    return name == role.name or name == f"{role.name}@{server}" or role.name == f"{name}@{server}"


def review_detail(role: Role, outcome: str, detail: dict) -> GameDetails | None:
    for side in detail.get("teams") or []:
        players = [player for player in side.get("players") or [] if isinstance(player, dict)]
        matches = [player for player in players if same_role(player, role)]
        if len(matches) != 1:
            continue
        player = matches[0]
        hearts = [heart_name(row.get("career") or row.get("_kungfu") or "") for row in players]
        lineup = tuple(sorted(hearts)) if all(hearts) and len(players) == int(role.mode[0]) else ()
        peers = tuple(Peer(field(row, "name", ""), field(row, "server", ""), heart)
                      for row, heart in zip(players, hearts) if row is not player and row.get("name"))
        talents = {item["name"]: field(item, "icon", "") for item in player.get("talents") or []
                   if isinstance(item, dict) and item.get("name")}
        return GameDetails(OUTCOMES.get(side.get("result"), OUTCOMES.get(outcome, "")), lineup, peers,
                           heart_name(player.get("career") or player.get("_kungfu") or ""), tuple(talents.items()),
                           field(detail, "match_id", ""))
    return None


async def battle_review(client: DataClient, origin: Role | tuple | RecordQuery | ReviewBatch,
                        *, summary_only: bool = False) -> Card | ChoiceMenu:
    action = REVIEW_SUMMARY if summary_only else "战绩复盘"
    if isinstance(origin, ReviewBatch):
        batch = origin if origin.source is not None else replace(origin, source=ReviewSource(origin))
    else:
        request = origin if isinstance(origin, RecordQuery) else RecordQuery(origin)
        profile = await resolve_profile(client, request, action)
        if isinstance(profile, ChoiceMenu):
            return profile
        doc = profile.document
        if not doc.get("data") and not request.cursor:
            has_matches = any(row.get("mode") == profile.role.mode and (number(row.get("total")) or 0) > 0
                              for row in profile.profile.get("stats") or [] if isinstance(row, dict))
            if has_matches:
                doc = await recover_recent(client, profile.role) or doc
        entries, older = await history_page(client, request, profile.role, doc)
        batch = ReviewBatch(request, profile.role, profile.heart, tuple(entries), older=older,
                            source=ReviewSource(origin, action))

    return review_card(batch) if summary_only else await review_details(client, batch)


async def retry_review(client: DataClient, batch: ReviewBatch) -> Card:
    return await review_details(client, batch, retry=True)


async def review_details(client: DataClient, batch: ReviewBatch, *, retry: bool = False) -> Card:
    details = {item.match_id: item for item in batch.details}
    failed = dict.fromkeys(batch.failed)
    offset, limited = batch.offset, False
    deadline = monotonic() + DETAIL_SECONDS
    indexes = batch.failed[:DETAIL_BATCH] if retry else range(offset, min(offset + DETAIL_BATCH, len(batch.matches)))
    for index in indexes:
        if monotonic() >= deadline:
            break
        entry, outcome = batch.matches[index]
        try:
            result = await match_payload(client, batch.role.mode, entry.id)
        except QueryError as exc:
            status = exc.status if isinstance(exc, ApiError) else None
            if status == 429:
                limited = True
                break
            failed.pop(index, None)
            if status is None or status == 408 or 500 <= status < 600:
                failed[index] = None
        else:
            failed.pop(index, None)
            parsed = review_detail(batch.role, outcome, result)
            if parsed:
                details[entry.id] = parsed
        if not retry:
            offset += 1
    ordered = tuple(details[entry.id] for entry, _ in batch.matches if entry.id in details)
    return review_card(replace(batch, offset=offset, details=ordered, failed=tuple(failed)), limited)


def team_identity(game: GameDetails, size: int) -> tuple[tuple[str, str], ...]:
    peers = set()
    for peer in game.peers:
        name, server = peer.name.strip(), peer.server.strip()
        name = name.removesuffix(f"@{server}") if server else name
        if not server or name in {"", "—", "未知", "未知角色", "未知侠士"}:
            return ()
        peers.add((name, server))
    if len(game.peers) == len(peers) == size - 1:
        return tuple(sorted(peers))
    return ()


def review_queues(batch: ReviewBatch) -> dict[str, str]:
    teams = {}
    for game in batch.details:
        key = team_identity(game, int(batch.role.mode[0]))
        if key:
            teams[game.match_id] = key
    counts = Counter(teams.values())
    allow_candidates = batch.offset >= len(batch.matches) and not batch.failed and len(teams) >= 2
    return {match_id: "组排" if counts[key] >= 2 else "散排"
            for match_id, key in teams.items() if counts[key] >= 2 or allow_candidates}


async def review_queue_view(client: DataClient, batch: ReviewBatch) -> Card:
    return review_card(batch, getattr(client, "limited_until", 0) > monotonic())


def score_trend(matches: tuple[tuple[RecentMatch, str], ...]) -> tuple[Trend | None, int, str]:
    dates, points = [], []
    for match, _ in reversed(matches):
        found = SCORE.match(match.score)
        points.append(int(found[1]) if found else None)
        dates.append(match.time[5:16] if len(match.time) >= 16 else match.time)
    known = [point for point in points if point is not None]
    if not known:
        return None, 0, "—"
    change = f"{known[0]} → {known[-1]}（{known[-1]-known[0]:+d}）" if len(known) > 1 else str(known[0])
    return Trend(tuple(dates), (("战后推栏分", tuple(points)),), "战后积分", " 分", include_zero=False), len(known), change


def review_card(batch: ReviewBatch, limited: bool = False) -> ReviewCard:
    count = len(batch.matches)
    bounds = batch.request.filters
    first_summary = bool(count and not batch.offset and batch.source and batch.source.action == REVIEW_SUMMARY)
    queues = review_queues(batch)
    queue_counts = Counter(queues.get(match.id, "未知") for match, _ in batch.matches)
    if first_summary or bounds.queue == "全部":
        matches, review_details = batch.matches, batch.details
    else:
        matches = tuple((match, result) for match, result in batch.matches
                        if queues.get(match.id, "未知") == bounds.queue)
        review_details = tuple(game for game in batch.details
                               if queues.get(game.match_id, "未知") == bounds.queue)
    selected = len(matches)
    scores = Counter(OUTCOMES.get(result, "") for _, result in matches)
    decided = scores["win"] + scores["loss"] + scores["draw"]
    filters = [("赛制", batch.role.mode), ("选中战绩", f"{count}/{batch.request.filters.limit} 场"),
               ("详情覆盖", f"{len(batch.details)}/{count} 场"),
               ("已尝试详情", f"{batch.offset}/{count} 场")]
    if first_summary and bounds.queue != "全部":
        filters.append(("待应用筛选", QUEUE_LABELS[bounds.queue]))
    if count and not first_summary:
        filters.append(("排队筛选", QUEUE_LABELS[bounds.queue]))
        if bounds.queue != "全部":
            filters.append(("本筛选", f"{selected} 场"))
        filters.append(("分类", " · ".join(f"{label} {queue_counts[key]}"
                         for key, label in QUEUE_LABELS.items() if key != "全部")))
    if bounds.start or bounds.end:
        span = bounds.start if bounds.start == bounds.end else f"{bounds.start or '最早'} 至 {bounds.end or '最新'}"
        filters.append(("日期", span))
    if decided:
        filters.extend([("胜负", f'{scores["win"]}胜 {scores["loss"]}负 {scores["draw"]}平'),
                        ("胜率", win_rate(scores["win"], decided))])
    chart, score_count, change = score_trend(matches)
    if score_count:
        filters.append(("积分", change))
    if score_count and score_count != selected:
        filters.append(("有效积分", f"{score_count}/{selected} 场"))

    lineups, peers, sets = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    peer_hearts = defaultdict(Counter)
    items = {}
    for game in review_details:
        if game.lineup:
            lineups[game.lineup][game.outcome] += 1
        seen = set()
        for peer in game.peers:
            key = (peer.name, peer.server)
            if key in seen:
                continue
            seen.add(key)
            peers[key][game.outcome] += 1
            peer_hearts[key][peer.heart] += 1
        if game.heart and game.talents:
            key = game.heart, tuple(sorted(name for name, _ in game.talents))
            sets[key][game.outcome] += 1
            items[key] = game.talents

    def ordered(entries, limit):
        return sorted(entries.items(), key=lambda pair: (-sum(pair[1].values()), pair[0]))[:limit]

    def summary(results):
        total = sum(results.values())
        return [str(total), str(results["win"]), win_rate(results["win"], total - results[""])]

    tables = []
    if lineups:
        rows = [[lineup_label(" ".join(names)), *summary(results)] for names, results in ordered(lineups, 10)]
        tables.append(Table("常用阵容", ["阵容", "同队场次", "胜场", "胜率"], rows))
    if peers:
        rows = [[labeled(name, peer_hearts[(name, server)].most_common(1)[0][0]), server or "—", *summary(results)]
                for (name, server), results in ordered(peers, 10)]
        tables.append(Table("常用队友", ["角色", "服务器", "同队场次", "胜场", "胜率"], rows))
    groups = []
    for (heart, names), results in ordered(sets, 5):
        total, _, rate = summary(results)
        caption = label_parts("常用奇穴 · ", heart_label(heart), f" · {total} 场 · 胜率 {rate}")
        groups.append((caption, [{"name": name, "icon": icon} for name, icon in items[(heart, names)]]))
    if groups:
        filters.append(("有奇穴记录", f"{sum(sum(stats.values()) for stats in sets.values())}/{selected} 场"))
    options = []
    if batch.offset < count:
        prefix = "分析前" if first_summary else "继续分析后"
        options.append((f"{prefix} {min(DETAIL_BATCH, count - batch.offset)} 场对局详情", batch))
    if batch.failed:
        options.append((f"重试失败场次（{len(batch.failed)} 场）", QueryTarget("复盘补查", batch)))
    details = ChoiceMenu("请选择一场对局。", "对局详情", tuple(
        (match.label(), MatchQuery(batch.role.mode, match)) for match, _ in matches
    ))
    if selected:
        options.append(("查看样本中的单场详情", details))
    if count and not first_summary:
        choices = tuple(
            (f"{label} · {count if key == '全部' else queue_counts[key]} 场",
             replace(batch, request=replace(batch.request, filters=replace(bounds, queue=key))))
            for key, label in QUEUE_LABELS.items() if key != bounds.queue
        )
        options.append(("切换排队筛选", ChoiceMenu("请选择排队筛选口径。", "复盘排队筛选", choices)))
    if batch.older and count < bounds.limit:
        target = batch.older
        if batch.source and batch.source.action == REVIEW_SUMMARY:
            target = QueryTarget(REVIEW_SUMMARY, batch.older)
        options.append(("查询更早一段公开战绩", target))
    options.append(("查看角色总览", QueryTarget("角色总览", batch.role)))
    notes = ["积分折线按对局时间排列，各点为战后分。"] if chart else []
    if not count:
        notes.append("公开战绩中暂未查到符合条件的对局。")
    elif first_summary:
        notes.append("汇总包括全部排队类型。按序号分析对局详情，可补查阵容、队友和奇穴，并应用排队筛选。")
    else:
        notes.append("排队类型按本次样本中的同队次数推测。同一完整队伍出现至少两次，列为疑似组排。")
        notes.append("至少有两场完整样本且分析结束后，完整队伍只出现一次的场次列为散排候选。其余未能分类的场次列为未知。")
        if bounds.queue != "全部" and not selected:
            notes.append("当前筛选暂无对局。")
        if batch.offset > len(batch.details):
            notes.append("有些对局详情未能读取，或无法在详情中匹配该角色。")
        if chart and bounds.queue != "全部":
            notes.append("首尾战后分的差值可能包含未选对局的影响。")
    if limited:
        notes.append("当前查询受限，可稍后继续分析。")
    title = " · 战绩复盘汇总" if first_summary else " · 多场战绩复盘"
    return ReviewCard(label_parts(labeled(batch.role.name, batch.heart), title), batch.role.server, filters,
                      note="".join(notes), chart=chart, tables=tables, groups=groups,
                      menu=ChoiceMenu("请选择下一步。", "战绩复盘", tuple(options)), batch=batch)
