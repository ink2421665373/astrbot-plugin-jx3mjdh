import re
from dataclasses import dataclass, replace

from .cards import Card, Sparkline, Table
from .data import DataClient, field, payload, percent
from .labels import heart_label, join_labels, label_parts, labeled, lineup_label
from .matches import Role, match_samples
from .query import (
    CONFIG_MATCHES, CONFIG_ORDERS, CONFIG_RANGES, CONFIG_TYPES,
    ChoiceMenu, Configuration, QueryError, QueryTarget, Strength,
)
from .rankings import HEART_NAMES, HEALERS, TANKS, heart_name, strength, update_time


SHORTCUTS = {
    "剑气花": ("剑纯", "气纯", "奶花"), "策藏秀": ("天策", "藏剑", "奶秀"),
    "剑苍花": ("剑纯", "苍云", "奶花"), "明歌药": ("喵子", "莫问", "奶药"),
    "气花秀": ("气纯", "花间", "奶秀"),
}
COMP_ALIASES = {**HEART_NAMES, **{name: name for name in TANKS},
                "明教": "喵子", "焚影": "喵子", "五毒": "毒经", "长歌": "莫问",
                "少林": "和尚", "大师": "和尚", "鲸鱼": "惊羽", "药宗": "无方", "离经": "奶花",
                "补天": "奶毒", "云裳": "奶秀", "剑": "剑纯", "气": "气纯", "花": "奶花",
                "秀": "奶秀", "毒": "奶毒", "药": "奶药", "歌": "奶歌", "策": "天策",
                "藏": "藏剑", "苍": "苍云", "霸": "霸刀", "明": "喵子", "螺": "田螺", "鲸": "惊羽"}
COMP_TOKENS = re.compile("|".join(map(re.escape, sorted(COMP_ALIASES, key=len, reverse=True))))
SORT = dict(zip(CONFIG_ORDERS, ("best", "score", "win_rate", "matches")))


@dataclass(frozen=True)
class FixedTeam:
    query: Configuration
    identity: str
    member: tuple[str, str, str] | None = None


def parse_lineup(expression: str) -> tuple[str, ...]:
    if expression in SHORTCUTS:
        return SHORTCUTS[expression]
    merged = re.sub(r"[\s,，、/+|\-]+", "", expression)
    tokens = COMP_TOKENS.findall(merged)
    if not 1 <= len(tokens) <= 3 or "".join(tokens) != merged:
        raise QueryError("未识别阵容。请用阵容简称或填写空格分隔的心法名。")
    names = [COMP_ALIASES[token] for token in tokens]
    ambiguous = {"花": "花间", "秀": "冰心", "毒": "毒经", "药": "无方", "歌": "莫问"}
    return tuple(ambiguous.get(token, name) if any(other in HEALERS for other in names[i + 1:]) else name
                 for i, (token, name) in enumerate(zip(tokens, names)))


def team_names(values) -> str:
    names = [heart_name(value) for value in values if value]
    return " ".join(sorted(names, key=lambda name: name in HEALERS))


def choices_for(query: Configuration, key: str, title: str, options) -> ChoiceMenu:
    return ChoiceMenu(f"请选择{title}。", "配置总览",
                      tuple((label, replace(query, **{key: value})) for label, value in options))


def scope_menu(query: Configuration) -> ChoiceMenu:
    return choices_for(query, "pool", "样本范围", ((f"Top {limit}", limit) for limit in CONFIG_RANGES))


async def get_builds(client: DataClient, query: Configuration, mode: str, **filters) -> dict:
    data = await client.get("rank/builds", {"mode": mode, "scope": f"top{query.pool}", **filters}, ttl=300)
    if data.get("error"):
        raise QueryError("原站配置数据暂不可用。")
    return data


def stat_rows(data: dict) -> list[dict]:
    stats = data.get("stats")
    if not isinstance(stats, list):
        raise QueryError("原站没有返回配置数据。")
    return stats


async def configuration(client: DataClient, query: Configuration) -> Card:
    if query.search:
        return await fixed_teams(client, query)
    selected = heart_name(query.school)
    if selected and query.role_type != "全部" and (selected in HEALERS) != (query.role_type == "治疗"):
        raise QueryError("所选心法与当前心法类型筛选不匹配。")
    if selected:
        detail = await get_builds(client, query, "detail", kungfu=selected)
        record = next((row for row in stat_rows(detail) if row.get("kungfu") == selected), None)
        if record is None:
            raise QueryError(f"当前范围没有「{query.school}」的配置。")
        return heart_configuration(record, query, detail)
    data = await get_builds(client, query, "summary")
    stats = stat_rows(data)
    records = stats
    if query.role_type != "全部":
        records = [row for row in stats if (row["kungfu"] in HEALERS) == (query.role_type == "治疗")]
    rows = []
    for row in records:
        top = row.get("top_comps") or []
        lineup = team_names(top[0].get("schools", [])) if top else ""
        rows.append([heart_label(row["kungfu"]), field(row, "player_count"), field(row, "sample_count"),
                     percent(row.get("win_rate"), 0), lineup_label(lineup) if lineup else "—"])
    filters = summary_filters(query, data)
    if stats:
        filters += [
            ("总样本", str(sum(row.get("sample_count", 0) for row in stats)) + " 场"),
            ("总玩家", str(sum(row.get("player_count", 0) for row in stats)) + " 人"),
            ("平均胜率", percent(sum(row.get("win_rate", 0) for row in stats) / len(stats), 0)),
            ("心法数量", f"{len(stats)} 种"),
        ]
    options = [
        ("切换样本范围", scope_menu(query)),
        ("切换心法类型", choices_for(query, "role_type", "心法类型", ((value, value) for value in CONFIG_TYPES))),
    ]
    if records:
        options.append(("按心法查看配队和奇穴", ChoiceMenu("请选择心法。", "配置总览", tuple(
            (heart_label(row["kungfu"]), replace(query, school=row["kungfu"])) for row in records
        ))))
    options.append(("快捷检索阵容", ChoiceMenu("请选择要检索的阵容。", "配置总览", tuple(
        (label_parts(name, " · ", lineup_label(" ".join(hearts))), replace(query, search=name))
        for name, hearts in SHORTCUTS.items()
    ))))
    return Card("配置总览", "名剑大会 3v3 组排", filters,
                tables=[Table("心法配置", ["心法", "玩家数", "对局场次", "胜率", "首位配队"], rows)],
                menu=ChoiceMenu("请选择要查看的内容。", "配置总览", tuple(options)),
                note="配置统计基于 3v3 固定队样本。")


def summary_filters(query, data) -> list:
    return [("样本范围", f"Top {query.pool}"), ("类型", query.role_type),
            ("更新于", update_time(data.get("updated_at", "")))]


def talent_groups(entries, tag: str = "") -> list:
    groups = []
    for index, row in enumerate(entries, 1):
        icons = row.get("talent_icons") or [{"name": name, "icon": ""} for name in row.get("talent_names") or []]
        if icons:
            name = f"{tag} {index}" if tag else ("常用" if index == 1 else f"备选 {index - 1}")
            groups.append((f'{name} · {row["count"]} 场 · 占比 {percent(row.get("rate"), 0)} · '
                           f'胜率 {percent(row.get("win_rate"), 0)}', icons))
    return groups


def global_talents(comps) -> list:
    aggregate = {}
    for comp in comps:
        for row in comp.get("top_talents") or []:
            entry = aggregate.setdefault(row["talent_key"], {**row, "count": 0, "wins": 0})
            count = row.get("count") or 0
            entry["count"] += count
            entry["wins"] += round((row.get("win_rate") or 0) * count)
    total = sum(row["count"] for row in aggregate.values())
    ordered = sorted((row for row in aggregate.values() if row["count"]), key=lambda row: -row["count"])
    return [{**row, "win_rate": row["wins"] / row["count"], "rate": row["count"] / total} for row in ordered[:5]]


def heart_configuration(record: dict, query: Configuration, data: dict) -> Card:
    name = record["kungfu"]
    filters = summary_filters(query, data) + [
        ("玩家数", field(record, "player_count")), ("场次", field(record, "sample_count")),
        ("胜率", percent(record.get("win_rate"), 0)),
    ]
    comps = record.get("top_comps") or []
    rows, options = [], []
    for index, team in enumerate(comps, 1):
        lineup = team_names(team.get("schools") or [])
        if lineup:
            shown = lineup_label(lineup)
            rows.append([str(index), shown, field(team, "count"), percent(team.get("win_rate"), 0),
                         percent(team.get("rate"), 0)])
            options.append((shown, replace(query, search=lineup, school="")))
    options.append(("返回配置总览", replace(query, school="")))
    return Card(label_parts(heart_label(name), " · 配置详情"), "组排常见配队与跨阵容热门奇穴", filters,
                tables=[Table("热门队伍配置", ["名次", "阵容", "场次", "胜率", "占比"], rows)],
                groups=talent_groups(global_talents(comps), "全局奇穴"),
                menu=ChoiceMenu("请选择阵容，继续查看固定队与奇穴。", "配置总览", tuple(options)))


async def search(client: DataClient, query: Configuration):
    names = parse_lineup(query.search)
    data = await get_builds(client, query, "comp-search", comp=",".join(names), sort=SORT[query.order],
                            min_matches=query.minimum, limit=8)
    return names, data


async def fixed_teams(client: DataClient, query: Configuration) -> Card:
    names, result = await search(client, query)
    summary = result.get("summary") or {}
    filters = [("范围", f"Top {query.pool}"), ("最少场次", f"{query.minimum} 场"), ("排序", query.order)]
    if summary:
        filters.extend([("固定队", field(summary, "team_count")), ("样本场次", field(summary, "match_count")),
                        ("整体胜率", percent(summary.get("win_rate"), 0))])
        if summary.get("best_rank"):
            filters.append(("最高名次", f'第 {summary["best_rank"]} 名'))
    rows, team_choices = [], []
    for index, team in enumerate(result.get("teams") or [], 1):
        members = {}
        for member in team.get("members") or []:
            key = (member.get("role_name") or "未知角色", member.get("server"), member.get("kungfu") or "")
            members[key] = labeled(key[0], key[2])
        wins = team.get("win_count") or 0
        count = team.get("match_count") or 0
        shown = join_labels(" / ", members.values())
        rows.append([str(index), shown, field(team, "best_rank"),
                     percent(team.get("win_rate"), 0), f"{wins}胜 {max(0, count-wins)}负", str(count)])
        if team.get("team_identity_key"):
            team_choices.append((label_parts(f"第 {index} 队 · ", shown),
                                 QueryTarget("固定队详情", FixedTeam(query, team["team_identity_key"]))))
    options = [
        ("切换样本范围", scope_menu(query)),
        ("调整最少组排场次", choices_for(query, "minimum", "最少组排场次", ((f"≥{n} 场", n) for n in CONFIG_MATCHES))),
        ("调整排序方式", choices_for(query, "order", "队伍排序方式", ((mode, mode) for mode in CONFIG_ORDERS))),
    ]
    lineup = team_names(names)
    matched = result.get("matched_comps") or []
    variants = []
    for item in matched:
        team = team_names(item.get("schools") or [])
        if team and team != lineup:
            variants.append((lineup_label(team), replace(query, search=team)))
    if variants:
        options.append(("选择匹配的完整阵容", ChoiceMenu("请选择阵容。", "配置总览", tuple(variants))))
    talent_choices = tuple(
        (heart_label(row["kungfu"]), QueryTarget("配队奇穴", (lineup, row["kungfu"], query)))
        for row in result.get("per_kungfu") or [] if row.get("top_talents")
    )
    if talent_choices:
        options.append(("查看这套阵容的通用奇穴", ChoiceMenu("请选择心法。", "配置总览", talent_choices)))
    return Card("配置检索", lineup_label(lineup), filters,
                tables=[Table("组排固定队排行", ["序号", "队伍成员", "最高名次", "胜率", "胜负", "场次"], rows,
                              f"展示前 {len(rows)} 支固定队" if rows else "")],
                menu=ChoiceMenu("请选择固定队或筛选项。", "配置总览", tuple(team_choices + options)),
                note="" if rows else "当前筛选暂无固定队排名。")


def member_key(member: dict) -> tuple[str, str, str]:
    return (field(member, "role_name", ""), field(member, "server", ""), field(member, "kungfu", ""))


async def fixed_team(client: DataClient, request: FixedTeam) -> Card:
    names, result = await search(client, request.query)
    team = next((row for row in result.get("teams") or [] if row.get("team_identity_key") == request.identity), None)
    if team is None:
        raise QueryError("原站固定队样本已更新，请重新检索阵容。")
    members = team.get("members") or []
    filters = [("范围", f"Top {request.query.pool}"), ("场次", field(team, "match_count")),
               ("胜场", field(team, "win_count")), ("胜率", percent(team.get("win_rate"))),
               ("最高名次", field(team, "best_rank"))]
    sample_ids = team.get("sample_match_ids") or []
    if request.member:
        member = next((row for row in members if member_key(row) == request.member), None)
        if member is None:
            raise QueryError("该角色已不在原站当前固定队样本中。")
        name, server, heart = request.member
        talents = member.get("top_talents") or []
        groups = talent_groups(talents)
        role = Role(name, server, "3v3", global_id=field(member, "global_role_id", ""))
        options = [("返回固定队", replace(request, member=None))]
        if name and server:
            options.insert(0, (label_parts("查看 ", labeled(name, heart), " 的战绩"), QueryTarget("战绩", role)))
        for index, item in enumerate(talents, 1):
            samples = match_samples(item.get("sample_match_ids") or [])
            if samples:
                options.append((f"奇穴方案 {index} 的实战样例", samples))
        return Card(label_parts(labeled(name, heart), " · 固定队奇穴"), server, filters, groups=groups,
                    menu=ChoiceMenu("请选择战绩、对局样例或返回队伍。", "固定队详情", tuple(options)),
                    note="" if groups else "暂无该成员的奇穴样本。")
    rows, options = [], []
    for member in members:
        name, server, heart = member_key(member)
        character = labeled(name or "未知角色", heart)
        rows.append([character, server or "—", heart_label(heart), field(member, "match_count"),
                     str(len(member.get("top_talents") or []))])
        options.append((label_parts(character, " · 查看奇穴和战绩"),
                        replace(request, member=member_key(member))))
    samples = match_samples(sample_ids)
    if samples:
        options.append(("查看代表对局", samples))
    options.append(("返回固定队排行", QueryTarget("配置总览", request.query)))
    return Card("固定队详情", lineup_label(team_names(names)), filters,
                ["角色", "服务器", "心法", "场次", "奇穴方案"], rows,
                menu=ChoiceMenu("请选择成员或代表对局。", "固定队详情", tuple(options)))


async def team_detail(client: DataClient, args: tuple) -> Card:
    lineup, heart, *settings = args
    query = settings[0] if settings else Configuration(search=lineup)
    _, result = await search(client, query)
    row = next((item for item in result.get("per_kungfu") or [] if item["kungfu"] == heart), {})
    groups = talent_groups(row.get("top_talents") or [])
    if not groups:
        raise QueryError(f"原站 Top {query.pool} 当前没有「{heart}」在该阵容的奇穴统计。")
    summary = result.get("summary") or {}
    return Card(label_parts(heart_label(heart), " · 配队奇穴"), lineup_label(lineup),
                [("范围", f"Top {query.pool}"), ("最少场次", f"{query.minimum} 场"),
                 ("固定队", field(summary, "team_count")), ("固定队场次", field(summary, "match_count"))],
                groups=groups)


async def team_rows(client: DataClient) -> list:
    teams = {}
    data = await get_builds(client, Configuration(), "summary")
    for row in stat_rows(data):
        for comp in row.get("top_comps") or []:
            key = comp.get("comp_key")
            if key and comp.get("schools") and (key not in teams or comp["count"] > teams[key]["count"]):
                teams[key] = comp
    return sorted(teams.values(), key=lambda comp: (-comp["count"], -comp["win_rate"]))[:5]


def team_options(teams: list) -> ChoiceMenu:
    selections = []
    for team in teams:
        lineup = team_names(team["schools"])
        shown = lineup_label(lineup)
        hearts = tuple((heart_label(name), (lineup, name)) for name in dict.fromkeys(lineup.split()))
        menu = ChoiceMenu(label_parts(shown, " · 请选择心法。"), "配队奇穴", hearts)
        selections.append((label_parts(shown, f' · {percent(team["win_rate"], 0)} · {team["count"]} 场'), menu))
    return ChoiceMenu("请选择要查看奇穴的队伍。", "配队奇穴", tuple(selections))


def popular_team_table(teams: list) -> Table:
    return Table("热门 3v3 配队 Top 5", ["阵容", "胜率", "样本场次"],
                 [[lineup_label(team_names(team["schools"])), percent(team.get("win_rate"), 0),
                   field(team, "count")] for team in teams], "前 200 名近期组排样本")


async def popular_teams(client: DataClient) -> Card:
    rows = await team_rows(client)
    return Card("热门配队", tables=[popular_team_table(rows)], menu=team_options(rows) if rows else None)


async def environment(client: DataClient, args: tuple) -> Card:
    queue, = args
    preset = Strength(queue=queue, lower=2200)
    strong = await strength(client, preset)
    teams = await team_rows(client)
    dist = payload(await client.get("rank/kungfu-distribution", {"type": "3c", "mode": "summary", "limit": 5}, ttl=300))
    dist_rows = [[str(i), heart_label(row["kungfu"]), Sparkline(tuple(row.get("counts") or [])),
                  field(row, "count"), f'{row.get("share", 0):.1f}%']
                 for i, row in enumerate(dist.get("topKungfus") or [], 1)]
    other = "单排" if queue == "组排" else "组排"
    options = [
        (f"切换强度榜至{other}", (other,)), ("查看完整输出心法强度", QueryTarget("强度", preset)),
    ]
    if teams:
        options.append(("选择热门配队查看奇穴", team_options(teams)))
    options += [
        ("查看 3v3 历周分布", QueryTarget("分布", ("3v3", "历周走势"))),
        ("查看 3v3 每日走势", QueryTarget("每日走势", ("3v3", "周内每日"))),
    ]
    return Card("本周 PVP 环境速览", "名剑大会高分段", tables=[
        Table("强势输出心法 Top 5", strong.columns, strong.rows[:5], f"2200+ 分段 · {queue}"),
        popular_team_table(teams),
        Table("3v3 前 1000 门派分布", ["名次", "心法", "历周走势", "人数", "占比"], dist_rows),
    ], menu=ChoiceMenu("请选择要继续查看的内容。", "环境", tuple(options)))
