from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone

from astrbot.api import logger

from .cards import Bars, Card, Sparkline, Table, Trend
from .data import ApiError, DataClient, OriginUnavailable, error_kind, field, payload, percent
from .labels import HEART_NAMES, TANKS, heart_label, join_labels, label_parts, labeled
from .matches import Role
from .query import ChoiceMenu, Configuration, QueryError, QueryTarget, Ranking, Strength, TrendQuery


CHINA_TIME = timezone(timedelta(hours=8))
HEALERS = {"奶花", "奶毒", "奶秀", "奶歌", "奶药"}
KNOWN_HEARTS = set(HEART_NAMES.values()) | TANKS
MODES = {"2v2": "2c", "3v3": "3c", "5v5": "5c"}
RANK_FIELDS = {
    "周期": "period", "心法": "school", "服务器": "server", "范围": "limit",
    "模式": "mode", "类型": "role_type", "装备": "weapon", "统计": "metric",
}
PAGE_SIZE = 15


def heart_name(name: str) -> str:
    return HEART_NAMES.get(name, name) if name else ""


def rank_heart(row: dict) -> str:
    return heart_name(row.get("kungfu") or row.get("force"))


def update_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(CHINA_TIME).strftime("%Y/%m/%d %H:%M:%S")
    except (ValueError, AttributeError):
        return value or "—"


def monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


async def periods(client: DataClient) -> tuple[int | None, list]:
    response, saved_at = await client.get_rank_time_tags()
    doc = payload(response)
    tags = [tag for tag in doc.get("week") or [] if type(tag) is int and tag > 0]
    latest = doc.get("defaultWeek") or max((tag for tag in tags if tag < 100000), default=0)
    seasons = []
    for item in doc.get("seasons") or []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
            continue
        try:
            start = monday(date.fromisoformat(item.get("startDate")))
        except (TypeError, ValueError):
            continue
        seasons.append((start, item["name"]))
    seasons.sort(reverse=True)
    current = monday(datetime.now(CHINA_TIME).date())
    reference = monday(saved_at.astimezone(CHINA_TIME).date()) if saved_at is not None else current
    options = []
    for tag in tags:
        try:
            start = (datetime.strptime(str(tag), "%Y%m%d").date() if tag >= 100000
                     else reference - timedelta(weeks=latest - tag))
        except (ValueError, OverflowError):
            continue
        stop = start + timedelta(days=6)
        season = next(((name, (start - since).days // 7 + 1) for since, name in seasons if start >= since), None)
        date_range = f"({start:%m/%d}-{stop:%m/%d})"
        if season:
            name, week = season
            command, label = f"{name}第{week}周", f"{name} · 第{week}周 {date_range}"
        else:
            command, label = "", f"第{tag}周 {date_range}"
        options.append((tag, command, label))
    return (latest or None) if reference == current else None, options


async def rank_source(client: DataClient, query: Ranking, *, weapons: bool):
    try:
        latest, options = await periods(client)
    except (OriginUnavailable, ApiError) as exc:
        if isinstance(exc, ApiError) and not 500 <= exc.status < 600:
            raise
        if query.period:
            raise QueryError("排行榜周期暂时无法查询，请稍后重试。省略周期可尝试查看默认榜单。") from exc
        logger.warning(f"名剑周期列表暂不可用，查询默认榜单 接口=rank/time-tags 类型={error_kind(exc)}")
        latest, options = None, []
    selected = next((item for item in options if item[1] == query.period), None) if query.period else None
    if query.period and not selected:
        raise QueryError(f"原站当前没有周期「{query.period}」。可用「排行 筛选」查看周期。")
    tag = selected[0] if selected else latest
    title = selected[2] if selected else next((item[2] for item in options if item[0] == tag), "当前周期")
    mode = MODES[query.mode]
    time = {"time_type": "week", "time_value": tag} if tag else {}
    result = payload(await client.get("rank/top1000", {"type": mode, **time}, ttl=180))
    rows = result.get("data")
    if not isinstance(rows, list):
        raise QueryError("原站榜单没有返回列表。")
    if weapons and rows:
        weapon_data = await client.get("rank/builds", {
            "mode": "weapons", "scope": "top1000", "rank_type": mode, **time,
        }, ttl=180)
        coverage = weapon_data.get("roles")
        if not isinstance(coverage, list):
            raise QueryError("原站当前没有这个榜单的橙武识别数据。")
        statuses = {str(row.get("global_role_id") or ""): row for row in coverage if row.get("global_role_id")}
        annotated = []
        for row in rows:
            id_value = row.get("global_role_id") or row.get("globalId")
            supplement = statuses.get(str(id_value), {})
            annotated.append({**row, "weapon_status": supplement.get("weapon_status") or row.get("weapon_status"),
                              "kungfu": row.get("kungfu") or supplement.get("kungfu")})
        rows = annotated
    return title, rows


async def ranking(client: DataClient, query: Ranking) -> Card:
    title, entries = await rank_source(client, query, weapons=query.weapon != "全部")
    school = heart_name(query.school)
    if school != "全部心法" and school not in KNOWN_HEARTS and not any(rank_heart(row) == school for row in entries):
        raise QueryError(f"无法识别心法「{query.school}」。发送 /剑三 排行 筛选 可查看心法选项。")
    chosen = query.server
    if chosen != "全部服务器":
        servers = {row.get("server") for row in entries}
        if chosen not in servers:
            available = payload(await client.get("server-list", ttl=3600)).get("servers") or []
            if chosen not in {item.get("value") for item in available}:
                raise QueryError(f"原站服务器列表中没有「{chosen}」。")
        entries = [row for row in entries if row.get("server") == chosen]
    weapon = {"橙武": "orange", "非橙武": "non_orange"}.get(query.weapon)
    filtered = []
    for row in entries[:query.limit]:
        spec = rank_heart(row)
        if school != "全部心法" and spec != school:
            continue
        if weapon and row.get("weapon_status") != weapon:
            continue
        if query.role_type == "奶妈" and spec not in HEALERS:
            continue
        if query.role_type == "DPS" and (spec in HEALERS | TANKS or not spec):
            continue
        filtered.append((row, spec))
    start = (query.page - 1) * PAGE_SIZE
    filters = [("周期", title), ("模式", query.mode), ("范围", f"Top {query.limit}"),
               ("心法", heart_label(school) if school != "全部心法" else school), ("服务器", chosen),
               ("类型", query.role_type), ("装备", query.weapon)]
    metrics = ("人数", "平均胜率")
    options = [(f"看心法{metric}统计", replace(query, metric=metric)) for metric in metrics if metric != query.metric]
    if query.metric:
        options.append(("返回排行榜", replace(query, metric="")))
        return ranking_stats(query.metric, filtered, filters, options)
    subset = filtered[start:start + PAGE_SIZE]
    if not subset and query.page > 1:
        raise QueryError("当前筛选没有这一页，请试试更小的页码。")
    rows, players, numbers = [], [], []
    for row, spec in subset:
        name = row.get("roleName") or row.get("username")
        server = row.get("server")
        shown = labeled(name or "—", spec)
        rows.append([field(row, "rank"), heart_label(spec) if spec else "—", shown, server or "—",
                     field(row, "score"), field(row, "winRate", field(row, "win_rate"))])
        if name and server:
            role = Role(name, server, query.mode, field(row, "roleid", ""),
                        field(row, "global_role_id", "") or field(row, "globalId", ""))
            players.append((label_parts(f'第 {field(row, "rank")} 名 · ', shown, f" · {server}"),
                            QueryTarget("战绩", role)))
        numbers.append(len(players) if name and server else None)
    if start + PAGE_SIZE < len(filtered):
        options.append(("查看下一页", replace(query, page=query.page + 1)))
    return Card("竞技场排行榜", f"第 {query.page} 页 · 共 {len(filtered)} 条", filters,
                ["名次", "心法", "角色", "服务器", "积分", "胜率"], rows,
                menu=ChoiceMenu("请选择角色战绩或榜单统计。", "排行", tuple(players + options)),
                note="" if rows else "当前筛选暂无数据。", leaderboard_numbers=tuple(numbers))


def ranking_stats(metric: str, filtered: list, filters: list, options: list) -> Card:
    counts = defaultdict(int)
    rates = defaultdict(list)
    for row, spec in filtered:
        name = spec or "未知心法"
        counts[name] += 1
        raw = str(row.get("winRate") or row.get("win_rate") or "").strip()
        if not raw.endswith("%"):
            continue
        try:
            number = float(raw[:-1])
        except ValueError:
            continue
        if 0 <= number <= 100:
            rates[name].append(number)
    if metric == "人数":
        values = [(name, count, f"{count} 人") for name, count in counts.items()]
        maximum = max(counts.values(), default=1)
        note = "统计涵盖全部筛选结果。"
    else:
        values = [(name, sum(entries) / len(entries), f"{sum(entries) / len(entries):.1f}% · {len(entries)} 人")
                  for name, entries in rates.items()]
        maximum = 100
        note = "对榜单公布的角色胜率取算术平均值。"
    bars = Bars(tuple((heart_label(name), value, label) for name, value, label in
                      sorted(values, key=lambda item: (-item[1], item[0]))), maximum)
    return Card(f"排行榜 · 心法{metric}", f"当前筛选共 {len(filtered)} 人", filters,
                bars=bars if values else None, note=note if values else "当前筛选暂无统计数据。",
                menu=ChoiceMenu("请选择要继续查看的内容。", "排行", tuple(options)))


async def ranking_choices(client: DataClient, field_and_query: tuple) -> ChoiceMenu:
    name, query = field_and_query
    if name == "周期":
        _, items = await periods(client)
        choices = [("当前周期", "")] + [(label, cmd) for _, cmd, label in items if cmd]
    elif name == "心法":
        _, entries = await rank_source(client, query, weapons=False)
        names = sorted({rank_heart(row) for row in entries} - {""})
        choices = [("全部心法", "全部心法"), *((heart_label(spec), spec) for spec in names)]
    elif name == "服务器":
        items = payload(await client.get("server-list", ttl=3600)).get("servers") or []
        choices = [("全部服务器", "全部服务器")] + [(row["text"], row["value"]) for row in items]
    else:
        values = {"范围": (50, 100, 200, 500, 1000), "模式": tuple(MODES),
                  "类型": ("全部", "DPS", "奶妈"), "装备": ("全部", "橙武", "非橙武"),
                  "统计": ("", "人数", "平均胜率")}[name]
        choices = [(str(value) or "排行榜", value) for value in values]
    return ChoiceMenu(f"请选择{name}。", "排行", tuple(
        (label, replace(query, **{RANK_FIELDS[name]: value}, page=1)) for label, value in choices
    ))


STRENGTH_PERIODS = {"": "node", "当前": "node", "近7天": "last7d", "近1天": "last1d"}


@dataclass(frozen=True)
class StrengthRecommendation:
    query: Strength
    heart_id: str
    name: str


def strength_request(query: Strength) -> dict:
    return {
        "kungfuType": "tps" if query.role_type == "治疗" else "dps",
        "matchMode": {"组排": "team", "单排": "solo", "全部": "all"}[query.queue],
        "periodType": STRENGTH_PERIODS.get(query.period, query.period),
        "scoreStart": query.lower, "scoreEnd": query.upper,
    }


def score_value(value, default: int) -> int:
    return default if value is None or value == "" else int(value)


def strength_filters(data: dict, query: Strength) -> tuple[list, Strength]:
    filters = data.get("filters") or {}
    used = data.get("params") or {}
    floor = score_value(filters.get("minScore"), 1000)
    ceiling = score_value(filters.get("maxScore"), 3000)
    requested_start = query.lower if query.lower is not None else floor
    requested_end = query.upper if query.upper is not None else ceiling
    if not floor <= requested_start < requested_end <= ceiling:
        raise QueryError(f"原站当前支持 {floor}–{ceiling}，最低分须小于最高分。")
    start = score_value(used.get("scoreStart"), requested_start)
    end = score_value(used.get("scoreEnd"), requested_end)
    expected = strength_request(query)["periodType"]
    period_type = used.get("periodType") or expected
    if period_type != expected:
        raise QueryError("原站当前没有该统计周期，请从强度周期菜单选择。")
    period = next((row["periodName"] for row in filters.get("periods") or []
                   if row.get("periodType") == period_type), "当前周期")
    return ([("周期", period), ("分段", f"{start}–{end}"), ("样本", query.queue), ("心法", query.role_type)],
            replace(query, lower=start, upper=end, period=period_type))


async def strength_periods(client: DataClient, query: Strength) -> ChoiceMenu:
    data = await client.get("rank/jjc-stats", strength_request(replace(query, period="")), ttl=180)
    return strength_period_menu(query, data)


def strength_period_menu(query: Strength, data: dict) -> ChoiceMenu:
    options = tuple((field(row, "periodName"), replace(query, period=row["periodType"]))
                    for row in (data.get("filters") or {}).get("periods") or []
                    if row.get("periodType") and not row.get("disabled"))
    if not options:
        raise QueryError("原站当前没有提供可选强度周期。")
    return ChoiceMenu("请选择强度统计周期。", "强度", options)


async def strength(client: DataClient, query: Strength) -> Card:
    resolved = query
    if query.period not in STRENGTH_PERIODS and query.period not in STRENGTH_PERIODS.values():
        menu = await strength_periods(client, query)
        resolved = next((choice for label, choice in menu.options if label == query.period), None)
        if resolved is None:
            raise QueryError("未知强度统计周期，可用「当前」「近7天」「近1天」或原站的周期全名。")
    data = await client.get("rank/jjc-stats", strength_request(resolved), ttl=180)
    shown_filters, normalized = strength_filters(data, resolved)
    rank = (data.get("rank") or {}).get("data")
    if not isinstance(rank, list):
        raise QueryError("原站没有返回强度数据。")
    rows, recommendations = [], []
    for row in sorted(rank, key=lambda item: item.get("strengthRank", 999)):
        name = heart_name(field(row, "kungfuName"))
        shown = heart_label(name, field(row, "kungfuIcon", ""))
        rows.append([field(row, "strengthRank"), shown, percent(row.get("winRate"), 2), percent(row.get("appearRate"), 2)])
        if row.get("canRecommend") and row.get("kungfuId"):
            recommendations.append((shown, StrengthRecommendation(normalized, str(row["kungfuId"]), name)))
    options = [("打开配置总览", QueryTarget("配置总览", Configuration()))]
    if (data.get("filters") or {}).get("periods"):
        options.append(("切换统计周期", QueryTarget("强度周期", resolved)))
    if recommendations:
        options.append(("选择心法查看推荐阵容",
                        ChoiceMenu("请选择心法。", "强度推荐", tuple(recommendations))))
    return Card("心法强度", "综合强度排序",
                shown_filters,
                ["排名", "心法", "胜率", "出场率"], rows,
                menu=ChoiceMenu("请选择要继续查看的内容。", "强度", tuple(options)),
                note="" if rows else "当前筛选暂无数据。")


async def strength_recommendations(client: DataClient, request: StrengthRecommendation) -> Card:
    params = {**strength_request(request.query), "mode": "team-recommend", "kungfuId": request.heart_id}
    data = await client.get("rank/jjc-stats", params, ttl=180)
    filters, _ = strength_filters(data, request.query)
    teams = (data.get("teams") or {}).get("data")
    if not isinstance(teams, list):
        raise QueryError("原站没有返回推荐阵容。")
    rows = []
    for team in teams:
        names = join_labels(" / ", (heart_label(heart_name(field(heart, "kungfuName")), field(heart, "kungfuIcon", ""))
                                    for heart in team.get("kungfus") or []))
        rows.append([field(team, "rank"), names, percent(team.get("winRate"), 2), percent(team.get("appearRate"), 2)])
    return Card(label_parts(heart_label(request.name), " · 推荐阵容"), "沿用所选强度榜筛选", filters,
                ["排名", "阵容", "胜率", "出场率"], rows,
                menu=ChoiceMenu("可返回强度榜。", "强度", (("返回强度榜", request.query),)),
                note="" if rows else "当前筛选暂无推荐阵容。")


def trend_request(query: TrendQuery, tag: int | None = None) -> dict:
    return {"type": MODES[query.mode], "mode": "detail",
            "granularity": "daily" if query.period == "周内每日" else "weekly", "week_tag": tag}


async def trend_week(client: DataClient, query: TrendQuery, data: dict) -> int:
    weeks = data.get("availableWeeks") or []
    week = str(query.week)
    if week.isascii() and week.isdecimal():
        tag = int(week)
    elif any(week == item.get("startDate") for item in weeks):
        tag = next(item["weekTag"] for item in weeks if week == item.get("startDate"))
    else:
        _, options = await periods(client)
        tag = next((value for value, command, _ in options if command == week), None)
    if tag is None or not any(item.get("weekTag") == tag for item in weeks):
        raise QueryError("原站没有该周的每日走势，可从「切换历史周次」菜单选择。")
    return tag


def trend_compare(query: TrendQuery, entries: list) -> ChoiceMenu:
    picks = query.hearts
    options = [("生成对比图", query)] if picks else []
    for row in entries:
        name = row["kungfu"]
        if name in picks:
            chosen = tuple(heart for heart in picks if heart != name)
            prefix = "移除 "
        elif len(picks) < 10:
            chosen = (*picks, name)
            prefix = "加入 "
        else:
            continue
        options.append((label_parts(prefix, heart_label(name)), (replace(query, hearts=chosen), "对比", "")))
    options.append(("返回走势", replace(query, hearts=())))
    message = join_labels(" / ", map(heart_label, picks)) if picks else "请选择想要对比的心法。"
    return ChoiceMenu(label_parts(f"已选 {len(picks)}/10 · ", message), "分布", tuple(options))


def trend_values(row: dict, metric: str) -> tuple:
    values = tuple(row.get("counts") or ())
    if metric == "人数" or not values or values[0] is None:
        return values if metric == "人数" else (None,) * len(values)
    return tuple(value - values[0] if value is not None else None for value in values)


async def distribution(client: DataClient, args: TrendQuery | tuple) -> Card | ChoiceMenu:
    selection, choice = "", ""
    if isinstance(args, TrendQuery):
        query = args
    elif isinstance(args[0], TrendQuery):
        query, selection, choice = args
    else:
        query = TrendQuery(*args[:2])
        if len(args) > 2:
            selection, choice = args[2:]
    daily = query.period == "周内每日"
    data = payload(await client.get("rank/kungfu-distribution", trend_request(query), ttl=300))
    if daily and query.week:
        tag = await trend_week(client, query, data)
        if tag != data.get("weekTag"):
            data = payload(await client.get("rank/kungfu-distribution", trend_request(query, tag), ttl=300))
        if data.get("weekTag") != tag:
            raise QueryError("原站没有返回所选周的走势。")
        query = replace(query, week=tag)
    entries = data.get("rows")
    if not isinstance(entries, list):
        raise QueryError("原站没有返回心法走势。")
    dates = tuple(data.get("labels") or data.get("dates") or [])
    if selection == "对比":
        return trend_compare(query, entries)
    schools = {}
    for row in entries:
        schools.setdefault(row.get("school") or row["kungfu"], []).append(row)
    if selection == "门派":
        members = schools.get(choice, [])
        if len(members) > 1:
            return ChoiceMenu(f"请选择{choice}的心法。", "分布", tuple(
                (heart_label(row["kungfu"]), replace(query, hearts=(row["kungfu"],))) for row in members
            ))
        if members:
            query = replace(query, hearts=(members[0]["kungfu"],))
    if selection == "心法":
        query = replace(query, hearts=(choice,))

    by_name = {row["kungfu"]: row for row in entries}
    wanted = tuple(dict.fromkeys(heart_name(name) for name in query.hearts))
    missing = [name for name in wanted if name not in by_name]
    if missing:
        raise QueryError(f"原站该周期没有「{'、'.join(missing)}」走势。可从走势图中选择心法。")
    query = replace(query, hearts=wanted)
    shown = [by_name[name] for name in wanted] if wanted else entries[:10]
    axis = "榜单人数" if query.metric == "人数" else "人数变化"
    chart = Trend(dates, tuple((heart_label(row["kungfu"]), trend_values(row, query.metric)) for row in shown), axis)
    options = [(heart_label(school), (query, "门派", school)) for school in schools] if daily else []
    if entries:
        options.append(("对比多个心法", (query, "对比", "")))
    other = "较首日" if query.metric == "人数" else "人数"
    metric = query.metric if daily or query.metric == "人数" else "较首期"
    options.append((f"切换为{'较首期' if not daily and other == '较首日' else other}",
                    replace(query, metric=other)))
    if wanted:
        options.append(("恢复热门心法", replace(query, hearts=())))
    weeks = data.get("availableWeeks") or []
    if daily and weeks:
        variants = [("当前周", replace(query, week="", hearts=()))]
        for week in weeks:
            tag = week.get("weekTag")
            if isinstance(tag, int):
                label = f'第 {tag} 期 · {week.get("startDate", "—")} 至 {week.get("endDate", "—")}'
                variants.append((label, replace(query, week=tag, hearts=())))
        options.append(("切换历史周次", ChoiceMenu("请选择有每日快照的周次。", "分布", tuple(variants))))
    options.append(("切换到历周走势" if daily else "切换到每日走势",
                    TrendQuery(query.mode, "历周走势" if daily else "周内每日")))

    filters = [("指标", metric)]
    if daily and data.get("weekTag"):
        filters.append(("快照期", str(data["weekTag"])))
    if dates:
        filters.append(("样本区间", f"{dates[0]} 至 {dates[-1]}"))
    title = "热门心法每日走势" if daily else "前 1000 心法人数走势"
    if wanted:
        names = join_labels(" / ", map(heart_label, wanted))
        title = label_parts(names, " · 每日走势" if daily else " · 历周走势") if len(wanted) == 1 else "多心法走势对比"
    note = ""
    if len(dates) == 1:
        note = "此周期只有一天的榜单快照。"
    if query.metric != "人数":
        note += f" 以{metric[1:]}人数为基准，缺少基准数据的心法留空。"
    menu = ChoiceMenu("请选择门派、对比心法或切换走势。", "分布", tuple(options))
    if daily:
        return Card(title, f"{query.mode} · Top 1000 榜单", filters, chart=chart, menu=menu, note=note.strip())
    rows = [[str(index), heart_label(row["kungfu"]), Sparkline(tuple(row.get("counts") or ())),
             field(row, "currentCount"), f'{row.get("currentShare", 0):.1f}%', field(row, "prevDelta")]
            for index, row in enumerate(entries, 1) if not wanted or row["kungfu"] in wanted]
    return Card(title, f"{query.mode} · 历周走势", filters, chart=chart, menu=menu, note=note.strip(),
                tables=[Table("门派分布", ["名次", "心法", "历周人数", "人数", "占比", "较上期"], rows)])
