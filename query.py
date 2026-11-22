import re
from collections.abc import Container
from dataclasses import dataclass
from datetime import date

from .labels import HEART_NAMES, TANKS, IconLabel, alias_key


class QueryError(ValueError):
    pass


@dataclass(frozen=True)
class QueryTarget:
    action: str
    params: object


@dataclass(frozen=True)
class ChoiceMenu:
    prompt: str
    action: str
    options: tuple[tuple[str, object], ...]

    def has_icons(self) -> bool:
        return isinstance(self.prompt, IconLabel) or any(isinstance(label, IconLabel) for label, _ in self.options)

    def text(self) -> str:
        choices = "\n".join(
            f"{number}. {label}" for number, (label, _) in enumerate(self.options, 1)
        )
        return f"{self.prompt}\n{choices}\n\n请在 2 分钟内直接回复序号。"


MODES = {"22": "2v2", "33": "3v3", "55": "5v5", "2v2": "2v2", "3v3": "3v3", "5v5": "5v5"}
REVIEW_SUMMARY = "复盘汇总"
CHINESE_NUMBERS = dict(zip("一二三四五六七八九", range(1, 10)))
PERIOD = re.compile(r"(?P<season>[^\s=]+)第(?P<week>[0-9]+)周")
DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
DATE_RANGE = re.compile(rf"({DATE})?[~～至]({DATE})?")
RANK_LIMITS = {"50", "100", "200", "500", "1000"}
HISTORY_QUEUES = {"全部", "组排", "散排", "未知", "单排"}
HEART_SEPARATOR = re.compile(r"[,，、]+")


def is_heart_name(value: str, aliases: Container[str]) -> bool:
    return value in HEART_NAMES or value in TANKS or alias_key(value) in aliases


@dataclass(frozen=True)
class Ranking:
    period: str = ""
    school: str = "全部心法"
    server: str = "全部服务器"
    limit: int = 200
    mode: str = "3v3"
    role_type: str = "全部"
    weapon: str = "全部"
    page: int = 1
    metric: str = ""


@dataclass(frozen=True)
class Strength:
    role_type: str = "输出"
    queue: str = "组排"
    lower: int | None = None
    upper: int | None = None
    period: str = ""


@dataclass(frozen=True)
class TrendQuery:
    mode: str = "3v3"
    period: str = "历周走势"
    week: str | int = ""
    metric: str = "人数"
    hearts: tuple[str, ...] = ()


@dataclass(frozen=True)
class HistoryFilter:
    start: str = ""
    end: str = ""
    limit: int = 10
    queue: str = "全部"


def parse_history(args: list[str], *, review: bool = False) -> tuple[list[str], HistoryFilter]:
    options = {}
    positional = []
    fields = {"日期", "起始", "结束", "场数"}
    if review:
        fields.add("排队")
    for token in args:
        key, sep, value = token.replace("＝", "=", 1).partition("=")
        if not sep:
            if re.fullmatch(DATE, token):
                key, value = "日期", token
            elif (span := DATE_RANGE.fullmatch(token)) and any(span.groups()):
                for field, bound in zip(("起始", "结束"), span.groups()):
                    if bound:
                        if field in options:
                            raise QueryError("请只填写一个日期区间。")
                        options[field] = bound
                continue
            elif token.endswith("场") and token[:-1].isascii() and token[:-1].isdecimal():
                key, value = "场数", token[:-1]
            elif review and token in HISTORY_QUEUES:
                key, value = "排队", token
            else:
                positional.append(token)
                continue
        if key not in fields or key in options or not value:
            raise QueryError(f"无法识别或重复的战绩参数「{token}」。")
        options[key] = value
    if "日期" in options and ("起始" in options or "结束" in options):
        raise QueryError("请选择单日或日期区间，例如 2026-11-20 或 2026-11-15~2026-11-20。")
    start = options.get("日期") or options.get("起始", "")
    end = options.get("日期") or options.get("结束", "")
    for value in (start, end):
        if value:
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError
            except ValueError:
                raise QueryError("日期请写成 YYYY-MM-DD，例如 2026-11-20。") from None
    if start and end and start > end:
        raise QueryError("起始日期须早于或等于结束日期。")
    limit = options.get("场数", "10")
    if not re.fullmatch(r"[1-9][0-9]?", limit) or int(limit) > 50:
        raise QueryError("场数可填 1–50，例如「30场」。")
    queue = options.get("排队", "全部")
    if queue == "单排":
        queue = "散排"
    if queue not in {"全部", "组排", "散排", "未知"}:
        raise QueryError("排队可填「全部」「组排」「散排」或「未知」。")
    return positional, HistoryFilter(start, end, int(limit), queue)


def parse_trend(args: list[str], daily: bool = False, *, heart_aliases: Container[str] = ()) -> TrendQuery:
    params = {}
    for token in args:
        key, sep, value = token.replace("＝", "=", 1).partition("=")
        if not sep:
            value = token
            if token.lower() in MODES:
                key, value = "模式", MODES[token.lower()]
            elif not daily and token in {"周内每日", "历周走势"}:
                key = "走势"
            elif token in {"人数", "较首日"}:
                key = "指标"
            elif PERIOD.fullmatch(token) or re.fullmatch(DATE, token) or re.fullmatch(r"[0-9]+期?", token):
                key, value = "周", token.removesuffix("期")
            elif all(is_heart_name(name, heart_aliases) for name in HEART_SEPARATOR.split(token)):
                key = "心法"
            else:
                raise QueryError(f"无法识别走势参数「{token}」。请填写心法、赛制或周次。")
        elif key not in {"周", "指标", "心法"}:
            raise QueryError(f"无法识别走势参数「{token}」。")
        if not value or key in params:
            raise QueryError(f"无法识别或重复的走势参数「{token}」。")
        params[key] = value
    mode = params.get("模式", "3v3")
    period = params.get("走势", "周内每日" if daily else "历周走势")
    week = params.get("周", "")
    if week and period != "周内每日":
        raise QueryError("「周」筛选用于周内每日走势。")
    metric = params.get("指标", "人数")
    if metric not in {"人数", "较首日"}:
        raise QueryError("指标可填「人数」或「较首日」。")
    hearts = tuple(dict.fromkeys(HEART_SEPARATOR.split(params["心法"]))) if "心法" in params else ()
    if any(not name for name in hearts) or len(hearts) > 10:
        raise QueryError("请用逗号分隔心法，最多选 10 个。")
    return TrendQuery(mode, period, week, metric, hearts)


CONFIG_RANGES = (50, 200, 1000)
CONFIG_MATCHES = (2, 4, 6)
CONFIG_ORDERS = ("综合最优", "分数最高", "胜率最高", "场次最多")
CONFIG_TYPES = ("全部", "输出", "治疗")


@dataclass(frozen=True)
class Configuration:
    pool: int = 200
    role_type: str = "全部"
    school: str = ""
    search: str = ""
    minimum: int = 2
    order: str = "综合最优"


def parse_configuration(args: list[str], *, heart_aliases: Container[str] = ()) -> Configuration:
    lookup = bool(args and args[0] == "检索")
    values = {}
    names = []
    for arg in (args[1:] if lookup else args):
        token = arg.replace("＝", "=", 1)
        tag, sep, value = token.partition("=")
        pool = re.fullmatch(r"(?:top)?(50|200|1000)", token, flags=re.IGNORECASE)
        if pool:
            key, option = "pool", int(pool[1])
        elif not lookup and token in (*CONFIG_TYPES, "DPS", "奶妈"):
            key, option = "role_type", {"DPS": "输出", "奶妈": "治疗"}.get(token, token)
        elif not lookup and (is_heart_name(token, heart_aliases) or tag == "心法" and value):
            key, option = "school", value if sep else token
        elif lookup and (tag == "场次" or not sep and token.isdecimal()):
            minimum = value if sep else token
            if minimum not in {str(n) for n in CONFIG_MATCHES}:
                raise QueryError("固定队的最少场次可填 2、4 或 6。")
            key, option = "minimum", int(minimum)
        elif lookup and (tag == "排序" and value in CONFIG_ORDERS or token in CONFIG_ORDERS):
            key, option = "order", value if sep else token
        elif lookup and not sep:
            names.append(token)
            continue
        else:
            raise QueryError(f"无法识别配置总览参数「{arg}」。请查看 /名剑帮助。")
        if key in values:
            raise QueryError("同一个配置筛选项只能填写一次。")
        values[key] = option
    if lookup:
        if not names:
            raise QueryError("请填写阵容简称或三个心法，例如 /剑三 配置总览 检索 剑气花。")
        values["search"] = " ".join(names)
        values.setdefault("pool", 1000)
    return Configuration(**values)


def parse_ranking(
    args: list[str], *, heart_aliases: Container[str] = (), server_aliases: Container[str] = (),
) -> Ranking:
    values = {}
    for arg in args:
        key, sep, value = arg.replace("＝", "=").partition("=")
        if not sep:
            if PERIOD.fullmatch(arg):
                key, value = "周期", arg
            elif arg.lower() in MODES:
                key, value = "模式", arg
            elif (limit := arg.lower().removeprefix("top")) in RANK_LIMITS:
                key, value = "范围", limit
            elif arg.lower() in {"dps", "奶妈"}:
                key, value = "类型", arg
            elif arg in {"橙武", "非橙武"}:
                key, value = "装备", arg
            elif arg in {"人数", "平均胜率"}:
                key, value = "统计", arg
            elif page := re.fullmatch(r"第([0-9]+)页", arg):
                key, value = "页", page[1]
            elif arg in {"全部类型", "全部装备"}:
                key, value = arg[2:], "全部"
            elif arg in {"当前", "当前周期"}:
                key, value = "周期", arg
            elif arg == "全部":
                continue
            elif re.fullmatch(r"[0-9]+", arg):
                key, value = "页", arg
            elif re.fullmatch(r"top[0-9]+", arg, re.IGNORECASE):
                raise QueryError("范围可填 50、100、200、500 或 1000。")
            else:
                alias = alias_key(arg)
                is_heart = arg == "全部心法" or is_heart_name(arg, heart_aliases)
                if is_heart and alias in server_aliases:
                    raise QueryError(f"「{arg}」同时是心法和服务器名，请填写全名或用排行筛选菜单选择。")
                key, value = ("心法" if is_heart else "服务器"), arg
        if not value or key in values:
            raise QueryError(f"「{key}」需要填写一个值，且只能填写一次。")
        values[key] = value
    unknown = values.keys() - {"周期", "心法", "服务器", "区服", "范围", "模式", "类型", "装备", "页", "统计"}
    if unknown:
        raise QueryError(f"未知榜单参数「{sorted(unknown)[0]}」。")
    if "服务器" in values and "区服" in values:
        raise QueryError("「服务器」和「区服」是同一个筛选项，请只填写一次。")
    period = values.get("周期", "")
    if period in {"当前", "当前周期"}:
        period = ""
    mode = MODES.get(values.get("模式", "3v3").lower())
    role_type = values.get("类型", "全部")
    role_type = "DPS" if role_type.lower() == "dps" else role_type
    weapon = values.get("装备", "全部")
    limit = values.get("范围", "200")
    page = values.get("页", "1")
    metric = values.get("统计", "")
    if metric not in {"", "人数", "平均胜率"}:
        raise QueryError("榜单统计可填「人数」或「平均胜率」。")
    if period and not PERIOD.fullmatch(period):
        raise QueryError("周期请写成「苍生铸世第3周」。")
    if not mode or role_type not in {"全部", "DPS", "奶妈"} or weapon not in {"全部", "橙武", "非橙武"}:
        raise QueryError("模式、类型或装备的值无效。请查看 /名剑帮助。")
    if limit not in RANK_LIMITS:
        raise QueryError("范围可填 50、100、200、500 或 1000。")
    if not re.fullmatch(r"[1-9][0-9]{0,2}", page) or int(page) > (int(limit) + 14) // 15:
        raise QueryError("页码超出当前 Top 范围，每页 15 条。")
    return Ranking(
        period=period, school=values.get("心法", "全部心法"),
        server=values.get("服务器", values.get("区服", "全部服务器")),
        limit=int(limit), mode=mode, role_type=role_type, weapon=weapon, page=int(page), metric=metric,
    )


def parse_strength(args: list[str]) -> Strength:
    values = {}
    for arg in args:
        tag, sep, text = arg.replace("＝", "=", 1).partition("=")
        if tag == "周期" and sep and text:
            key, value = "period", text
        elif arg in {"当前", "近1天", "近7天"}:
            key, value = "period", arg
        elif arg in {"组排", "单排", "全部"}:
            key, value = "queue", arg
        elif arg in {"输出", "治疗", "奶妈"} or arg.casefold() == "dps":
            key = "role_type"
            value = "治疗" if arg in {"治疗", "奶妈"} else "输出"
        elif re.fullmatch(r"[0-9]{3,4}[+＋]", arg):
            key, value = "lower", int(arg[:-1])
        elif re.fullmatch(r"[0-9]{3,4}[-–][0-9]{3,4}", arg):
            if "lower" in values:
                raise QueryError("请只填写一个分段。")
            lower, upper = re.split("[-–]", arg)
            values.update(lower=int(lower), upper=int(upper))
            continue
        elif not sep and (PERIOD.fullmatch(arg) or re.search(r"[0-9]{1,2}月[0-9]{1,2}日", arg)):
            key, value = "period", arg
        else:
            raise QueryError(f"无法识别强度参数「{arg}」。")
        if key in values:
            raise QueryError("同一筛选项只能填写一次。")
        values[key] = value
    if "upper" in values and values["lower"] >= values["upper"]:
        raise QueryError("最低分应小于最高分。")
    return Strength(**values)


def parse_tier(value: str) -> int:
    digits = "".join(CHINESE_NUMBERS)
    match = re.fullmatch(rf"第([1-9][0-9]?|[{digits}]|十[{digits}]?)重", value)
    if not match:
        raise QueryError("奇穴层数请写成「第八重」或「第8重」。")
    number = match.group(1)
    if number.isascii():
        return int(number)
    if number.startswith("十"):
        return 10 + CHINESE_NUMBERS.get(number[1:], 0)
    return CHINESE_NUMBERS[number]
