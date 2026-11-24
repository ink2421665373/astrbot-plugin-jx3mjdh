import asyncio
import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import partial
from http.client import HTTPSConnection, IncompleteRead
from math import ceil
from os import fsync
from pathlib import Path
from socket import gaierror
from ssl import SSLCertVerificationError, SSLEOFError
from tempfile import NamedTemporaryFile
from time import monotonic, time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPSHandler, Request, build_opener

from astrbot.api import logger

from .labels import SITE
from .query import QueryError


USER_AGENT = "AstrBot-Mingjian/0.2.72"
MAX_JSON_BYTES = 12 * 1024 * 1024
REQUEST_GAP = 2
DEFAULT_COOLDOWN = 60
SCHOOL_ROUTE = "schools/data"
SCHOOL_TTL = 3600
SCHOOL_MAX_AGE = 24 * SCHOOL_TTL
MAX_SCHOOL_CACHE_BYTES = MAX_JSON_BYTES + 512
RANK_TAGS_ROUTE = "rank/time-tags"
RANK_TAGS_TTL = 600
RANK_TAGS_MAX_AGE = 24 * 3600
MAX_RANK_TAGS_CACHE_BYTES = 256 * 1024
MATCH_RESPONSES = {"match/detail": "详情", "match/replay": "回放数据"}


@dataclass
class OriginTrace:
    stage: str = "DNS/TCP连接"
    peer: str = "-"


class TracedHTTPSConnection(HTTPSConnection):
    def __init__(self, *args, trace: OriginTrace, **kwargs):
        super().__init__(*args, **kwargs)
        self.trace = trace
        self._base_create_connection = self._create_connection
        self._create_connection = self._connect_peer

    def _connect_peer(self, *args, **kwargs):
        self.trace.stage, self.trace.peer = "DNS/TCP连接", "-"
        sock = self._base_create_connection(*args, **kwargs)
        self.trace.peer = sock.getpeername()[0]
        self.trace.stage = "TLS握手"
        return sock

    def _tunnel(self):
        self.trace.stage = "代理隧道"
        super()._tunnel()
        self.trace.stage = "TLS握手"

    def connect(self):
        super().connect()
        self.trace.stage = "发送HTTP请求"

    def getresponse(self):
        self.trace.stage = "读取HTTP响应头"
        return super().getresponse()


class TracedHTTPSHandler(HTTPSHandler):
    def __init__(self, trace: OriginTrace):
        super().__init__()
        self.trace = trace

    def https_open(self, request):
        return self.do_open(partial(TracedHTTPSConnection, trace=self.trace), request)


class OriginUnavailable(QueryError):
    def __init__(self, message: str, trace: OriginTrace | None = None):
        super().__init__(message)
        self.trace = trace


class OriginDisconnected(OriginUnavailable):
    pass


class ApiError(QueryError):
    def __init__(self, status: int, details=None, retry_after: float = 0):
        self.status = status
        self.details = details if isinstance(details, dict) else {}
        self.retry_after = retry_after
        message = {
            401: "原站要求授权才能读取这项数据。",
            403: "原站拒绝了这项数据查询。",
            404: "原站目前没有这项查询的数据。",
            429: "原站限制了查询频率，请稍后再试。",
        }.get(status, f"原站数据查询暂不可用（HTTP {status}）。")
        if status == 429 and retry_after > 0:
            message = f"原站限制了查询频率，请约 {ceil(retry_after)} 秒后再试。"
        super().__init__(message)


def error_kind(error: Exception) -> str:
    cause = error.__cause__ or error
    if isinstance(cause, (ApiError, HTTPError)):
        status = cause.status if isinstance(cause, ApiError) else cause.code
        return f"HTTP{status}"
    if isinstance(cause, URLError) and isinstance(cause.reason, Exception):
        cause = cause.reason
    return type(cause).__name__


def retry_delay(value: str) -> float:
    value = value.strip()
    if value.isascii() and value.isdecimal() and len(value) <= 12:
        return int(value)
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(0, date.timestamp() - time())
    except (ValueError, TypeError, OverflowError, OSError):
        return 0


def read_json(url: str, body: bytes | None) -> dict:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers)
    trace = OriginTrace()
    try:
        with build_opener(TracedHTTPSHandler(trace)).open(request, timeout=12) as response:
            final_url = urlsplit(response.geturl())
            if final_url.scheme != "https" or final_url.hostname != urlsplit(SITE).hostname:
                raise QueryError("原站数据请求跳转到了其他站点。")
            trace.stage = "读取HTTP响应体"
            content = response.read(MAX_JSON_BYTES + 1)
            response_headers = getattr(response, "headers", None) or {}
            size = response_headers.get("Content-Length", "").strip()
            if (len(content) <= MAX_JSON_BYTES and response_headers.get("Transfer-Encoding", "").lower() != "chunked"
                    and len(size) <= 18 and size.isascii() and size.isdecimal()):
                remaining = int(size) - len(content)
                if remaining > 0:
                    raise IncompleteRead(content, remaining)
    except HTTPError as exc:
        with exc:
            details = None
            if exc.code in (400, 404, 409, 422):
                try:
                    details = json.loads(exc.read(128 * 1024))
                except (ValueError, OSError, IncompleteRead):
                    pass
            delay = retry_delay(exc.headers.get("Retry-After") or "") if exc.code == 429 and exc.headers else 0
            raise ApiError(exc.code, details, delay) from None
    except (OSError, IncompleteRead) as exc:
        cause = exc.reason if isinstance(exc, URLError) else exc
        if isinstance(cause, gaierror):
            trace.stage = "DNS解析"
        if isinstance(cause, (SSLEOFError, ConnectionResetError, ConnectionAbortedError, IncompleteRead)):
            raise OriginDisconnected("与原站的连接中途断开，请稍后再试。", trace) from exc
        if isinstance(cause, SSLCertVerificationError):
            raise QueryError("无法校验原站 HTTPS 证书，请检查 AstrBot 所在环境的证书和代理。") from exc
        if isinstance(cause, TimeoutError):
            raise OriginUnavailable("请求原站超时，请稍后再试。", trace) from exc
        raise OriginUnavailable("连不上原站数据，请检查 AstrBot 的网络后再试。", trace) from exc
    if len(content) > MAX_JSON_BYTES:
        raise QueryError("原站返回的数据太大，暂时无法出图。")
    try:
        document = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise QueryError("原站没有返回可读取的 JSON 数据。") from exc
    if not isinstance(document, dict):
        raise QueryError("原站数据格式有变化。")
    return document


@dataclass(frozen=True)
class DataSnapshot:
    document: dict
    saved_at: datetime
    saved_monotonic: float
    restored: bool = False


SchoolSnapshot = DataSnapshot


def valid_school_document(document: object) -> bool:
    if not isinstance(document, dict) or document.get("success") is False or document.get("error"):
        return False
    forces = document.get("forces")
    return (isinstance(forces, list) and bool(forces)
            and all(isinstance(force, dict) and force.get("name") for force in forces))


def valid_rank_tags(document: object) -> bool:
    if not isinstance(document, dict) or document.get("success") is False or document.get("error"):
        return False
    tags = payload(document)
    weeks = tags.get("week")
    latest = tags.get("defaultWeek")
    return (isinstance(weeks, list) and any(type(tag) is int and tag > 0 for tag in weeks)
            and (latest is None or type(latest) is int and latest >= 0)
            and (tags.get("seasons") is None or isinstance(tags["seasons"], list)))


def load_snapshot(path: Path, validator, max_bytes: int, max_age: int, label: str) -> DataSnapshot | None:
    try:
        with path.open("rb") as file:
            raw = file.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError("cache too large")
        stored = json.loads(raw)
        if not isinstance(stored, dict) or not validator(stored.get("document")):
            raise ValueError("invalid snapshot")
        saved_at = datetime.fromisoformat(stored["saved_at"])
        if saved_at.tzinfo is None:
            raise ValueError("cache timestamp has no timezone")
        age = (datetime.now(timezone.utc) - saved_at).total_seconds()
        if 0 <= age < max_age:
            return DataSnapshot(stored["document"], saved_at, monotonic() - age, restored=True)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError) as exc:
        logger.warning(f"名剑读取{label}缓存失败 类型={error_kind(exc)}")
    return None


def save_snapshot(path: Path, snapshot: DataSnapshot, max_bytes: int, label: str):
    temporary = None
    try:
        content = json.dumps({"saved_at": snapshot.saved_at.isoformat(), "document": snapshot.document},
                             ensure_ascii=False, separators=(",", ":")).encode("utf-8", errors="backslashreplace")
        if len(content) > max_bytes:
            raise ValueError("cache too large")
        with NamedTemporaryFile(dir=path.parent, prefix=f".{path.stem}-", delete=False) as file:
            temporary = Path(file.name)
            file.write(content)
            file.flush()
            fsync(file.fileno())
        temporary.replace(path)
        temporary = None
    except (OSError, ValueError, RecursionError) as exc:
        logger.warning(f"名剑保存{label}缓存失败 类型={error_kind(exc)}")
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(f"名剑清理{label}缓存临时文件失败 类型={error_kind(exc)}")


def load_school_snapshot(path: Path) -> SchoolSnapshot | None:
    return load_snapshot(path, valid_school_document, MAX_SCHOOL_CACHE_BYTES, SCHOOL_MAX_AGE, "门派")


def save_school_snapshot(path: Path, snapshot: SchoolSnapshot):
    save_snapshot(path, snapshot, MAX_SCHOOL_CACHE_BYTES, "门派")


class DataClient:
    def __init__(self, school_cache_path: Path | None = None):
        self.cache = OrderedDict()
        self.lock = asyncio.Lock()
        self.completed = 0.0
        self.limited_until = 0.0
        self.inflight: asyncio.Task[dict] | None = None
        self.inflight_key: tuple[str, bytes | None] | None = None
        self.school_snapshot: SchoolSnapshot | None = None
        self.school_cache_path = school_cache_path
        self.school_cache_loaded = False
        self.rank_tags_snapshot: DataSnapshot | None = None
        self.rank_tags_cache_path = school_cache_path.with_name("rank-time-tags-cache.json") if school_cache_path else None
        self.rank_tags_cache_loaded = False

    async def close(self):
        async with self.lock:
            if self.inflight is not None:
                try:
                    await asyncio.shield(self.inflight)
                except QueryError:
                    pass
            self.clear_cache()

    def clear_cache(self):
        self.cache.clear()
        self.school_snapshot = None
        self.rank_tags_snapshot = None

    async def get_school_data(self) -> tuple[dict, datetime | None]:
        if self.school_cache_path is not None and not self.school_cache_loaded:
            async with self.lock:
                if not self.school_cache_loaded:
                    if self.school_snapshot is None:
                        self.school_snapshot = await asyncio.to_thread(load_school_snapshot, self.school_cache_path)
                    self.school_cache_loaded = True
        snapshot = self.school_snapshot
        if snapshot and monotonic() - snapshot.saved_monotonic < SCHOOL_TTL:
            return snapshot.document, snapshot.saved_at if snapshot.restored else None
        try:
            return await self.get(SCHOOL_ROUTE, ttl=SCHOOL_TTL), None
        except (OriginUnavailable, ApiError) as exc:
            snapshot = self.school_snapshot
            if not snapshot or monotonic() - snapshot.saved_monotonic >= SCHOOL_MAX_AGE:
                raise
            if isinstance(exc, ApiError) and exc.status != 429 and not 500 <= exc.status < 600:
                raise
            logger.warning(f"名剑使用门派缓存 接口={SCHOOL_ROUTE} 缓存时间={snapshot.saved_at.isoformat()} "
                           f"类型={error_kind(exc)}")
            return snapshot.document, snapshot.saved_at

    async def get_rank_time_tags(self) -> tuple[dict, datetime | None]:
        if self.rank_tags_cache_path is not None and not self.rank_tags_cache_loaded:
            async with self.lock:
                if not self.rank_tags_cache_loaded:
                    restored = await asyncio.to_thread(
                        load_snapshot, self.rank_tags_cache_path, valid_rank_tags,
                        MAX_RANK_TAGS_CACHE_BYTES, RANK_TAGS_MAX_AGE, "榜单周期",
                    )
                    if self.rank_tags_snapshot is None:
                        self.rank_tags_snapshot = restored
                    self.rank_tags_cache_loaded = True
        snapshot = self.rank_tags_snapshot
        if snapshot and monotonic() - snapshot.saved_monotonic < RANK_TAGS_TTL:
            return snapshot.document, snapshot.saved_at
        try:
            document = await self.get(RANK_TAGS_ROUTE, ttl=RANK_TAGS_TTL)
        except (OriginUnavailable, ApiError) as exc:
            if isinstance(exc, ApiError) and exc.status != 429 and not 500 <= exc.status < 600:
                raise
            snapshot = self.rank_tags_snapshot
            if not snapshot or monotonic() - snapshot.saved_monotonic >= RANK_TAGS_MAX_AGE:
                raise
            logger.warning(f"名剑使用榜单周期缓存 接口={RANK_TAGS_ROUTE} 缓存时间={snapshot.saved_at.isoformat()} "
                           f"类型={error_kind(exc)}")
            return snapshot.document, snapshot.saved_at
        snapshot = self.rank_tags_snapshot
        return document, snapshot.saved_at if snapshot is not None else None

    async def _fetch(self, route: str, url: str, body: bytes | None, match_id: str | None) -> dict:
        for attempt in (1, 2, 3):
            try:
                result = await self._fetch_once(route, url, body, attempt, match_id)
                break
            except OriginDisconnected:
                if attempt == 3:
                    raise
                await asyncio.sleep(REQUEST_GAP if attempt == 1 else REQUEST_GAP * 2)
        if route == SCHOOL_ROUTE:
            snapshot = self.school_snapshot = SchoolSnapshot(result, datetime.now(timezone.utc), self.completed)
            self.school_cache_loaded = True
            if self.school_cache_path is not None:
                await asyncio.to_thread(save_school_snapshot, self.school_cache_path, snapshot)
        elif route == RANK_TAGS_ROUTE:
            snapshot = self.rank_tags_snapshot = DataSnapshot(result, datetime.now(timezone.utc), self.completed)
            self.rank_tags_cache_loaded = True
            if self.rank_tags_cache_path is not None:
                await asyncio.to_thread(save_snapshot, self.rank_tags_cache_path, snapshot,
                                        MAX_RANK_TAGS_CACHE_BYTES, "榜单周期")
        return result

    async def _fetch_once(self, route: str, url: str, body: bytes | None, attempt: int, match_id: str | None) -> dict:
        started = monotonic()
        try:
            result = await asyncio.to_thread(read_json, url, body)
            if route == SCHOOL_ROUTE and not valid_school_document(result):
                raise QueryError("原站暂未返回可用的门派资料。")
            if route == RANK_TAGS_ROUTE and not valid_rank_tags(result):
                raise OriginUnavailable("暂未返回可用的排行榜周期。")
            if match_id is not None:
                match_document(result, route, match_id)
        except QueryError as exc:
            trace = exc.trace if isinstance(exc, OriginUnavailable) else None
            details = f" 阶段={trace.stage} TCP对端={trace.peer}" if trace else ""
            logger.warning(f"名剑取数失败 接口={route} 尝试={attempt} "
                           f"耗时={monotonic() - started:.2f}s 类型={error_kind(exc)}{details}")
            if isinstance(exc, ApiError) and exc.status == 429:
                delay = max(DEFAULT_COOLDOWN, exc.retry_after)
                self.limited_until = monotonic() + delay
                raise ApiError(429, retry_after=delay) from None
            raise
        else:
            if attempt > 1:
                logger.info(f"名剑取数重试成功 接口={route} 尝试={attempt} 耗时={monotonic() - started:.2f}s")
            return result
        finally:
            self.completed = monotonic()

    async def _fetch_and_cache(self, route: str, key: tuple[str, bytes | None], ttl: int, match_id: str | None) -> dict:
        try:
            url, body = key
            result = await self._fetch(route, url, body, match_id)
            if result.get("success") is not False and not result.get("error"):
                self.cache[key] = (self.completed + ttl, result)
                self.cache.move_to_end(key)
                while len(self.cache) > 32:
                    self.cache.popitem(last=False)
            return result
        finally:
            self.inflight = None
            self.inflight_key = None

    async def get(self, route: str, params: dict | None = None, *, body: dict | None = None,
                  ttl: int = 90) -> dict:
        url = SITE + "api/" + route
        if params:
            url += "?" + urlencode(sorted((key, value) for key, value in params.items() if value is not None))
        payload = json.dumps(body, ensure_ascii=False, sort_keys=True).encode() if body is not None else None
        key = (url, payload)
        match_id = str((body or {}).get("match_id") or "") if route in MATCH_RESPONSES else None
        if self.inflight is not None and self.inflight_key == key:
            return await asyncio.shield(self.inflight)
        async with self.lock:
            cached = self.cache.get(key)
            now = monotonic()
            if cached and now < cached[0]:
                self.cache.move_to_end(key)
                return cached[1]
            if self.inflight is not None:
                if self.inflight_key == key:
                    return await asyncio.shield(self.inflight)
                try:
                    await asyncio.shield(self.inflight)
                except QueryError:
                    pass
                now = monotonic()
            if now < self.limited_until:
                raise ApiError(429, retry_after=self.limited_until - now)
            await asyncio.sleep(max(0, REQUEST_GAP - (now - self.completed)))
            self.inflight_key = key
            fetch = self.inflight = asyncio.create_task(self._fetch_and_cache(route, key, ttl, match_id))
            fetch.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
            return await asyncio.shield(fetch)


def payload(response: dict) -> dict:
    nested = response.get("data")
    return nested if isinstance(nested, dict) else response


def match_document(response: dict, route: str, match_id: str) -> dict:
    document = payload(response)
    if not match_id or str(document.get("match_id", "")) != match_id:
        raise QueryError(f"原站没有返回这场对局的{MATCH_RESPONSES[route]}。")
    return document


def percent(number, places: int = 1) -> str:
    return f"{number * 100:.{places}f}%" if isinstance(number, (int, float)) else "—"


def field(obj: dict, name: str, default="—") -> str:
    value = obj.get(name)
    return str(value) if value is not None and value != "" else default
