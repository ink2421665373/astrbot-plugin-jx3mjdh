import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from pathlib import Path
from time import monotonic
from weakref import ReferenceType, ref

from astrbot.api import logger

from .cards import Card, MenuImage, menu_table
from .configurations import configuration, environment, fixed_team, popular_teams, team_detail
from .data import DataClient, error_kind
from .icon_cache import IconCache
from .labels import SITE
from .matches import match_detail, record, role_lineups
from .matchups import matchup, matchup_samples
from .query import REVIEW_SUMMARY, ChoiceMenu, QueryError
from .rankings import distribution, ranking, ranking_choices, strength, strength_periods, strength_recommendations
from .render import render_image
from .replays import replay_card
from .role_analysis import ReviewBatch, ReviewCard, battle_review, retry_review, review_card, review_queue_view, role_overview
from .schools import CachedSchoolResult, school
from .timing import QueryTiming


HOME = SITE
HANDLERS = {
    "排行": ranking, "排行筛选": ranking_choices, "强度": strength, "配置总览": configuration,
    "分布": distribution, "每日走势": distribution, "环境": environment,
    "配队奇穴": team_detail, "配队": popular_teams, "战绩": record, "对局详情": match_detail,
    "固定队详情": fixed_team, "角色配队": role_lineups, "强度周期": strength_periods,
    "强度推荐": strength_recommendations, "对位": matchup, "对位样例": matchup_samples,
    "战绩复盘": battle_review, "复盘补查": retry_review, "复盘排队筛选": review_queue_view, "角色总览": role_overview,
    REVIEW_SUMMARY: partial(battle_review, summary_only=True),
}
SCHOOL_ACTIONS = {"门派", "技能", "奇穴", "阵法"}
MAX_CACHED_IMAGES = 16
MAX_IMAGE_CACHE_BYTES = 16 * 1024 * 1024


class MingjianService:
    def __init__(self, school_cache_path: Path | None = None):
        self.client = DataClient(school_cache_path)
        self.icon_cache = IconCache(school_cache_path.parent / "icon-cache") if school_cache_path is not None else None
        self.lock = asyncio.Lock()
        self.cache = OrderedDict()
        self.pending = 0
        self.completed_at = 0.0
        self.render_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mingjian-render")
        self.render_pending = 0
        self.image_cache: OrderedDict[int, tuple[ReferenceType[Card], bytes]] = OrderedDict()

    async def close(self):
        self.render_executor.shutdown(wait=False, cancel_futures=True)
        async with self.lock:
            self.cache.clear()
            self.image_cache.clear()
            await self.client.close()

    def _cached(self, key):
        entry = self.cache.get(key)
        return entry if entry and monotonic() - entry[0] < 90 else None

    def _store_card(self, key, card, saved_at):
        self.cache[key] = (saved_at, card)
        self.cache.move_to_end(key)
        while len(self.cache) > 24:
            self.cache.popitem(last=False)

    async def _get_card(self, action: str, args, timing: QueryTiming | None = None):
        if action == "回放流水":
            return replay_card(args)
        if action == "复盘排队筛选":
            return await review_queue_view(self.client, args)
        if timing is not None:
            timing.queue_started = monotonic()
        async with self.lock:
            key = (action, args)
            cached = self._cached(key)
            if cached:
                if timing is not None:
                    timing.finish_queue()
                    timing.card_hit = True
                return cached[1]
            root_key = None
            root = None
            if isinstance(args, ReviewBatch) and args.source is not None:
                root_key = (args.source.action, args.source.args)
                entry = self._cached(root_key)
                if entry and isinstance(entry[1], ReviewCard):
                    latest = entry[1].batch
                    if latest.source is args.source:
                        root = entry
                        args = replace(latest, request=args.request)
            await asyncio.sleep(max(0, 4 - (monotonic() - self.completed_at)))
            started = monotonic()
            if timing is not None:
                timing.finish_queue()
            try:
                if action in SCHOOL_ACTIONS:
                    card = await school(self.client, action, args)
                elif action == "配队":
                    card = await popular_teams(self.client)
                else:
                    card = await HANDLERS[action](self.client, args)
            finally:
                self.completed_at = monotonic()
                if timing is not None:
                    timing.data += self.completed_at - started
            if isinstance(card, CachedSchoolResult):
                return card.card
            now = monotonic()
            limited = now < getattr(self.client, "limited_until", 0)
            if root and isinstance(card, ReviewCard):
                saved_at, original = root
                original_request = original.batch.request
                if now - saved_at < 90:
                    refreshed = card
                    if limited or card.batch.request != original_request:
                        refreshed = review_card(replace(card.batch, request=original_request))
                    self._store_card(root_key, refreshed, saved_at)
            elif root_key is None and action != "复盘补查" and not limited:
                self._store_card(key, card, now)
            return card

    async def run(self, action: str, args, draw: bool = True, *, timing: QueryTiming | None = None):
        started = monotonic()
        counted = False
        try:
            async with asyncio.timeout(100):
                cached = None if action in {"回放流水", "复盘排队筛选"} else self._cached((action, args))
                if cached:
                    if timing is not None:
                        timing.card_hit = True
                    card = cached[1]
                else:
                    if self.pending >= 4:
                        raise QueryError("查询队列已满，请稍后再发指令。")
                    self.pending += 1
                    counted = True
                    card = await self._get_card(action, args, timing)
                return await self.render_card(card, timing=timing) if draw and isinstance(card, Card) else card
        except TimeoutError as exc:
            logger.warning(f"名剑查询超时 操作={action} 耗时={monotonic() - started:.2f}s")
            raise QueryError("查数据或出图超时，请稍后再试。") from exc
        except (KeyError, TypeError, IndexError) as exc:
            logger.warning(f"名剑数据处理失败 操作={action} 耗时={monotonic() - started:.2f}s 类型={error_kind(exc)}")
            raise QueryError("原站返回的数据格式有变化，暂时无法使用。") from exc
        finally:
            if timing is not None:
                timing.finish_queue()
            if counted:
                self.pending -= 1

    async def render_card(self, card: Card, *, timing: QueryTiming | None = None) -> bytes | MenuImage:
        key = id(card)
        numbers_in_image = bool(card.menu and (card.leaderboard_numbers or card.menu.has_icons()))
        cached = self.image_cache.get(key)
        if cached and cached[0]() is card:
            self.image_cache.move_to_end(key)
            if timing is not None:
                timing.image_hit = True
            png = cached[1]
            return MenuImage(png, card.menu, numbers_in_image) if card.menu else png
        original = card
        if self.render_pending >= 4:
            raise QueryError("出图队列已满，请稍后再发指令。")
        if numbers_in_image:
            start = max((number or 0 for number in card.leaderboard_numbers), default=0) + 1
            if start <= len(card.menu.options):
                card = replace(card, tables=[*card.tables, menu_table(card.menu, start=start)])
        started = monotonic()
        self.render_pending += 1
        try:
            args = (card, self.icon_cache) if self.icon_cache is not None else (card,)
            frame = await asyncio.get_running_loop().run_in_executor(self.render_executor, render_image, *args)
        except (OSError, RuntimeError, ValueError, TypeError, IndexError) as exc:
            logger.warning(f"名剑出图失败 耗时={monotonic() - started:.2f}s 类型={error_kind(exc)}")
            raise QueryError("本地渲染失败，请检查插件安装包和中文字体文件。") from exc
        finally:
            self.render_pending -= 1
            if timing is not None:
                timing.render += monotonic() - started
        png = frame.png
        if not frame.has_icon_failures and len(png) <= MAX_IMAGE_CACHE_BYTES:
            self.image_cache[key] = (ref(original), png)
            self.image_cache.move_to_end(key)
            while (len(self.image_cache) > MAX_CACHED_IMAGES
                   or sum(len(entry[1]) for entry in self.image_cache.values()) > MAX_IMAGE_CACHE_BYTES):
                self.image_cache.popitem(last=False)
        return MenuImage(png, card.menu, numbers_in_image) if card.menu else png

    async def render_menu(self, menu: ChoiceMenu, *, timing: QueryTiming | None = None) -> bytes:
        try:
            async with asyncio.timeout(30):
                return await self.render_card(Card("名剑查询", tables=[menu_table(menu)]), timing=timing)
        except TimeoutError as exc:
            raise QueryError("编号菜单出图超时，请重新发起查询。") from exc
