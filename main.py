import asyncio
import re
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from unicodedata import normalize

from astrbot.api import AstrBotConfig, logger
from astrbot.api import message_components as components
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

from .ai_review import REVIEW_ACTION, REVIEW_REPORTS, review_data
from .aliases import MAX_ROLES_PER_NICKNAME, NameAliases, index_role_nicknames
from .cards import Card, MenuImage
from .dashboard import register_settings_page
from .data import ApiError
from .flow_content import flow_card
from .help_content import help_card
from .labels import alias_key
from .matches import RecordQuery, Role
from .matchups import parse_matchup
from .natural import QUERY_ACTIONS, NaturalLanguage
from .query import (
    REVIEW_SUMMARY, ChoiceMenu, HistoryFilter, MODES, QueryError, QueryTarget, parse_configuration,
    parse_history, parse_ranking, parse_strength, parse_tier, parse_trend,
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
ROLE_QUERY_ACTIONS = {"战绩", "战绩复盘", "角色总览"}
CHAT_QUERY_TOPIC = re.compile(
    r"剑(?:网)?[三3]|名剑|推栏|竞技场|战绩|对局|回放|奇穴|排行|排名|榜单|胜率|出场率|"
    r"心法|门派|配队|阵容|段位|积分|技能|阵法|走势|强度|分布|对位|配置总览|角色总览|复盘|环境|"
    r"最近[零一二三四五六七八九十两\d]+\s*场"
)
CHAT_QUERY_REQUEST = re.compile(
    r"^\s*看|查|搜|检索|看看|看一?下|(?:想|要|给我|帮我)看|我要|列出|显示|调出|拉取|"
    r"给我|发(?:我|个|一张|张|一份|份)|多少|第几|什么名次|复盘|统计|图谱|排行榜|资料|帮助|流程图"
)
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
    rf"{STRENGTH_ROLE}\s*(?:的\s*)?(?:心法\s*)?强度\s*(?:排行榜?|榜单?)[。！？!?]?",
    re.IGNORECASE,
)
ROLE_STRENGTH_TOOL_HINT = (
    "用户明确要查询心法强度榜时，请调用 mingjian_query，action 填强度。"
    "奶妈或治疗的 args 加入治疗；DPS 或输出的 args 加入输出。只保留用户明确给出的其他筛选。"
    "普通玩家名次榜使用排行。"
)


def chat_key(event: AstrMessageEvent) -> tuple[str, str, str]:
    return event.unified_msg_origin, str(event.get_self_id()), str(event.get_sender_id())


def selection_number(event: AstrMessageEvent) -> int | None:
    text = normalize("NFKC", event.get_message_str().strip())
    return int(text) if re.fullmatch(r"[0-9]{1,3}", text) else None


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
        if match[1].casefold() not in {"我", "你", "他", "她", "它", "ta", "对方", "这个人", "那个人"}:
            return [match[1]]
    return None


class DirectRecord(filter.CustomFilter):
    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        return direct_record_args(event) is not None


@register("astrbot_plugin_mingjian", "Codex", "剑网三名剑大会数据查询与图卡", "0.2.46")
class Mingjian(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.aliases = NameAliases.from_config(config)
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
        finally:
            timing.log(event, status)

    async def _query(self, event: AstrMessageEvent, action: str, args: list[str]):
        if action == "配置总览":
            query = parse_configuration(args, heart_aliases=self.aliases.hearts)
            return replace(query, school=self.aliases.heart(query.school), search=self.aliases.lineup(query.search))
        if action == "对位":
            return parse_matchup(args, self.aliases)
        if action == "环境":
            if not args:
                return ("组排",)
            if len(args) == 1 and args[0] in {"组排", "单排"}:
                return (args[0],)
            raise QueryError("用法：/剑三 环境 [组排/单排]")
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
        async for response in self._execute(event, "战绩", args):
            timing = event.get_extra(TIMING_KEY)
            event.set_extra(TIMING_KEY, None)
            await self._send_timed(event, response, timing)

    @filter.on_llm_request()
    async def guide_role_strength_query(self, event: AstrMessageEvent, request: ProviderRequest):
        """为模型补充输出和治疗强度的工具用法。"""
        tool = request.func_tool.get_tool("mingjian_query") if request.func_tool else None
        text = event.get_message_str().strip()
        if (tool and tool.active and ROLE_STRENGTH_TOPIC.search(text)
                and (re.match(QUERY_VERB, text) or BARE_ROLE_STRENGTH.fullmatch(text))):
            request.system_prompt += "\n\n" + ROLE_STRENGTH_TOOL_HINT

    @filter.llm_tool(name="mingjian_query")
    async def mingjian_query(self, event: AstrMessageEvent, action: str = "", args: list[str] | None = None):
        """发送剑网三名剑大会的数据图卡。仅在用户明确要查战绩、排行、强度统计、奇穴图谱等公开数据时调用，每条消息最多一次。

        日常聊天、打法建议或模糊评价走原生对话。例如「花间游怎么打苍云」不调用。「花间游最近强吗」先确认要数据还是打法。
        奶妈或治疗心法的强度榜是强度统计。对「查一下奶妈的心法强度排行」调用 action="强度"、args=["治疗"]。如果明确说 2200 分以上，参数填 "2200+"。普通玩家名次榜才用排行。
        DPS（也可写 dps）指输出。对「dps强度排行」或「查一下输出心法强度排行」调用 action="强度"、args=["输出"]。
        查战绩时必须有角色名、角色昵称或推栏数字 ID，区服可省略。照抄用户输入的昵称，由插件显示候选。用户没说明的筛选省略，禁止猜测角色、服务器或条件。
        查单场详情、回放或需要评价一场比赛时，先查角色战绩，然后由用户回复编号。图卡与菜单由插件直接发送。

        Args:
            action(string): 只选 战绩、战绩复盘、角色总览、排行、强度、配置总览、对位、环境、配队、分布、每日走势、门派、技能、阵法、奇穴、名剑帮助、名剑流程 中的一项。
            args(array[string]): 每个参数一项，按名剑指令填写。无筛选填空数组。例如「花间游的3v3前100排行」填 ["花间游","33","100"]，「梦江南的云晚归最近5场」填 ["梦江南","云晚归","5场"]。
        """
        text = event.get_message_str().strip()
        if not (CHAT_QUERY_TOPIC.search(text) and (CHAT_QUERY_REQUEST.search(text) or BARE_ROLE_STRENGTH.fullmatch(text))):
            yield "本条消息没有明确请求剑三数据。请继续原生聊天，意图不明确时可以询问用户。"
            return
        if (not isinstance(action, str) or action not in QUERY_ACTIONS or not isinstance(args, list)
                or len(args) > 20 or any(not isinstance(value, str) or not value.strip()
                                         or len(value) > 100 or not value.isprintable() for value in args)
                or (action in {"名剑帮助", "名剑流程"} and args)):
            yield "名剑查询参数无效，请按工具说明提供一个受支持的操作和参数数组。"
            return
        if event.get_extra(CHAT_TOOL_USED):
            yield "本条消息已经调用过名剑查询。可让用户回复菜单编号或发起下一次查询。"
            return
        event.set_extra(CHAT_TOOL_USED, True)

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
        # The tool already sent its result. Finish this event without a second AI reply.
        event.clear_result()
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
        except QueryError as exc:
            if _chats.get(key) is state:
                if (utterance is not None and action == "名剑" and previous
                        and not previous.busy and previous.can_navigate() and previous.expires > monotonic()):
                    _chats[key] = previous
                yield self._result_ready(event, event.plain_result(str(exc)), timing)
        finally:
            if _chats.get(key) is state and not state.can_navigate():
                _chats.pop(key, None)

    async def _send_query_progress(self, event: AstrMessageEvent, timing: QueryTiming):
        chain = []
        platform = event.get_platform_name()
        message_id = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "").strip()
        if platform in REPLY_PLATFORMS and message_id not in {"", "0"}:
            chain.append(components.Reply(id=message_id))
        elif platform == "aiocqhttp" and event.get_group_id():
            sender_id = str(event.get_sender_id())
            if sender_id.isdecimal():
                chain.append(components.At(qq=sender_id))
        chain.append(components.Plain("查询中"))
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

