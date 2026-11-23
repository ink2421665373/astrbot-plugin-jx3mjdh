import asyncio
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import TypeGuard
from unicodedata import normalize

from astrbot.api import AstrBotConfig, logger
from astrbot.api import message_components as components
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.agent.tool import ToolSet

from .ai_review import REVIEW_ACTION, REVIEW_REPORTS, review_data
from .aliases import MAX_ROLES_PER_NICKNAME, NameAliases, index_role_nicknames
from .cards import Card, MenuImage, Sparkline
from .dashboard import register_settings_page
from .data import ApiError
from .flow_content import flow_card
from .help_content import help_card
from .labels import HEART_NAMES, alias_key
from .matches import RecordQuery, Role
from .matchups import parse_matchup
from .natural import CHINA_TIME, QUERY_ACTIONS, SYSTEM_PROMPT as NATURAL_SYSTEM_PROMPT, NaturalLanguage, correct_guide_action, parse_model_result
from .query import (
    REVIEW_SUMMARY, ChoiceMenu, HistoryFilter, MODES, QueryError, QueryTarget, parse_configuration,
    RANK_LIMITS, parse_environment, parse_history, parse_ranking, parse_strength, parse_tier, parse_trend,
)
from .replays import MAX_EXPORT_BYTES, ReplayExport
from .site import HOME, MingjianService
from .timing import QueryTiming


@dataclass
class Pending:
    expires: float
    menu: ChoiceMenu | None = None
    history: tuple[ChoiceMenu, ...] = ()
    busy: bool = False

    def can_navigate(self) -> bool:
        return self.menu is not None or bool(self.history)


_chats: dict[tuple[str, str, str], Pending] = {}
_expired_menus: dict[tuple[str, str, str], None] = {}
MAX_EXPIRED_MENUS = 512
MAX_MENU_HISTORY = 16
REPLY_PLATFORMS = {"aiocqhttp", "qq_official", "telegram", "discord", "satori", "kook", "mattermost"}
TIMING_KEY = "astrbot_plugin_mingjian.timing"
CHAT_TOOL_USED = "astrbot_plugin_mingjian.chat_tool_used"
QUERY_RETRY_REQUEST = "astrbot_plugin_mingjian.query_retry_request"
QUERY_RETRY_ATTEMPTED = "astrbot_plugin_mingjian.query_retry_attempted"
QUERY_RETRY_TIMEOUT = 10
ROLE_QUERY_ACTIONS = {"战绩", "战绩复盘", "角色总览"}
CHAT_QUERY_TOPIC = re.compile(
    r"剑(?:网)?[三3]|名剑|推栏|竞技场|战绩|对局|回放|奇穴|排行|排名|榜单|胜率|出场率|"
    r"心法|门派|配队|阵容|段位|积分|技能|阵法|走势|强度|分布|对位|配置总览|角色总览|复盘|环境|"
    r"最近[零一二三四五六七八九十两\d]+\s*场"
)
TALK_ONLY = re.compile(
    r"怎么打|如何打|打法|思路|技巧|攻略|教学|手法|怎么玩|选哪个|哪个好|区别|为什么|为啥|"
    r"评价|点评|怎么算|怎么计算|怎么定义|定义|意思|是什么|怎么理解"
)
OPINION_TALK = re.compile(r"厉害|强吗|强不强|值得|好用|好玩|推荐吗|难|离谱|没用|不行|奇怪|不对")
QUERY_TOPIC_STRONG = re.compile(
    r"战绩|复盘|排行|排名|榜单|强度|环境|配队|阵容|分布|走势|奇穴|门派|技能|阵法|对位|"
    r"配置总览|角色总览|胜率|出场率|段位|积分|帮助|流程图"
)
TOPIC_ACTIONS = (
    (re.compile(r"战绩复盘|复盘"), ("战绩复盘",)),
    (re.compile(r"角色总览"), ("角色总览",)),
    (re.compile(r"配置总览"), ("配置总览",)),
    (re.compile(r"每日走势"), ("每日走势",)),
    (re.compile(r"奇穴"), ("奇穴",)),
    (re.compile(r"技能"), ("技能",)),
    (re.compile(r"阵法"), ("阵法",)),
    (re.compile(r"门派|资料"), ("门派",)),
    (re.compile(r"对位"), ("对位",)),
    (re.compile(r"配队|阵容"), ("配队",)),
    (re.compile(r"分布"), ("分布",)),
    (re.compile(r"排行|排名|榜单"), ("排行",)),
    (re.compile(r"强度"), ("强度",)),
    (re.compile(r"环境"), ("环境",)),
    (re.compile(r"战绩|对局"), ("战绩", "战绩复盘")),
    (re.compile(r"帮助"), ("名剑帮助",)),
    (re.compile(r"流程"), ("名剑流程",)),
)
GENERIC_ACTIONS = frozenset({
    "战绩", "战绩复盘", "角色总览", "配置总览", "对位", "配队", "分布", "每日走势",
    "门派", "技能", "阵法", "奇穴", "名剑帮助", "名剑流程",
})
ENTITY_ACTIONS = frozenset({"技能", "门派", "阵法", "奇穴"})
GENERIC_TOOL_HINT = (
    "用户在查询剑网三数据，请调用 mingjian_query 并选择对应 action。"
    "参数示例：战绩 [\"唯我独尊\",\"云晚归\",\"33\"]；战绩复盘 [\"唯我独尊\",\"云晚归\",\"33\",\"10场\"]；"
    "角色总览 [\"唯我独尊\",\"云晚归\",\"33\"]；排行 [\"花间游\",\"33\",\"100\"]；"
    "强度 [\"治疗\",\"组排\",\"2200+\"]；配置总览 [\"花间游\",\"Top200\",\"治疗\"]；"
    "对位 [\"花间游\",\"苍云\"]；环境 [\"组排\"]；配队 []；分布 [\"33\",\"历周走势\"]；"
    "每日走势 [\"33\",\"花间游\"]；门派 [\"万花\"]；技能 [\"万花\",\"芙蓉并蒂\"]；"
    "阵法 [\"花间游\"]；奇穴 [\"花间游\"] 或 [\"花间游\",\"第8重\",\"套路=2\"]。"
    "参数只填值，不要写 心法=、技能= 这类标签；不要猜测角色、服务器或筛选条件。"
)
DIGEST_TOPICS = frozenset({"强度", "对位", "配队"})
ACTION_COMMANDS = {
    "战绩": "/剑三 战绩 区服 角色名",
    "战绩复盘": "/剑三 战绩复盘 区服 角色名",
    "角色总览": "/剑三 角色总览 区服 角色名",
    "排行": "/剑三 排行 心法 33",
    "强度": "/剑三 强度 输出",
    "配置总览": "/剑三 配置总览 心法",
    "对位": "/剑三 对位 我方心法 敌方心法",
    "环境": "/剑三 环境 组排",
    "配队": "/剑三 配队",
    "分布": "/剑三 分布 33",
    "每日走势": "/剑三 每日走势 33",
    "门派": "/剑三 门派 万花",
    "技能": "/剑三 技能 万花 芙蓉并蒂",
    "阵法": "/剑三 阵法 花间游",
    "奇穴": "/剑三 奇穴 花间游",
    "名剑帮助": "/名剑帮助",
    "名剑流程": "/名剑流程",
}
MULTI_SEPARATOR = re.compile(r"和|以及|还有|顺便|、|,")
MULTI_TOPICS = ("战绩", "复盘", "角色总览", "排行", "强度", "配置总览", "对位", "环境",
                "配队", "分布", "每日走势", "门派", "技能", "阵法", "奇穴")


def multi_intent(text: str) -> bool:
    """一条消息里是否明确问了两个不同的数据主题。"""
    return bool(MULTI_SEPARATOR.search(text)
                and sum(1 for topic in MULTI_TOPICS if topic in text) >= 2)


QUERY_NUDGE = "\n\n（内部提示：这是剑三数据查询，请调用 mingjian_query 工具，不要直接回答。若一次问了两件数据，请分别调用两次。）"
DIGEST_TOOL_HINT = (
    "用户在讨论或评价剑网三数据。需要事实依据时调用 mingjian_digest，"
    "topic 填 强度、对位 或 配队，只取文字摘要，不要调用 mingjian_query 出图。"
)
CHAT_DIGEST_USED = "astrbot_plugin_mingjian.chat_digest_used"
CHAT_TOOL_COUNT = "astrbot_plugin_mingjian.chat_tool_count"
PLUGIN_VERSION = "0.2.69"
QUERY_STATS = {"tool": 0, "retry": 0, "text": 0, "rule": 0, "fail": 0}
LAST_STAGE: dict[tuple[str, str, str], tuple[str, float]] = {}
LAST_QUERY_AT: dict[tuple[str, str, str], float] = {}
LAST_ERROR: dict[tuple[str, str, str], tuple[str, float]] = {}
ROUTE_CACHE: dict[tuple, tuple[str, tuple[str, ...], float]] = {}
ROUTE_CACHE_TTL = 300
CACHE_HITS = 0
CURRENT_ALIASES: NameAliases | None = None
CHINESE_DIGITS = dict(zip("零一二三四五六七八九", range(10)))
CHINESE_DIGITS["两"] = 2
CHINESE_UNITS = {"十": 10, "百": 100, "千": 1000}
DATE_WORDS = {"今天": 0, "昨天": 1, "前天": 2}
MODE_WORDS = {"三三": "33", "二二": "22", "五五": "55", "三V三": "3v3", "二V二": "2v2", "五V五": "5v5"}
CACHE_SKIP = re.compile(r"那|换成|改成|呢|继续|下一页|下一个|上一个|第[0-9一二三四五六七八九十]+")
RECENT_QUERY: dict[tuple[str, str, str], tuple[str, float, tuple[str, ...], str]] = {}
RECORD_NAME = r"[\w·丨]{1,24}"
RECORD_SERVERS = frozenset({
    "眉间雪", "山海相逢", "九天", "龙争虎斗", "斗转星移", "乾坤一掷", "剑胆琴心", "绝代天骄",
    "梦江南", "幽月轮", "长安城", "唯我独尊", "唯满侠", "蝶恋花", "天鹅坪", "破阵子", "飞龙在天",
    "比赛专用服", "练习专用服",
})
BARE_RECORD = re.compile(rf"战绩[ \t]+({RECORD_NAME})[ \t]+({RECORD_NAME})(?:[ \t]+(22|33|55|2v2|3v3|5v5))?", re.IGNORECASE)
QUERY_VERB = r"(?:请\s*)?(?:(?:帮我|给我)\s*)?(?:查(?:询)?(?:一下|下)?|看看|看一下)\s*"
ASK_RECORD = re.compile(
    rf"{QUERY_VERB}(?:剑(?:网)?[三3]\s*)?({RECORD_NAME})\s*的\s*(?:竞技场\s*)?战绩[。！？!?]?"
)
STRENGTH_ROLE = r"(?:奶妈|治疗|输出|(?<![a-z0-9_])dps(?![a-z0-9_]))"
ROLE_STRENGTH_TOPIC = re.compile(rf"{STRENGTH_ROLE}.*强度|强度.*{STRENGTH_ROLE}", re.IGNORECASE)
BARE_ROLE_STRENGTH = re.compile(
    rf"{STRENGTH_ROLE}\s*(?:的\s*)?(?:心法\s*)?强度\s*(?:排行榜?|榜单?)?[。！？!?]?",
    re.IGNORECASE,
)
ROLE_STRENGTH_TOOL_HINT = (
    "用户明确要查询心法强度榜时，请调用 mingjian_query，action 填强度。"
    "奶妈或治疗的 args 加入治疗；DPS 或输出的 args 加入输出。只保留用户明确给出的其他筛选。"
    "例如「查一下dps心法强度」调用 action=强度、args=[输出]。"
    "普通玩家名次榜使用排行。"
)
RANK_CHAT_TOPIC = re.compile(r"排行|榜单")
RANK_CHAT_OTHER_TOPIC = re.compile(r"强度|战绩|配置|配队|分布|走势|帮助|流程|用法|怎么|如何|什么用|什么意思|(?:不要|不用)查")
RANK_CHAT_GAME = re.compile(r"剑(?:网)?[三3]|名剑|竞技场|推栏|心法")
RANK_CHAT_HEARTS = frozenset(HEART_NAMES) | frozenset(HEART_NAMES.values())
RANK_TOOL_HINT = (
    "用户明确要求查询名剑玩家排行榜或发送榜单图时，调用 mingjian_query，action 填排行。"
    "例如「帮我查剑三花间游的 3v3 排行榜，发榜单图」的 args 为 [\"花间游\",\"33\"]。"
    "图卡会由工具发送；只有工具返回后才可确认查询结果。"
)
BARE_RANK = re.compile(
    r"(?:剑(?:网)?[三3]|名剑|竞技场|推栏)?[\w·丨]{0,12}?"
    r"(?:22|33|55|2v2|3v3|5v5)?(?:排行榜?|榜单?)",
    re.IGNORECASE,
)
ENV_TOPIC = re.compile(r"环境", re.IGNORECASE)
ENV_GAME = re.compile(r"剑(?:网)?[三3]|名剑|竞技场|推栏|pvp|jjc|组排|单排", re.IGNORECASE)
ENV_OTHER = re.compile(r"变量|运行环境|开发环境|部署|服务器环境|系统环境|网络环境|电脑|配置环境")
ENV_WORDS = ("剑网三", "剑三", "名剑", "竞技场", "推栏", "pvp", "jjc", "本周", "当前", "组排", "单排")
ENV_CONTEXT = frozenset({"剑网三", "剑三", "名剑", "竞技场", "推栏", "pvp", "jjc", "组排", "单排"})
ENV_TOOL_HINT = (
    "用户明确要查询名剑 PVP 环境速览时，必须调用 mingjian_query，action 固定填「环境」。"
    "args 只能填「组排」或「单排」，没有明确写就填空数组；不要填 本周、当前、pvp、jjc。"
    "例如「查一下本周组排PVP环境」的 args 为 [\"组排\"]，「本周单排pvp环境」的 args 为 [\"单排\"]。"
)


def chat_key(event: AstrMessageEvent) -> tuple[str, str, str]:
    return event.unified_msg_origin, str(event.get_self_id()), str(event.get_sender_id())


def query_retry_finished(event: AstrMessageEvent) -> bool:
    return (event.is_stopped() or bool(event.get_extra(CHAT_TOOL_USED))
            or bool(event.get_extra("agent_user_aborted")) or bool(event.get_extra("agent_stop_requested")))


def strength_role(text: str) -> str | None:
    """返回强度问句里唯一的角色类型，出现两种角色时不猜。"""
    roles = set()
    for match in re.finditer(STRENGTH_ROLE, text, re.IGNORECASE):
        role = match.group(0).casefold()
        roles.add("治疗" if role in {"奶妈", "治疗"} else "输出")
    return roles.pop() if len(roles) == 1 else None


def explicit_request(text: str) -> bool:
    return bool(re.search(QUERY_VERB, text))


def chat_only(text: str) -> bool:
    """打法、定义、评价类消息，或没有查询动词的评价句。"""
    if TALK_ONLY.search(text):
        return True
    return bool(OPINION_TALK.search(text)) and not explicit_request(text)


def digest_relevant(text: str) -> bool:
    """讨论句是否涉及可用摘要的剑三数据话题。"""
    return bool(QUERY_TOPIC_STRONG.search(text) or CHAT_QUERY_TOPIC.search(text)
                or any(heart in text for heart in RANK_CHAT_HEARTS))


def digest_text(card: Card) -> str:
    """把数据卡片压成给对话模型看的文字摘要。"""
    def cell(value: object) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, Sparkline):
            return "走势" if value.counts else ""
        text = getattr(value, "label", None) or getattr(value, "text", None)
        return str(text) if text is not None else ""

    lines = [f"【{card.title}】{card.subtitle}".strip()]
    if card.filters:
        lines.append("筛选：" + "；".join(f"{name}={cell(value)}" for name, value in card.filters))
    for table in card.tables[:2]:
        lines.append(f"{table.title}：")
        if table.columns:
            lines.append(" | ".join(cell(value) for value in table.columns))
        lines.extend(" | ".join(cell(value) for value in row) for row in table.rows[:5])
    if card.columns and card.rows:
        lines.append(" | ".join(cell(value) for value in card.columns))
        lines.extend(" | ".join(cell(value) for value in row) for row in card.rows[:5])
    for title, items in card.groups[:2]:
        lines.append(f"{title}：")
        lines.extend("，".join(f"{key}={cell(value)}" for key, value in item.items())
                     for item in items[:3])
    if card.note:
        lines.append(f"说明：{card.note}")
    lines.append("（以上为站点公开数据摘要，回答时请说明周期与筛选条件。）")
    return "\n".join(line for line in lines if line)[:1600]


def valid_chat_args(args: object) -> TypeGuard[list[str]]:
    return (isinstance(args, list) and len(args) <= 20
            and all(isinstance(value, str) and value.strip() and len(value) <= 100
                    and value.isprintable() for value in args))


def bare_rank_query(text: str) -> bool:
    """整句只有「对象 + 模式 + 排行/榜单」时视为明确榜单请求。"""
    if not RANK_CHAT_TOPIC.search(text) or RANK_CHAT_OTHER_TOPIC.search(text):
        return False
    compact = re.sub(r"\s+", "", text)
    return bool(BARE_RANK.fullmatch(compact)
                and (RANK_CHAT_GAME.search(compact)
                     or any(heart in compact for heart in RANK_CHAT_HEARTS)))


def bare_environment_query(text: str) -> bool:
    """整句由剑三、排队类型和「环境」组成时视为明确环境请求。"""
    compact = re.sub(r"\s+", "", text).casefold().rstrip("，,。.！!？?~～…")
    if ENV_OTHER.search(compact) or not compact.endswith("环境"):
        return False
    rest, seen = compact[:-2], set()
    while rest:
        for word in ENV_WORDS:
            if rest.startswith(word):
                seen.add(word)
                rest = rest[len(word):]
                break
        else:
            return False
    return bool(seen & ENV_CONTEXT)


def environment_args(text: str) -> list[str]:
    """环境查询参数：只在明确说单排时传单排，其余按默认组排。"""
    compact = re.sub(r"\s+", "", text)
    return ["单排"] if "单排" in compact and "组排" not in compact else []


def retry_args(action: str, args: object, text: str) -> list[str] | None:
    """补调返回的环境参数以消息里的排队类型为准。"""
    if action != "环境":
        return args if isinstance(args, list) else None
    compact = re.sub(r"\s+", "", text)
    if "单排" in compact:
        return ["单排"]
    if "组排" in compact:
        return ["组排"]
    values = [value.strip() for value in (args or [])
              if isinstance(value, str) and value.strip() in {"组排", "单排"}]
    return values[:1]


STRENGTH_ARG_FIXES = {
    "dps": "输出", "DPS": "输出", "奶妈": "治疗", "奶": "治疗",
    "近七天": "近7天", "近一周": "近7天", "当前周期": "当前", "全分段": "全部",
}


def score_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    if re.fullmatch(r"[一二三四五六七八九十百千两]+", value):
        return chinese_number(value)
    return None


def normalize_args(action: str, args: object, text: str) -> list[str] | None:
    """把模型常见的参数写法纠正成插件能解析的形式。"""
    if not isinstance(args, list):
        return None
    values = [MODE_WORDS.get(str(value).strip(), str(value).strip())
              for value in args if str(value).strip()]
    if action == "环境":
        return retry_args("环境", values, text) or []
    if action == "强度":
        fixed = []
        for value in values:
            value = STRENGTH_ARG_FIXES.get(value, value)
            if match := re.fullmatch(r"([0-9]{3,4}|[一二三四五六七八九十百千两]+)\s*分?(?:以上|\+)", value):
                number = score_number(match[1])
                if number:
                    value = f"{number}+"
            elif match := re.fullmatch(
                    r"([0-9]{3,4}|[一二三四五六七八九十百千两]+)\s*[-–到]\s*"
                    r"([0-9]{3,4}|[一二三四五六七八九十百千两]+)\s*分?", value):
                lower, upper = score_number(match[1]), score_number(match[2])
                if lower and upper:
                    value = f"{lower}-{upper}"
            else:
                number = score_number(value)
                if number and 1000 <= number <= 4000:
                    value = f"{number}+"
            if value not in fixed:
                fixed.append(value)
        return fixed
    if action == "排行":
        fixed = []
        for value in values:
            if value in {"名剑", "竞技场", "推栏", "pvp", "PVP", "jjc", "JJC"}:
                continue
            if match := re.fullmatch(r"前?([0-9]+|[一二三四五六七八九十百千两]+)\s*名?", value):
                number = score_number(match[1])
                if number is not None and str(number) in RANK_LIMITS:
                    value = str(number)
            if value not in fixed:
                fixed.append(value)
        return fixed
    if action in ROLE_QUERY_ACTIONS:
        fixed = []
        for value in values:
            if value in {"最近", "近"}:
                continue
            if value in DATE_WORDS:
                value = (datetime.now(CHINA_TIME).date()
                         - timedelta(days=DATE_WORDS[value])).isoformat()
            for server in sorted(RECORD_SERVERS, key=len, reverse=True):
                if value.startswith(server) and len(value) > len(server):
                    fixed.extend([server, value[len(server):]])
                    break
            else:
                fixed.append(value)
        return fixed
    if action in {"门派", "技能", "奇穴", "阵法"}:
        fixed = []
        for value in values:
            for label in ("心法", "技能"):
                for sep in ("=", "＝"):
                    prefix = label + sep
                    if value.startswith(prefix):
                        value = value[len(prefix):].strip()
                        break
            if value and value not in {action, "心法", "技能"}:
                fixed.append(value)
        if len(fixed) >= 2 and alias_key(fixed[0]) == alias_key(fixed[1]):
            fixed.pop(1)
        return fixed
    return values


FOLLOW_UP_PREFIX = re.compile(r"^\s*(?:那|那么|换成|改成|再|看看)\s*")
FOLLOW_UP_SUFFIX = re.compile(r"\s*(?:呢|怎么样|怎么算|如何)\s*$")


def follow_up_entity(text: str, aliases: NameAliases) -> str | None:
    """「那奶妈呢」「换成单排」这类短追问里的实体。"""
    entity = FOLLOW_UP_SUFFIX.sub("", FOLLOW_UP_PREFIX.sub("", text)).strip()
    if not entity or len(entity) > 8:
        return None
    if entity in {"组排", "单排"} or re.fullmatch(STRENGTH_ROLE, entity, re.IGNORECASE):
        return entity
    if entity in HEART_NAMES or alias_key(entity) in aliases.hearts:
        return entity
    return None


def route_prompt(event: AstrMessageEvent, text: str) -> str:
    """补调和文本路由带上上一轮问句，支持「那奶妈呢」这类追问。"""
    recent = RECENT_QUERY.get(chat_key(event))
    if recent and monotonic() - recent[1] <= 120 and recent[0] != text:
        return f"上一轮用户：{recent[0]}\n本轮用户：{text}"
    return text


def remember_query(event: AstrMessageEvent, text: str, action: str):
    hint = {"强度": ROLE_STRENGTH_TOOL_HINT, "排行": RANK_TOOL_HINT,
            "环境": ENV_TOOL_HINT}.get(action, GENERIC_TOOL_HINT)
    RECENT_QUERY[chat_key(event)] = (text, monotonic(), (action,), hint)


def cacheable_route(text: str) -> bool:
    return (len(text) >= 6 and not CACHE_SKIP.search(text)
            and bool(QUERY_TOPIC_STRONG.search(text)))


def cached_route(event: AstrMessageEvent) -> tuple[str, list[str]] | None:
    if not event.is_at_or_wake_command:
        return None
    text = normalize("NFKC", event.get_message_str().strip())
    entry = ROUTE_CACHE.get(chat_key(event) + (text,))
    if not entry or monotonic() - entry[2] > ROUTE_CACHE_TTL:
        return None
    return entry[0], list(entry[1])


class CachedQuery(filter.CustomFilter):
    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        return cached_route(event) is not None


def direct_query_route(event: AstrMessageEvent) -> tuple[str, list[str]] | None:
    if (event.get_extra("mingjian_no_direct") or not event.is_at_or_wake_command
            or CURRENT_ALIASES is None):
        return None
    raw = str(getattr(getattr(event, "message_obj", None), "message_str", "") or "")
    text = normalize("NFKC", event.get_message_str().strip())
    if (raw.strip().startswith(("/", "／")) or text.startswith(("/", "／"))
            or chat_only(text) or multi_intent(text)):
        return None
    return fallback_route(text, CURRENT_ALIASES)


class DirectQuery(filter.CustomFilter):
    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        return direct_query_route(event) is not None


SCHOOL_TOKEN = re.compile(
    r"(?:查|看|帮我|给我)?[一下]*(?:剑(?:网)?[三3])?\s*([\w·丨]{2,8}?)(?:的)?\s*"
    r"(?:门派|资料|阵法|奇穴|技能)")


def find_hearts(text: str, aliases: NameAliases) -> list[str]:
    found = []
    for alias, targets in sorted(aliases.hearts.items(), key=lambda item: -len(item[0])):
        if len(targets) == 1 and alias and alias in text and targets[0] not in found:
            found.append(targets[0])
    for alias in sorted(HEART_NAMES, key=len, reverse=True):
        if not re.search(r"[\u4e00-\u9fff]", alias) or alias not in text:
            continue
        name = HEART_NAMES[alias]
        if name not in found:
            found.append(name)
    return found


ROLE_BEFORE = re.compile(r"([\w·丨]{2,24}?)的(?:竞技场)?(?:战绩|复盘|角色总览)")
ROLE_BEFORE_RECENT = re.compile(r"([\w·丨]{2,24}?)(?:最近|近)\s*[0-9一二三四五六七八九十两]+\s*场")
ROLE_AFTER = re.compile(r"(?:战绩|复盘|角色总览)\s+([\w·丨]{2,24})(?:\s+([\w·丨]{2,24}))?")
SKILL_PAIR = re.compile(r"([\w·丨]{2,8})的([\w·丨]{2,12}?)(?:技能|详情)")
ROLE_PREFIX = re.compile(
    r"^(?:帮我|给我|请|查一下|查一查|查询|查|看看|看一下|看|复盘|战绩复盘|多场复盘)\s*")


def split_role_name(value: str) -> tuple[str, str]:
    for server in sorted(RECORD_SERVERS, key=len, reverse=True):
        if value.startswith(server) and len(value) > len(server):
            return server, value[len(server):]
    return "", value


def find_server(text: str, aliases: NameAliases) -> str:
    servers = set(RECORD_SERVERS) | set(aliases.servers)
    for server in sorted(servers, key=len, reverse=True):
        if server and server in text:
            return server
    return ""


def text_date(text: str) -> str:
    if match := re.search(r"\d{4}-\d{2}-\d{2}", text):
        return match[0]
    for word, days in DATE_WORDS.items():
        if word in text:
            return (datetime.now(CHINA_TIME).date() - timedelta(days=days)).isoformat()
    return ""


def text_mode(text: str) -> str:
    for value in ("22", "33", "55", "2v2", "3v3", "5v5"):
        if value in text:
            return value
    for word, value in MODE_WORDS.items():
        if word in text:
            return value
    return ""


def text_pool(text: str) -> str:
    match = re.search(r"(?:前|top)\s*([0-9]{2,4}|[一二三四五六七八九十百千两]+)\s*名?",
                      text, re.IGNORECASE)
    if match:
        number = score_number(match[1])
        if number is not None and str(number) in RANK_LIMITS:
            return str(number)
    return ""


def fallback_route(text: str, aliases: NameAliases) -> tuple[str, list[str]] | None:
    """按问法直接得到动作和参数；模型失败时的兜底，也是明确问法的直查路径。"""
    if chat_only(text):
        return None
    actions = query_actions(text)
    if not actions:
        return None
    action = actions[0]
    hearts = find_hearts(text, aliases)
    school_match = SCHOOL_TOKEN.search(text)
    school = hearts[0] if hearts else (school_match[1] if school_match else "")
    mode = text_mode(text)
    date = text_date(text)
    count_match = re.search(r"([0-9]{1,3})\s*场", text)
    count = f"{count_match[1]}场" if count_match else ""
    queue = "单排" if "单排" in text else "组排" if "组排" in text else ""
    role_type = strength_role(text)
    pool = text_pool(text)
    if action in ROLE_QUERY_ACTIONS:
        match = ROLE_AFTER.search(text)
        if match and match[2]:
            server, name = match[1], match[2]
        else:
            part = ""
            cleaned = ROLE_PREFIX.sub("", text)
            for pattern in (ROLE_BEFORE, ROLE_BEFORE_RECENT):
                if found := pattern.search(cleaned):
                    part = found[1]
                    break
            if not part and match:
                part = match[1]
            if not part:
                return None
            server, name = split_role_name(part)
        args = ([server] if server else []) + [name]
        if mode:
            args.append(mode)
        if count:
            args.append(count)
        if date:
            args.append(date)
        if action == "战绩复盘" and queue:
            args.append(queue)
        return action, args
    if action == "技能":
        pair = SKILL_PAIR.search(text)
        return (action, [pair[1], pair[2]]) if pair else None
    if action == "奇穴" and school:
        return action, [school, *([] if "套路" in text else ["套路=1"])]
    if action in {"门派", "阵法"} and school:
        return action, [school]
    if action == "配置总览":
        args = []
        if school:
            args.append(school)
        if pool:
            args.append(f"Top{pool}")
        if role_type:
            args.append(role_type)
        return action, args
    if action == "配队":
        return action, []
    if action == "环境":
        return action, environment_args(text)
    if action == "分布":
        args = [mode] if mode else []
        period = "周内每日" if "周内每日" in text else "历周走势" if "历周走势" in text else ""
        if period:
            args.append(period)
        return action, args
    if action == "每日走势":
        args = [mode] if mode else []
        if hearts:
            args.append(",".join(hearts[:3]))
        if "较首日" in text:
            args.append("较首日")
        return action, args
    if action == "排行":
        args = []
        if hearts:
            args.append(hearts[0])
        server = find_server(text, aliases)
        if server:
            args.append(server)
        if mode:
            args.append(mode)
        if pool:
            args.append(pool)
        return action, args
    if action == "强度":
        args = []
        if role_type:
            args.append(role_type)
        if queue:
            args.append(queue)
        score = ""
        if match := re.search(r"([0-9]{3,4})\s*分?(?:以上|\+)", text):
            score = match[1] + "+"
        elif match := re.search(r"([0-9]{3,4})\s*[-–到]\s*([0-9]{3,4})", text):
            score = f"{match[1]}-{match[2]}"
        if score:
            args.append(score)
        period = next((value for value in ("近7天", "近1天", "当前") if value in text), "")
        if period:
            args.append(period)
        return action, args
    if action == "对位" and hearts:
        return action, hearts[:2]
    if action in {"名剑帮助", "名剑流程"}:
        return action, []
    return None


def query_actions(text: str) -> tuple[str, ...]:
    for pattern, actions in TOPIC_ACTIONS:
        if pattern.search(text):
            return actions
    return ()


def generic_query_actions(text: str) -> tuple[str, ...]:
    """明确的数据请求但未命中专用补调时，返回可接受的 action。"""
    if chat_only(text) or not QUERY_TOPIC_STRONG.search(text):
        return ()
    actions = tuple(action for action in query_actions(text) if action in GENERIC_ACTIONS)
    if not actions:
        return ()
    if actions[0] in {"名剑帮助", "名剑流程"} and not re.search(r"剑(?:网)?[三3]|名剑", text):
        return ()
    entity = (any(heart in text for heart in RANK_CHAT_HEARTS)
              or any(server in text for server in RECORD_SERVERS)
              or bool(re.search(r"22|33|55|2v2|3v3|5v5", text, re.IGNORECASE)))
    return actions if explicit_request(text) or entity or len(re.sub(r"\s+", "", text)) <= 12 else ()


def chinese_number(text: str) -> int | None:
    if text in CHINESE_DIGITS:
        return CHINESE_DIGITS[text]
    if match := re.fullmatch(r"([一二三四五六七八九两])千([一二三四五六七八九两])", text):
        return CHINESE_DIGITS[match[1]] * 1000 + CHINESE_DIGITS[match[2]] * 100
    total, current = 0, 0
    for char in text:
        if char in CHINESE_DIGITS:
            current = CHINESE_DIGITS[char]
        elif char in CHINESE_UNITS:
            total += (current or 1) * CHINESE_UNITS[char]
            current = 0
        else:
            return None
    return total + current


def selection_number(event: AstrMessageEvent) -> int | None:
    text = normalize("NFKC", event.get_message_str().strip())
    if text in {"返回", "上一级", "上一步", "取消"}:
        return 0
    if re.fullmatch(r"[0-9]{1,3}", text):
        return int(text)
    match = re.fullmatch(r"(?:选|第)?\s*([0-9]{1,3}|[一二三四五六七八九十两]{1,3})\s*(?:个|号|项)?", text)
    if not match:
        return None
    value = match[1]
    return int(value) if value.isdigit() else chinese_number(value)


def expire_menu(key: tuple[str, str, str]):
    state = _chats.pop(key)
    if state.can_navigate():
        _expired_menus[key] = None
        if len(_expired_menus) > MAX_EXPIRED_MENUS:
            _expired_menus.pop(next(iter(_expired_menus)))


def pending_menu(event: AstrMessageEvent) -> Pending | None:
    key = chat_key(event)
    state = _chats.get(key)
    if state and state.expires <= monotonic():
        expire_menu(key)
        return None
    return state if state and not state.busy and state.can_navigate() else None


class PendingNumber(filter.CustomFilter):
    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        if selection_number(event) is None:
            return False
        return pending_menu(event) is not None or chat_key(event) in _expired_menus


def direct_record_args(event: AstrMessageEvent) -> list[str] | None:
    text = event.get_message_str().strip()
    if match := BARE_RECORD.fullmatch(text):
        return [value for value in match.groups() if value is not None]
    if not event.is_at_or_wake_command:
        return None
    if match := ASK_RECORD.fullmatch(text):
        name = match[1]
        if name.casefold() in {"我", "你", "他", "她", "它", "ta", "对方", "这个人", "那个人"}:
            return None
        for server in sorted(RECORD_SERVERS, key=len, reverse=True):
            if name.startswith(server) and len(name) > len(server):
                return [server, name[len(server):]]
        return [name]
    return None


class DirectRecord(filter.CustomFilter):
    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        return direct_record_args(event) is not None


@register("astrbot_plugin_mingjian", "Codex", "剑网三名剑大会数据查询与图卡", "0.2.69")
class Mingjian(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.aliases = NameAliases.from_config(config)
        global CURRENT_ALIASES
        CURRENT_ALIASES = self.aliases
        self.role_nicknames = index_role_nicknames((config or {}).get("role_nicknames"))
        self.natural = NaturalLanguage(context, config)
        try:
            cache_path = StarTools.get_data_dir("astrbot_plugin_mingjian") / "school-cache.json"
        except (OSError, RuntimeError) as exc:
            logger.warning(f"名剑无法打开插件数据目录，门派资料改用内存缓存 类型={type(exc).__name__}")
            cache_path = None
        self.service = MingjianService(cache_path)
        register_settings_page(self, config)

    async def terminate(self):
        _chats.clear()
        _expired_menus.clear()
        await self.service.close()

    @filter.command("名剑帮助")
    async def help_mingjian(self, event: AstrMessageEvent):
        """发送名剑查询帮助图。"""
        yield await self._guide_result(event, help_card())

    @filter.command("名剑流程")
    async def flow_mingjian(self, event: AstrMessageEvent):
        """发送名剑指令从入口到后续菜单的流程长图。"""
        yield await self._guide_result(event, flow_card())

    def _cache_route(self, event: AstrMessageEvent, text: str, action: str, args: list[str]):
        if not self.natural.route_cache_enabled or not cacheable_route(text):
            return
        if action in {"名剑帮助", "名剑流程"}:
            return
        ROUTE_CACHE[chat_key(event) + (text,)] = (action, tuple(args), monotonic())
        while len(ROUTE_CACHE) > 64:
            ROUTE_CACHE.pop(next(iter(ROUTE_CACHE)))

    @filter.command("名剑诊断")
    async def diagnose(self, event: AstrMessageEvent):
        """查看名剑插件的工具、模型和最近一次查询状态。"""
        provider = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
        stage = LAST_STAGE.get(chat_key(event))
        now = monotonic()
        stage_text = (f"{stage[0]}（{int(now - stage[1])} 秒前）"
                      if stage else "本会话还没有完成查询")
        error = LAST_ERROR.get(chat_key(event))
        error_text = f"{error[0]}（{int(now - error[1])} 秒前）" if error else "无"
        tool_text = "未知"
        try:
            toolset = self.context.provider_manager.llm_tools.get_full_tool_set()
            tool_text = "已注册" if toolset.get_tool("mingjian_query") else "未注册"
        except Exception:
            pass
        if getattr(self.service.client, "limited_until", 0) > now:
            site_text = "限流中"
        else:
            try:
                await asyncio.wait_for(self.service.client.get("rank/time-tags", ttl=600), 3)
                site_text = "正常"
            except asyncio.TimeoutError:
                site_text = "超时"
            except Exception as exc:
                site_text = f"失败（{type(exc).__name__}）"
        yield event.plain_result("\n".join((
            f"名剑查询 v{PLUGIN_VERSION}",
            f"工具：{tool_text}（人格未授权时日志会提示不可用）",
            f"对话模型：{provider or '未配置'}",
            f"自然语言：{'已开启' if self.natural.enabled else '未开启'} · 超时 {self.natural.timeout_seconds}s",
            f"站点：{site_text}",
            f"缓存：路由命中 {CACHE_HITS} 次 · 当前 {len(ROUTE_CACHE)} 条",
            f"最近一次：{stage_text}",
            f"最近错误：{error_text}",
            f"运行统计：规则直查 {QUERY_STATS['rule']} · 模型调用 {QUERY_STATS['tool']} · "
            f"补调 {QUERY_STATS['retry']} · 文本路由 {QUERY_STATS['text']} · 失败 {QUERY_STATS['fail']}",
        )))

    async def _guide_result(self, event: AstrMessageEvent, card: Card, timing: QueryTiming | None = None):
        timing = timing or QueryTiming(card.title)
        try:
            image = await self.service.render_card(card, timing=timing)
        except QueryError as exc:
            result = event.plain_result(str(exc))
        else:
            timing.image_bytes = len(image)
            result = event.chain_result([components.Image.fromBytes(image)])
        return self._result_ready(event, result, timing)

    @staticmethod
    def _result_ready(event: AstrMessageEvent, result, timing: QueryTiming):
        timing.send_started = monotonic()
        event.set_extra(TIMING_KEY, timing)
        return result

    @filter.after_message_sent()
    async def log_result_timing(self, event: AstrMessageEvent):
        """Record the elapsed time through AstrBot's final response stage."""
        timing = event.get_extra(TIMING_KEY)
        if isinstance(timing, QueryTiming):
            event.set_extra(TIMING_KEY, None)
            timing.log(event, "发送阶段结束")

    @staticmethod
    async def _send_timed(event: AstrMessageEvent, result, timing: QueryTiming):
        timing.send_started = monotonic()
        status = "发送异常或中止"
        try:
            await event.send(result)
            status = "发送调用完成"
        except Exception as exc:
            logger.warning(f"名剑发送结果失败 类型={type(exc).__name__}")
            LAST_ERROR[chat_key(event)] = (f"发送{type(exc).__name__}", monotonic())
            try:
                await event.send(event.plain_result("图卡发送失败，请稍后重试。"))
            except Exception as retry_exc:
                logger.warning(f"名剑发送失败提示也失败 类型={type(retry_exc).__name__}")
        finally:
            timing.log(event, status)

    async def _query(self, event: AstrMessageEvent, action: str, args: list[str]):
        if action == "配置总览":
            query = parse_configuration(args, heart_aliases=self.aliases.hearts)
            return replace(query, school=self.aliases.heart(query.school), search=self.aliases.lineup(query.search))
        if action == "对位":
            return parse_matchup(args, self.aliases)
        if action == "环境":
            return parse_environment(args)
        if action == "排行":
            choosing_filters = bool(args and args[0] == "筛选")
            query = parse_ranking(
                args[1:] if choosing_filters else args,
                heart_aliases=self.aliases.hearts, server_aliases=self.aliases.servers,
            )
            query = replace(query, school=self.aliases.heart(query.school), server=self.aliases.server(query.server))
            if choosing_filters:
                fields = ("周期", "心法", "服务器", "范围", "模式", "类型", "装备", "统计")
                return ChoiceMenu(
                    "请选择要修改的排行榜筛选项。", "排行筛选",
                    tuple((field, (field, query)) for field in fields),
                )
            return query
        if action == "强度":
            if args and args[0] == "筛选":
                query = parse_strength(args[1:])
                menus = (
                    ("排队类型", tuple((label, replace(query, queue=label)) for label in ("组排", "单排", "全部"))),
                    ("心法类型", tuple((label, replace(query, role_type=label)) for label in ("输出", "治疗"))),
                    ("常用分段", tuple(
                        (label, replace(query, lower=minimum, upper=None))
                        for label, minimum in (("全部分段", None), ("2200+", 2200), ("2400+", 2400))
                    )),
                )
                options = tuple(
                    (label, ChoiceMenu(f"请选择{label}。", "强度", options))
                    for label, options in menus
                )
                return ChoiceMenu("请选择要修改的强度筛选项。", "强度",
                                  (*options, ("统计周期", QueryTarget("强度周期", query))))
            return parse_strength(args)
        if action == "配队":
            if args:
                raise QueryError("用法：/剑三 配队")
            return ()
        if action in {"分布", "每日走势"}:
            if action == "分布" and args == ["筛选"]:
                return ChoiceMenu("请选择走势的比赛模式。", "分布", tuple(
                    (value, ChoiceMenu("请选择统计周期。", "分布", tuple(
                        (label, (value, label)) for label in ("历周走势", "周内每日")
                    )))
                    for value in ("2v2", "3v3", "5v5")
                ))
            query = parse_trend(args, daily=action == "每日走势", heart_aliases=self.aliases.hearts)
            if not (query.week or query.hearts or query.metric != "人数"):
                return query.mode, query.period
            return replace(query, hearts=tuple(self.aliases.heart(name) for name in query.hearts))
        if action in ROLE_QUERY_ACTIONS:
            return self._role_query(action, args)
        if action in {"门派", "技能", "奇穴", "阵法"}:
            if not args:
                raise QueryError(f"用法：/剑三 {action} 门派或心法。详见 /名剑帮助。")
            name = self.aliases.heart(args.pop(0))
            detail, tactic = "", ""
            if action == "技能":
                if len(args) != 1:
                    raise QueryError("用法：/剑三 技能 门派或心法 技能名")
                detail = args.pop()
            if action == "奇穴":
                for value in args:
                    key, sep, selection = value.replace("＝", "=", 1).partition("=")
                    if not sep and value.startswith("第") and value.endswith("重"):
                        if detail:
                            raise QueryError("请只填写一重奇穴，例如「第8重」。")
                        detail = parse_tier(value)
                    else:
                        if tactic or sep and (key != "套路" or not selection):
                            raise QueryError("用法为 /剑三 奇穴 心法 [第N重] [套路编号或名称]。")
                        tactic = selection if sep else value
            elif args:
                raise QueryError(f"用法：/剑三 {action} 门派或心法")
            return name, detail, tactic
        raise QueryError("没有这个子指令。发送 /名剑帮助 查看用法。")

    def _role_query(self, action: str, args: list[str]):
        params, mode = [], ""
        for token in args:
            key, sep, value = token.replace("＝", "=", 1).partition("=")
            if key == "模式" and sep:
                selected = MODES.get(value.lower())
                if mode or not selected:
                    raise QueryError("模式请填写一次，可选 22、33、55 或 2v2、3v3、5v5。")
                mode = selected
            elif sep and action == "角色总览":
                raise QueryError("角色总览可指定雷达赛制。日期和场数筛选请用 /剑三 战绩复盘。")
            else:
                params.append(token)
        role_params, history = parse_history(params, review=action == "战绩复盘")
        if action == "角色总览" and role_params != params:
            raise QueryError("角色总览可指定雷达赛制。日期和场数筛选请用 /剑三 战绩复盘。")
        params = role_params
        if params and params[-1].lower() in MODES and (not mode or len(params) > 1):
            if mode:
                raise QueryError("同一条指令只能填写一次模式。")
            mode = MODES[params.pop().lower()]
        mode = mode or "3v3"
        if not params:
            raise QueryError(f"请填写角色名或推栏个人 ID，例如 /剑三 {action} 梦江南 角色名 33。")
        if len(params) == 1:
            server, name = "", params[0]
        elif len(params) == 2:
            server, name = params
            server = self.aliases.server(server)
        else:
            raise QueryError(f"用法为 /剑三 {action} [服务器] 角色名或ID [模式]。")
        role = (server, name, mode)
        return role if history == HistoryFilter() else RecordQuery(role, history)

    def _role_nickname_menu(self, action: str, params: tuple | RecordQuery) -> ChoiceMenu | None:
        server, name, mode = params.origin if isinstance(params, RecordQuery) else params
        roles = self.role_nicknames.get(alias_key(name))
        if not roles:
            return None
        server_key = alias_key(server)
        filtered = (role for role in roles if server_key in {"", "全部服务器", "全服搜索"}
                    or alias_key(role[0]) == server_key)
        options = []
        for role_server, role_name in filtered:
            role = Role(role_name, role_server, mode)
            query = replace(params, origin=role) if isinstance(params, RecordQuery) else role
            options.append((f"{role_server} · {role_name}", query))
            if len(options) == MAX_ROLES_PER_NICKNAME:
                break
        prompt = f"「{name}」对应以下角色。"
        if not options:
            prompt = f"「{name}」在「{server}」没有配置角色。"
        options.append((f"按角色原名「{name}」查询", params))
        return ChoiceMenu(prompt, action, tuple(options))

    @filter.command("剑三")
    async def jx3(self, event: AstrMessageEvent):
        """剑网三战绩、排行榜、心法强度和门派资料。"""
        command = event.get_message_str().split(maxsplit=2)
        if len(command) < 2:
            yield event.plain_result("发送 /名剑帮助 查看查询菜单。")
            return
        action = command[1]
        rest = command[2].strip() if len(command) == 3 else ""
        if action == "问":
            async for result in self._execute(event, "名剑", [], utterance=rest):
                yield result
            return
        if action in {"多场复盘", "多场战绩复盘"}:
            action = "战绩复盘"
        args = [match[1] or match[2] for match in re.finditer(r'"([^"]+)"(?=\s|$)|(\S+)', rest)]
        async for result in self._execute(event, action, args):
            yield result

    @filter.command("名剑")
    async def natural_command(self, event: AstrMessageEvent):
        """用自然语言查询名剑大会。"""
        command = event.get_message_str().split(maxsplit=1)
        text = command[1].strip() if len(command) == 2 else ""
        async for result in self._execute(event, "名剑", [], utterance=text):
            yield result

    @filter.custom_filter(DirectRecord, priority=100)
    async def direct_record(self, event: AstrMessageEvent):
        """查询明确的战绩短句，直接复用战绩指令。"""
        args = direct_record_args(event)
        if args is None:
            return
        if len(args) > 1:
            server = args[0]
            if server not in RECORD_SERVERS and alias_key(server) not in self.aliases.servers:
                return
        event.stop_event()
        sent = False
        async for response in self._execute(event, "战绩", args):
            timing = event.get_extra(TIMING_KEY)
            event.set_extra(TIMING_KEY, None)
            await self._send_timed(event, response, timing)
            sent = True
        if sent:
            remember_query(event, event.get_message_str().strip(), "战绩")

    @filter.custom_filter(CachedQuery, priority=100)
    async def cached_query(self, event: AstrMessageEvent):
        """同一会话重复的明确问法直接复用上次成功的路由。"""
        hit = cached_route(event)
        if hit is None:
            return
        action, args = hit
        global CACHE_HITS
        CACHE_HITS += 1
        LAST_STAGE[chat_key(event)] = ("路由缓存", monotonic())
        event.stop_event()
        async for response in self._execute(event, action, args):
            timing = event.get_extra(TIMING_KEY)
            event.set_extra(TIMING_KEY, None)
            await self._send_timed(event, response, timing)

    @filter.custom_filter(DirectQuery, priority=100)
    async def direct_query(self, event: AstrMessageEvent):
        """明确的自然语言查询直接执行，不依赖模型是否调用工具。"""
        route = direct_query_route(event)
        if route is None or event.is_stopped():
            return
        action, args = route
        text = normalize("NFKC", event.get_message_str().strip())
        now = monotonic()
        key = chat_key(event)
        if self.natural.cooldown and now - LAST_QUERY_AT.get(key, 0.0) < self.natural.cooldown:
            return
        LAST_QUERY_AT[key] = now
        logger.info(f"名剑规则直查 动作={action} 参数={args}")
        LAST_STAGE[key] = ("规则直查", now)
        QUERY_STATS["rule"] += 1
        event.stop_event()
        if action in {"名剑帮助", "名剑流程"}:
            card = help_card() if action == "名剑帮助" else flow_card()
            await self._send_timed(event, await self._guide_result(event, card), QueryTiming(action))
        else:
            async for response in self._execute(event, action, list(args)):
                timing = event.get_extra(TIMING_KEY)
                event.set_extra(TIMING_KEY, None)
                await self._send_timed(event, response, timing)
        remember_query(event, text, action)

    @filter.on_llm_request()
    async def guide_query_tool(self, event: AstrMessageEvent, request: ProviderRequest):
        """为明确的数据查询补充工具用法，并为讨论补一份数据摘要说明。"""
        text = normalize("NFKC", event.get_message_str().strip())
        func_tool = request.func_tool
        tool = func_tool.get_tool("mingjian_query") if func_tool else None
        digest = func_tool.get_tool("mingjian_digest") if func_tool else None
        query_ready = bool(tool and tool.active)
        digest_ready = bool(digest and digest.active)
        if chat_only(text):
            if digest_ready and digest_relevant(text):
                request.system_prompt += "\n\n" + DIGEST_TOOL_HINT
            return
        if not query_ready:
            if not digest_ready and CHAT_QUERY_TOPIC.search(text):
                logger.info("名剑工具当前会话不可用，未补充查询说明；请检查人格工具权限、模型工具调用设置和唤醒方式。")
            return
        explicit_query = re.search(QUERY_VERB, text)
        ready = (not event.get_extra(QUERY_RETRY_ATTEMPTED)
                 and not query_retry_finished(event))
        if ROLE_STRENGTH_TOPIC.search(text) and (explicit_query or BARE_ROLE_STRENGTH.fullmatch(text)):
            request.system_prompt += "\n\n" + ROLE_STRENGTH_TOOL_HINT
            expected_role = strength_role(text)
            if expected_role and ready:
                event.set_extra(QUERY_RETRY_REQUEST, {
                    "actions": ("强度",),
                    "hint": ROLE_STRENGTH_TOOL_HINT,
                    "system": (request.system_prompt or "")[:4000],
                    "tools": ToolSet([tool]),
                    "role_type": expected_role,
                })
        elif ((explicit_query or bare_rank_query(text))
              and RANK_CHAT_TOPIC.search(text) and not RANK_CHAT_OTHER_TOPIC.search(text)
              and (RANK_CHAT_GAME.search(text) or any(heart in text for heart in RANK_CHAT_HEARTS))):
            request.system_prompt += "\n\n" + RANK_TOOL_HINT
            if ready:
                event.set_extra(QUERY_RETRY_REQUEST, {
                    "actions": ("排行",),
                    "hint": RANK_TOOL_HINT,
                    "system": (request.system_prompt or "")[:4000],
                    "tools": ToolSet([tool]),
                })
        elif (ENV_TOPIC.search(text) and ENV_GAME.search(text) and not ENV_OTHER.search(text)
              and (explicit_query or bare_environment_query(text))):
            request.system_prompt += "\n\n" + ENV_TOOL_HINT
            if ready:
                event.set_extra(QUERY_RETRY_REQUEST, {
                    "actions": ("环境",),
                    "hint": ENV_TOOL_HINT,
                    "system": (request.system_prompt or "")[:4000],
                    "tools": ToolSet([tool]),
                    "fallback": environment_args(text),
                })
        else:
            actions = generic_query_actions(text)
            hint = GENERIC_TOOL_HINT
            if not actions:
                recent = RECENT_QUERY.get(chat_key(event))
                if (recent and monotonic() - recent[1] <= 120
                        and follow_up_entity(text, self.aliases)):
                    actions, hint = recent[2], recent[3]
            if actions:
                request.system_prompt += "\n\n" + hint
                if ready:
                    payload = {
                        "actions": actions,
                        "hint": hint,
                        "system": (request.system_prompt or "")[:4000],
                        "tools": ToolSet([tool]),
                    }
                    if actions == ("强度",) and (role := strength_role(text)):
                        payload["role_type"] = role
                    event.set_extra(QUERY_RETRY_REQUEST, payload)
        if isinstance(event.get_extra(QUERY_RETRY_REQUEST), dict):
            if multi_intent(text):
                event.set_extra("mingjian_multi", True)
            prompt = (request.prompt or "").rstrip()
            if "请调用 mingjian_query" not in prompt:
                request.prompt = prompt + QUERY_NUDGE

    @filter.on_llm_response()
    async def retry_query_tool(self, event: AstrMessageEvent, response: LLMResponse | None):
        """明确的名剑排行或心法强度请求漏调工具时，让当前模型补选一次。"""
        if (event.get_extra("mingjian_multi") and event.get_extra(CHAT_TOOL_USED)
                and response is not None and response.role == "assistant" and not response.tools_call_name):
            response.result_chain = None
            response.completion_text = ""
            return
        retry_request = event.get_extra(QUERY_RETRY_REQUEST)
        if not isinstance(retry_request, dict):
            if (response is not None and response.role == "assistant" and not response.tools_call_name
                    and not response.result_chain and not (response.completion_text or "").strip()):
                text = normalize("NFKC", event.get_message_str().strip())
                if CHAT_QUERY_TOPIC.search(text) and not chat_only(text):
                    logger.info("名剑数据查询返回空回复，补充提示。")
                    response.completion_text = "本次没有生成查询结果，可以发送 /剑三 或 /名剑帮助 重新查询。"
            return
        text = normalize("NFKC", event.get_message_str().strip())
        if query_retry_finished(event):
            event.set_extra(QUERY_RETRY_REQUEST, None)
            return
        if response is None or response.role != "assistant":
            return
        if response.tools_call_name:
            return
        event.set_extra(QUERY_RETRY_REQUEST, None)
        event.set_extra(QUERY_RETRY_ATTEMPTED, True)
        actions = retry_request.get("actions")
        hint = retry_request.get("hint")
        system = retry_request.get("system") or hint
        tools = retry_request.get("tools")
        if (not isinstance(actions, tuple) or not actions
                or any(value not in QUERY_ACTIONS for value in actions)
                or not isinstance(hint, str) or not isinstance(tools, ToolSet)):
            return
        label = "/".join(actions)
        logger.info(f"名剑{label}请求未调用工具，开始模型补调。")
        try:
            await self._send_query_progress(event, QueryTiming("补查"), "正在补查…")
        except Exception:
            pass
        retry = None
        if self.natural.retry_enabled:
            try:
                async with asyncio.timeout(min(self.natural.timeout_seconds, self.natural.retry_timeout)):
                    provider = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
                    if query_retry_finished(event):
                        return
                    retry = await self.context.llm_generate(
                        chat_provider_id=provider, prompt=route_prompt(event, text), tools=tools,
                        system_prompt=system, tool_choice="required",
                    ) if provider else None
            except Exception as exc:
                logger.warning(f"名剑{label}工具补调失败 类型={type(exc).__name__}")
                LAST_ERROR[chat_key(event)] = (type(exc).__name__, monotonic())
                retry = None
        if query_retry_finished(event):
            return
        if (retry and retry.role in {"assistant", "tool"} and retry.tools_call_name == ["mingjian_query"]
                and len(retry.tools_call_args or []) == 1):
            tool_call = retry.tools_call_args[0]
            chosen = tool_call.get("action") if isinstance(tool_call, dict) else None
            args = normalize_args(chosen or "",
                                  tool_call.get("args") if isinstance(tool_call, dict) else None, text)
            if chosen in actions and args is not None and valid_chat_args(args):
                args = [value.strip() for value in args]
                if chosen in {"名剑帮助", "名剑流程"}:
                    valid = True
                else:
                    try:
                        parsed = await self._query(event, chosen, list(args))
                        valid = (chosen != "强度"
                                 or getattr(parsed, "role_type", None) == retry_request.get("role_type"))
                    except QueryError:
                        valid = False
                if valid:
                    logger.info(f"名剑工具补调成功 动作={chosen} 参数={args}")
                    event.set_extra("mingjian_stage", "工具补调")
                    async for _ in self.mingjian_query(event, chosen, args):
                        pass
                    if query_retry_finished(event):
                        return
        if not query_retry_finished(event) and self.natural.text_route_enabled:
            routed = await self._text_route(event, text)
            if routed is not None and routed[0] in actions:
                action, route_args = routed
                route_args = normalize_args(action, route_args, text)
                valid = True
                if action not in {"名剑帮助", "名剑流程"}:
                    try:
                        await self._query(event, action, list(route_args))
                    except QueryError:
                        valid = False
                if valid:
                    logger.info(f"名剑文本补调成功 动作={action} 参数={route_args}")
                    event.set_extra("mingjian_stage", "文本补调")
                    async for _ in self.mingjian_query(event, action, route_args):
                        pass
                    if query_retry_finished(event):
                        return
        if not query_retry_finished(event):
            routed_fallback = fallback_route(text, self.aliases)
            if routed_fallback and routed_fallback[0] in actions:
                action, args = routed_fallback
                try:
                    await self._query(event, action, list(args))
                except QueryError:
                    pass
                else:
                    logger.info(f"名剑规则兜底成功 动作={action} 参数={args}")
                    event.set_extra("mingjian_stage", "规则兜底")
                    async for _ in self.mingjian_query(event, action, args):
                        pass
                    if query_retry_finished(event):
                        return
        fallback = retry_request.get("fallback")
        if (actions == ("环境",) and valid_chat_args(fallback)
                and not query_retry_finished(event)):
            logger.info("名剑环境补调未返回有效工具调用，按消息中的排队类型直接查询。")
            try:
                async for _ in self.mingjian_query(event, "环境", fallback):
                    pass
            except Exception as exc:
                logger.warning(f"名剑环境兜底查询失败 类型={type(exc).__name__}")
            if query_retry_finished(event):
                return
        QUERY_STATS["fail"] += 1
        LAST_STAGE[chat_key(event)] = ("查询失败", monotonic())
        LAST_ERROR[chat_key(event)] = ("模型未完成", monotonic())
        response.result_chain = None
        if actions == ("强度",):
            response.completion_text = (
                "模型未成功调用名剑心法强度工具，没有生成强度图。可以发送 /剑三 强度 或 /剑三 强度 治疗。"
            )
        elif actions == ("排行",):
            response.completion_text = "模型未成功调用名剑排行榜工具，没有生成榜单图。可以发送 /剑三 排行。"
        elif actions == ("环境",):
            response.completion_text = "模型未成功调用名剑环境工具，没有生成环境图。可以发送 /剑三 环境 或 /剑三 环境 单排。"
        else:
            command = ACTION_COMMANDS.get(actions[0], "/名剑帮助")
            response.completion_text = (
                f"模型没有完成这次查询，没有生成图卡。可以发送 {command} 直接查询，或再发一次。"
            )

    async def _text_route(self, event: AstrMessageEvent, text: str) -> tuple[str, list[str]] | None:
        """工具调用失败后，用普通文本 JSON 再解析一次意图，覆盖全部动作。"""
        try:
            provider = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
            if not provider:
                return None
            date = datetime.now(CHINA_TIME).date().isoformat()
            async with asyncio.timeout(min(self.natural.timeout_seconds, QUERY_RETRY_TIMEOUT)):
                response = await self.context.llm_generate(
                    chat_provider_id=provider,
                    prompt=f"北京时间日期 {date}\n用户原话\n{route_prompt(event, text)}",
                    system_prompt=NATURAL_SYSTEM_PROMPT,
                )
        except Exception as exc:
            logger.warning(f"名剑文本补调失败 类型={type(exc).__name__}")
            LAST_ERROR[chat_key(event)] = (type(exc).__name__, monotonic())
            return None
        try:
            return parse_model_result(response.completion_text or "")
        except QueryError:
            return None

    @filter.llm_tool(name="mingjian_digest")
    async def mingjian_digest(self, event: AstrMessageEvent, topic: str = "", args: list[str] | None = None):
        """讨论剑三数据时获取一小段事实摘要，不发送图卡。

        用户用讨论、评价或打法语气问数据时调用，例如「花间游怎么打苍云」「奶花现在强吗」「这版本什么配队好用」。
        只在需要数字依据时调用；明确的看图请求（例如「发一下强度榜」）改用 mingjian_query。每条消息最多一次。

        Args:
            topic(string): 只选 强度、对位 或 配队。
            args(array[string]): 强度填 输出/治疗/组排/单排/2200+ 等；对位填 [我方心法, 敌方心法]；配队填空数组。
        """
        text = normalize("NFKC", event.get_message_str().strip())
        values = args or []
        if topic not in DIGEST_TOPICS:
            yield "摘要主题只支持 强度、对位、配队。"
            return
        if not valid_chat_args(values):
            yield "摘要参数无效，请按工具说明填写。"
            return
        if not chat_only(text) and explicit_request(text) and QUERY_TOPIC_STRONG.search(text):
            yield "这条消息是明确的数据查询，请调用 mingjian_query 发送图卡；mingjian_digest 只用于讨论。"
            return
        if event.get_extra(CHAT_DIGEST_USED):
            yield "本条消息已经取过摘要，请直接结合摘要回答。"
            return
        event.set_extra(CHAT_DIGEST_USED, True)
        try:
            params = await self._query(event, topic, [value.strip() for value in values])
            if isinstance(params, ChoiceMenu):
                yield "参数还需要选择，请改为发送对应指令查询。"
                return
            card = await self.service.run(topic, params, draw=False)
        except QueryError as exc:
            yield f"摘要取数失败：{exc}"
            return
        except Exception as exc:
            logger.warning(f"名剑摘要取数失败 主题={topic} 类型={type(exc).__name__}")
            yield "摘要取数失败，请稍后再试。"
            return
        if not isinstance(card, Card):
            yield "没有可用的数据摘要。"
            return
        event.set_extra(CHAT_TOOL_USED, True)
        yield digest_text(card)

    @filter.llm_tool(name="mingjian_query")
    async def mingjian_query(self, event: AstrMessageEvent, action: str = "", args: list[str] | None = None):
        """发送剑网三名剑大会的数据图卡。仅在用户明确要查战绩、排行、强度统计、奇穴图谱等公开数据时调用。用户一条消息里明确问了两件数据时，可以分别调用两次；最多两次。

        日常聊天、打法建议或模糊评价走原生对话。例如「花间游怎么打苍云」不调用。「花间游最近强吗」先确认要数据还是打法。
        奶妈或治疗心法的强度榜是强度统计。对「查一下奶妈的心法强度排行」调用 action="强度"、args=["治疗"]。如果明确说 2200 分以上，参数填 "2200+"。普通玩家名次榜才用排行。
        DPS（也可写 dps）指输出。对「查一下dps心法强度」「dps强度排行」或「查一下输出心法强度排行」调用 action="强度"、args=["输出"]。
        「查一下本周组排PVP环境」调用 action="环境"、args=["组排"]。「本周」和「当前」是默认范围，可省略。
        「帮我查剑三花间游的 3v3 排行榜，发榜单图」调用 action="排行"、args=["花间游","33"]。图卡由工具发送。
        「给我名剑帮助图」调用 action="名剑帮助"；「给我名剑流程图」调用 action="名剑流程"，这两项 args 均为空数组。
        查战绩时必须有角色名、角色昵称或推栏数字 ID，区服可省略。照抄用户输入的昵称，由插件显示候选。用户没说明的筛选省略，禁止猜测角色、服务器或条件。
        查单场详情、回放或需要评价一场比赛时，先查角色战绩，然后由用户回复编号。图卡与菜单由插件直接发送。

        Args:
            action(string): 只选 战绩、战绩复盘、角色总览、排行、强度、配置总览、对位、环境、配队、分布、每日走势、门派、技能、阵法、奇穴、名剑帮助、名剑流程 中的一项。
            args(array[string]): 每个参数一项，按名剑指令填写。无筛选填空数组。例如「花间游的3v3前100排行」填 ["花间游","33","100"]，「梦江南的云晚归最近5场」填 ["梦江南","云晚归","5场"]。
        """
        text = normalize("NFKC", event.get_message_str().strip())
        logger.info(f"名剑工具调用 操作={action} 参数={args}")
        if chat_only(text):
            yield "这条消息更像打法或评价讨论，请继续原生聊天；需要数据时再调用工具。"
            return
        if not CHAT_QUERY_TOPIC.search(text) and action not in ENTITY_ACTIONS:
            yield "本条消息没有明确的剑三数据主题。请继续原生聊天，意图不明确时可以询问用户。"
            return
        if (not isinstance(action, str) or action not in QUERY_ACTIONS or not valid_chat_args(args)
                or (action in {"名剑帮助", "名剑流程"} and args)):
            yield "名剑查询参数无效，请按工具说明提供一个受支持的操作和参数数组。"
            return
        used = int(event.get_extra(CHAT_TOOL_COUNT) or 0)
        if used >= self.natural.max_calls:
            yield f"本条消息最多查询 {self.natural.max_calls} 次。可让用户回复菜单编号或发起下一次查询。"
            return
        stage = event.get_extra("mingjian_stage") or "模型调用工具"
        now = monotonic()
        if (used == 0 and stage == "模型调用工具" and self.natural.cooldown
                and now - LAST_QUERY_AT.get(chat_key(event), 0.0) < self.natural.cooldown):
            yield f"查询太频繁，请等 {self.natural.cooldown} 秒后再试。"
            return
        LAST_QUERY_AT[chat_key(event)] = now
        event.set_extra(CHAT_TOOL_COUNT, used + 1)
        event.set_extra(CHAT_TOOL_USED, True)
        action = correct_guide_action(text, action)
        args = normalize_args(action, args, text)
        LAST_STAGE[chat_key(event)] = (stage, now)
        QUERY_STATS[{"模型调用工具": "tool", "工具补调": "retry", "文本补调": "text",
                    "规则兜底": "text"}.get(stage, "tool")] += 1

        if action in {"名剑帮助", "名剑流程"}:
            result = await self._guide_result(event, help_card() if action == "名剑帮助" else flow_card())
        else:
            result = None
            async for response in self._execute(event, action, [value.strip() for value in args]):
                result = response
        if result is not None:
            timing = event.get_extra(TIMING_KEY)
            event.set_extra(TIMING_KEY, None)
            await self._send_timed(event, result, timing)
        elif not event.is_stopped():
            logger.warning(f"名剑工具没有生成结果 操作={action}")
            QUERY_STATS["fail"] += 1
            LAST_STAGE[chat_key(event)] = ("空结果", monotonic())
            LAST_ERROR[chat_key(event)] = ("空结果", monotonic())
            await event.send(event.plain_result("没有生成查询结果，请重新发送或发送 /名剑帮助 查看用法。"))
        remember_query(event, text, action)
        self._cache_route(event, text, action, args)
        # The tool already sent its result. Multi-intent messages may run a second tool call.
        event.clear_result()
        if not event.get_extra("mingjian_multi"):
            event.stop_event()
        yield

    async def _execute(self, event: AstrMessageEvent, action: str, args: list[str], *, utterance: str | None = None):
        timing = QueryTiming("自然语言" if utterance is not None else "指令查询")
        key = chat_key(event)
        now = monotonic()
        for expired in [chat for chat, item in _chats.items() if item.expires <= now]:
            expire_menu(expired)
        _expired_menus.pop(key, None)
        previous = _chats.get(key)
        state = _chats[key] = Pending(float("inf") if utterance is not None else now + 120)
        try:
            if utterance is not None:
                action, args = await self.natural.route(
                    utterance, event.unified_msg_origin, on_start=lambda: self._send_query_progress(event, timing),
                )
                if _chats.get(key) is not state:
                    return
                if action in {"名剑帮助", "名剑流程"}:
                    timing.action = action
                    if previous and not previous.busy and previous.can_navigate() and previous.expires > monotonic():
                        _chats[key] = previous
                    card = help_card() if action == "名剑帮助" else flow_card()
                    yield await self._guide_result(event, card, timing)
                    return
            params = await self._query(event, action, args)
            role_query = action in ROLE_QUERY_ACTIONS
            if utterance is None and action == "战绩复盘":
                action = REVIEW_SUMMARY
            if role_query:
                params = self._role_nickname_menu(action, params) or params
            timing.action = action
            response = await self._reply(event, state, action, params, timing, show_progress=utterance is None)
            if response is not None:
                yield self._result_ready(event, response, timing)
            else:
                logger.warning(f"名剑查询没有生成结果 操作={action}")
                yield self._result_ready(
                    event, event.plain_result("本次查询没有生成结果，请重新发送。"), timing,
                )
        except QueryError as exc:
            if _chats.get(key) is state:
                if (utterance is not None and action == "名剑" and previous
                        and not previous.busy and previous.can_navigate() and previous.expires > monotonic()):
                    _chats[key] = previous
                yield self._result_ready(event, event.plain_result(str(exc)), timing)
        finally:
            if _chats.get(key) is state and not state.can_navigate():
                _chats.pop(key, None)

    async def _send_query_progress(self, event: AstrMessageEvent, timing: QueryTiming, label: str = "查询中"):
        chain = []
        platform = event.get_platform_name()
        message_id = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "").strip()
        if platform in REPLY_PLATFORMS and message_id not in {"", "0"}:
            chain.append(components.Reply(id=message_id))
        elif platform == "aiocqhttp" and event.get_group_id():
            sender_id = str(event.get_sender_id())
            if sender_id.isdecimal():
                chain.append(components.At(qq=sender_id))
        chain.append(components.Plain(label))
        started = monotonic()
        try:
            async with asyncio.timeout(3):
                await event.send(event.chain_result(chain))
        except Exception as exc:
            logger.warning(f"名剑发送查询进度失败 平台={platform} 类型={type(exc).__name__}")
        finally:
            timing.progress += monotonic() - started

    async def _reply(self, event: AstrMessageEvent, state: Pending, action: str, params, timing: QueryTiming,
                     *, show_progress: bool = True, menu_prefix: str = ""):
        progress = None
        if show_progress and (not isinstance(params, ChoiceMenu) or params.has_icons()):
            progress = asyncio.create_task(self._send_query_progress(event, timing))
        review = None
        try:
            if isinstance(params, ChoiceMenu):
                result = params
            elif self.natural.review_enabled and action in REVIEW_REPORTS:
                result = await self.service.run(action, params, draw=False, timing=timing)
                if isinstance(result, Card):
                    review = review_data(result)
                    result = await self.service.render_card(result, timing=timing)
            else:
                result = await self.service.run(action, params, timing=timing)
            if _chats.get(chat_key(event)) is not state:
                return None
            if isinstance(result, ChoiceMenu) and result.has_icons():
                image = await self.service.render_menu(result, timing=timing)
                result = MenuImage(image, result, numbers_in_image=True)
        except asyncio.CancelledError:
            if progress is not None:
                progress.cancel()
            raise
        finally:
            if progress is not None:
                await asyncio.gather(progress, return_exceptions=True)
        if _chats.get(chat_key(event)) is not state:
            return None
        menu_message = "请在 2 分钟内直接回复图中的序号。"
        if review and self.natural.review_enabled:
            if isinstance(result, MenuImage):
                menu = result.menu
                result = replace(result, menu=replace(menu, options=(
                    *menu.options, (REVIEW_ACTION, QueryTarget(REVIEW_ACTION, review)),
                )))
                if result.numbers_in_image:
                    menu_message = (f"{len(result.menu.options)}. {REVIEW_ACTION}\n"
                                    "请在 2 分钟内直接回复图中或消息里的序号。")
            else:
                menu = ChoiceMenu("请选择下一步。", action, ((REVIEW_ACTION, QueryTarget(REVIEW_ACTION, review)),))
                result = MenuImage(result, menu)
        if isinstance(result, (ChoiceMenu, MenuImage)):
            state.menu = result.menu if isinstance(result, MenuImage) else result
        if state.can_navigate():
            state.expires = monotonic() + 120
        back_hint = "\n回复 0 返回上一级。" if state.history else ""
        if isinstance(result, ChoiceMenu):
            return event.plain_result(menu_prefix + result.text() + back_hint)
        image = result.image if isinstance(result, MenuImage) else result
        timing.image_bytes = len(image)
        chain = [components.Image.fromBytes(image)]
        if isinstance(result, MenuImage):
            instructions = menu_message if result.numbers_in_image else result.menu.text()
            chain.append(components.Plain("\n" + menu_prefix + instructions + back_hint))
        elif state.history:
            chain.append(components.Plain("\n请在 2 分钟内回复 0 返回上一级。"))
        if action in {"分布", "每日走势"}:
            chain.append(components.Plain(f"\n完整走势：{HOME}ranking/distribution/"))
        if action in {"配队", "配队奇穴"}:
            chain.append(components.Plain(f"完整配置与对位模拟：{HOME}jjc-stats/"))
        return event.chain_result(chain)

    async def _return_to_parent(self, event: AstrMessageEvent, state: Pending):
        if not state.history:
            await event.send(event.plain_result("已经是最上一级，请直接回复当前菜单的序号。"))
            return
        key = chat_key(event)
        parent = state.history[-1]
        restored = _chats[key] = Pending(monotonic() + 120, parent, state.history[:-1], busy=True)
        timing = QueryTiming("返回上一级")
        confirmation = "已返回上一级。\n"
        sent = False
        try:
            try:
                response = await self._reply(event, restored, parent.action, parent, timing,
                                             show_progress=False, menu_prefix=confirmation)
            except QueryError:
                hint = "\n回复 0 返回上一级。" if restored.history else ""
                response = event.plain_result(confirmation + parent.text() + hint)
            if response is not None and _chats.get(key) is restored:
                await self._send_timed(event, response, timing)
                sent = True
        finally:
            if _chats.get(key) is restored:
                if sent:
                    restored.busy = False
                else:
                    _chats[key] = state

    @filter.custom_filter(PendingNumber, priority=100)
    async def choose_number(self, event: AstrMessageEvent):
        """接收当前名剑菜单的纯数字回复。"""
        number = selection_number(event)
        if number is None:
            return
        key = chat_key(event)
        state = pending_menu(event)
        if state is None:
            if key in _expired_menus:
                _expired_menus.pop(key)
                event.stop_event()
                await event.send(event.plain_result("菜单已过期，请重新发送查询指令。"))
            return
        event.stop_event()
        if number == 0:
            await self._return_to_parent(event, state)
            return
        menu = state.menu
        if menu is None:
            await event.send(event.plain_result("当前结果没有编号选项，回复 0 返回上一级。"))
            return
        if not 1 <= number <= len(menu.options):
            hint = " 回复 0 可返回上一级。" if state.history else ""
            await event.send(event.plain_result(f"请输入 1–{len(menu.options)} 之间的序号，可直接重选。" + hint))
            return
        action = menu.action
        params = menu.options[number - 1][1]
        if isinstance(params, QueryTarget):
            action, params = params.action, params.params
        timing = QueryTiming(action)
        history = state.history if action in {"导出回放", REVIEW_ACTION} else (*state.history, menu)[-MAX_MENU_HISTORY:]
        selected = _chats[key] = Pending(monotonic() + 120, history=history, busy=True)
        sent = False
        try:
            if action == "导出回放":
                await self._send_replay(event, params)
                state.expires = monotonic() + 120
                return
            if action == REVIEW_ACTION:
                if self.natural.review_enabled:
                    await self._send_query_progress(event, timing)
                if _chats.get(key) is not selected:
                    return
                review = await self.natural.review(params, event.unified_msg_origin)
                if _chats.get(key) is selected:
                    state.expires = monotonic() + 120
                    await self._send_timed(event, event.plain_result(f"{REVIEW_ACTION}\n{review}"), timing)
                return
            response = await self._reply(event, selected, action, params, timing)
            if response is not None:
                await self._send_timed(event, response, timing)
                sent = True
        except QueryError as exc:
            if _chats.get(key) is selected:
                cooldown = exc.retry_after if isinstance(exc, ApiError) and exc.status == 429 else 0
                state.expires = monotonic() + cooldown + 120
                message = str(exc)
                if action != "导出回放":
                    when = "冷却结束后" if cooldown else ""
                    message += f"\n原菜单已保留，{when}可再次回复 {number} 重试。"
                await self._send_timed(event, event.plain_result(message), timing)
        finally:
            if _chats.get(key) is selected:
                if sent:
                    selected.busy = False
                else:
                    _chats[key] = state

    async def _send_replay(self, event: AstrMessageEvent, export: ReplayExport):
        if len(export.content) > MAX_EXPORT_BYTES:
            raise QueryError("回放 JSON 超过 4 MB，可在原站回放页下载。")
        try:
            with TemporaryDirectory(prefix="mingjian-replay-") as directory:
                path = Path(directory) / export.name
                path.write_bytes(export.content)
                await event.send(event.chain_result([components.File(file=str(path), name=export.name)]))
        except Exception as exc:
            raise QueryError("当前聊天平台未能发送回放文件，可以在原站回放页导出 JSON。") from exc

