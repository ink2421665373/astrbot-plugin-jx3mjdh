import re
from dataclasses import dataclass, replace
from itertools import zip_longest

from .aliases import NameAliases
from .cards import Card, Table
from .configurations import get_builds, parse_lineup, search, talent_groups, team_names
from .data import ApiError, DataClient, field, percent
from .labels import heart_label, join_labels, label_parts, lineup_label
from .matches import MatchQuery, RecentMatch, match_payload
from .query import CONFIG_RANGES, ChoiceMenu, Configuration, QueryError, QueryTarget
from .rankings import KNOWN_HEARTS, heart_name


@dataclass(frozen=True)
class Matchup:
    pool: int = 1000
    own: tuple[str, ...] = ()
    enemy: tuple[str, ...] = ()
    building: bool = False
    talent: str = ""


def parse_matchup(args: list[str], aliases: NameAliases) -> Matchup:
    pool = 1000
    if args and args[0].lower().startswith("top"):
        raw = args.pop(0)[3:]
        if raw not in {str(value) for value in CONFIG_RANGES}:
            raise QueryError("对位范围可填 Top50、Top200 或 Top1000。")
        pool = int(raw)
    if not args:
        return Matchup(pool)
    if args[0] in {"阵容", "模拟"}:
        if len(args) == 1:
            return Matchup(pool, building=True)
        sides = re.split(r"\s+(?:vs|对阵)\s+", " ".join(args[1:]), flags=re.IGNORECASE)
        if len(sides) == 2:
            own, enemy = (parse_lineup(aliases.lineup(side)) for side in sides)
            if len(own) == len(enemy) == 3:
                return Matchup(pool, own, enemy)
        raise QueryError("用法为 /剑三 对位 阵容 剑气花 vs 苍云 蓬莱 奶毒，双方各三个心法。")
    names = tuple(heart_name(aliases.heart(name)) for name in args)
    if len(names) > 2 or any(name not in KNOWN_HEARTS for name in names):
        raise QueryError("请填写一个或两个心法，例如 /剑三 对位 奶花 苍云。")
    return Matchup(pool, names[:1], names[1:])


def lineup_picker(query: Matchup) -> ChoiceMenu:
    side = "我方" if len(query.own) < 3 else "敌方"
    current = query.own if side == "我方" else query.enemy
    options = []
    for name in sorted(KNOWN_HEARTS):
        changed = replace(query, **{"own" if side == "我方" else "enemy": (*current, name)})
        if len(changed.enemy) == 3:
            changed = replace(changed, building=False)
        options.append((heart_label(name), changed))
    prompt = f"请选择{side}第 {len(current) + 1} 个心法。"
    chosen = join_labels(" · ", (lineup_label(team_names(names)) for names in (query.own, query.enemy) if names))
    if chosen:
        prompt = label_parts(prompt, " 已选 ", chosen)
    return ChoiceMenu(prompt, "对位", tuple(options))


def matchup_filters(query: Matchup, data: dict) -> list:
    return [("赛制", "3v3 组排"), ("样本范围", f"Top {query.pool}"),
            ("样本场次", field(data, "sample_count")), ("我方胜率", percent(data.get("win_rate")))]


async def matchup_data(client: DataClient, query: Matchup) -> dict:
    mode = "simulate" if len(query.own) == 3 else "simulate-pair"
    return await get_builds(client, Configuration(pool=query.pool), mode,
                            my=",".join(query.own), enemy=",".join(query.enemy))


def facing_label(query: Matchup):
    return label_parts(lineup_label(team_names(query.own)), "  对阵  ", lineup_label(team_names(query.enemy)))


async def matchup(client: DataClient, query: Matchup) -> Card | ChoiceMenu:
    if query.building:
        return lineup_picker(query)
    if not query.own:
        names = tuple((heart_label(name), replace(query, own=(name,))) for name in sorted(KNOWN_HEARTS))
        options = [
            ("单心法对位", ChoiceMenu("请选择我方心法。", "对位", names)),
            ("完整 3v3 阵容模拟", replace(query, building=True)),
            ("切换样本范围", ChoiceMenu("请选择样本范围。", "对位", tuple(
                (f"Top {pool}", replace(query, pool=pool)) for pool in CONFIG_RANGES
            ))),
        ]
        return ChoiceMenu("请选择对位分析方式。", "对位", tuple(options))
    if not query.enemy:
        name = query.own[0]
        data = await get_builds(client, Configuration(pool=query.pool), "matchup", group="kungfu", kungfu=name)
        opponents = data.get("rows")
        if not isinstance(opponents, list):
            raise QueryError("原站没有返回心法对位统计。")
        options = []
        rows = []
        for index, row in enumerate(opponents, 1):
            label = heart_label(row["enemy"])
            rows.append([str(index), label, field(row, "sample_count"), percent(row.get("win_rate"))])
            options.append((label, replace(query, enemy=(row["enemy"],))))
        return Card(label_parts(heart_label(name), " · 心法对位"), "3v3 组排",
                    [("样本范围", f"Top {query.pool}")], ["序号", "敌方心法", "场次", "我方胜率"], rows,
                    menu=ChoiceMenu("请选择敌方心法查看详情。", "对位", tuple(options)) if options else None,
                    note="少量场次的胜率波动较大。" if rows else "暂无对位样本。")
    data = await matchup_data(client, query)
    total = data.get("sample_count") or 0
    filters = matchup_filters(query, data)
    options = []
    tables = []
    groups = []
    if total:
        options.append(("查公开对局样例", QueryTarget("对位样例", replace(query, talent=""))))
        for key, title in (("my_comps", "我方常见阵容"), ("enemy_comps", "敌方常见阵容")):
            comps = data.get(key) or []
            if comps:
                tables.append(Table(title, ["阵容", "场次", "占比"],
                                    [[lineup_label(team_names(row.get("schools") or [])), field(row, "count"),
                                      percent(row.get("rate"))] for row in comps]))
        choices = []
        for row in data.get("per_kungfu") or []:
            name = row["kungfu"]
            if not row.get("top_talents"):
                continue
            if name == query.talent:
                groups = talent_groups(row["top_talents"])
            else:
                choices.append((heart_label(name), replace(query, talent=name)))
        if choices:
            options.append(("查看对位奇穴", ChoiceMenu("请选择我方心法。", "对位", tuple(choices))))
    if len(query.own) == 1:
        options.append(("返回对手列表", replace(query, enemy=(), talent="")))
    options.append(("返回对位菜单", Matchup(query.pool)))
    note = "暂无符合该阵容的对局样本。" if not total else (
        "此组合样本不足。胜率来自历史对局。" if not data.get("sufficient") else "胜率来自历史对局。"
    )
    title = label_parts(heart_label(query.talent), " · 对位奇穴") if query.talent else "3v3 对位详情"
    return Card(title, facing_label(query), filters, tables=tables, groups=groups, note=note,
                menu=ChoiceMenu("请选择样例、奇穴或返回。", "对位", tuple(options)))


def side_hearts(side: dict) -> tuple[str, ...]:
    return tuple(sorted(heart_name(player.get("career") or "") for player in side.get("players") or []))


def matches_side(requested: tuple[str, ...], actual: tuple[str, ...]) -> bool:
    return len(actual) == 3 and (requested[0] in actual if len(requested) == 1 else tuple(sorted(requested)) == actual)


async def matchup_samples(client: DataClient, query: Matchup) -> Card:
    data = await matchup_data(client, query)
    if len(query.own) == 3:
        own_lineups, enemy_lineups = (query.own,), (query.enemy,)
    else:
        own_lineups = [item["schools"] for item in (data.get("my_comps") or [])[:1]]
        enemy_lineups = [item["schools"] for item in (data.get("enemy_comps") or [])[:1]]

    async def candidate_ids(lineups) -> dict:
        lists = []
        for lineup in lineups:
            _, builds = await search(client, Configuration(pool=query.pool, search=team_names(lineup)))
            lists.extend(team.get("sample_match_ids") or [] for team in builds.get("teams") or [])
        return dict.fromkeys(str(value) for group in zip_longest(*lists) for value in group
                             if value is not None and re.fullmatch(r"[0-9]{1,24}", str(value)))

    own_ids = await candidate_ids(own_lineups)
    enemy_ids = await candidate_ids(enemy_lineups)
    shared = (value for value in own_ids if value in enemy_ids)
    alternated = (value for group in zip_longest(own_ids, enemy_ids) for value in group if value is not None)
    candidates = dict.fromkeys((*shared, *alternated))
    rows, options = [], []
    checked = 0
    limited = False
    for match_id in list(candidates)[:10]:
        try:
            detail = await match_payload(client, "3v3", match_id)
        except ApiError as exc:
            if exc.status == 429:
                limited = True
                break
            continue
        except QueryError:
            continue
        checked += 1
        teams = detail.get("teams") or []
        if len(teams) != 2:
            continue
        for own, enemy in (teams, teams[::-1]):
            first, second = side_hearts(own), side_hearts(enemy)
            if not matches_side(query.own, first) or not matches_side(query.enemy, second):
                continue
            game = RecentMatch(match_id, field(detail, "start_time"), field(detail, "duration"), "—")
            arena = field(detail, "map")
            role_result = {"win": "胜", "lose": "负", "loss": "负", "draw": "平"}.get(own.get("result"), "—")
            rows.append([game.time, lineup_label(team_names(first)), lineup_label(team_names(second)), role_result])
            options.append((f"{game.time} · {arena}", QueryTarget("对局详情", MatchQuery("3v3", game))))
            break
    options.append(("返回对位详情", query))
    note = "每次最多核对 10 场代表对局。"
    if not rows:
        note += " 这批公开对局中暂无可核实的同对位样例。"
    if limited:
        note += " 当前查询受限，稍后可再试。"
    filters = [("已核对", f"{checked}/{min(len(candidates), 10)} 场"), ("匹配", f"{len(rows)} 场")]
    return Card("公开对位样例", facing_label(query), filters, tables=[
        Table("实战对局", ["时间", "我方阵容", "敌方阵容", "我方结果"], rows)
    ], note=note, menu=ChoiceMenu("请选择实战对局或返回统计。", "对位", tuple(options)))
