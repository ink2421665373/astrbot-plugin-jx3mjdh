import json
from hashlib import sha256
from pathlib import Path

from astrbot.api import logger

from .aliases import (
    ALIAS_SEPARATOR, MAX_ROLES_PER_NICKNAME, ROLE_NICKNAME_FIELDS, NameAliases, index_role_nicknames,
    role_nickname_rows,
)
from .labels import alias_key
from .natural import DEFAULT_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS
from .query import MODES, QueryError, parse_history


PLUGIN = "astrbot_plugin_mingjian"
ALIAS_FIELDS = {
    "heart_aliases": ("心法", {"全部心法"}),
    "server_aliases": ("服务器", {"全部服务器", "全服搜索"}),
}
DEFAULTS = {
    "natural_language_enabled": False,
    "natural_language_provider": "",
    "natural_language_timeout": DEFAULT_TIMEOUT_SECONDS,
    "review_enabled": False,
    "review_persona": "",
    "heart_aliases": (),
    "server_aliases": (),
    "role_nicknames": (),
}


def public_settings(config):
    settings = {key: config.get(key, default) for key, default in DEFAULTS.items()}
    for key in ALIAS_FIELDS:
        rows = settings[key]
        settings[key] = [{name: row[name] if isinstance(row.get(name), str) else "" for name in ("target", "aliases")}
                         for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    settings["role_nicknames"] = role_nickname_rows(settings["role_nicknames"])
    settings["natural_language_enabled"] = settings["natural_language_enabled"] is True
    settings["review_enabled"] = settings["review_enabled"] is True
    for key in ("natural_language_provider", "review_persona"):
        if not isinstance(settings[key], str):
            settings[key] = ""
    timeout = settings["natural_language_timeout"]
    if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
        settings["natural_language_timeout"] = DEFAULT_TIMEOUT_SECONDS
    return settings


def revision(settings):
    data = json.dumps(settings, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return sha256(data).hexdigest()


def normalize_role_nicknames(rows, aliases):
    if not isinstance(rows, list):
        raise ValueError("角色昵称须为列表。")
    links, seen, counts = [], set(), {}
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or any(not isinstance(row.get(key), str) for key in ROLE_NICKNAME_FIELDS):
            raise ValueError(f"请检查角色昵称第 {index} 行。")
        nickname, server, name = (row[key].strip() for key in ROLE_NICKNAME_FIELDS)
        if not any((nickname, server, name)):
            continue
        if not all(value and value.isprintable() and len(value) <= 100 for value in (nickname, server, name)):
            raise ValueError(f"角色昵称第 {index} 行须填写昵称、区服和角色名，每项最多 100 字。")
        key = alias_key(nickname)
        try:
            tokens, _ = parse_history([key], review=True)
        except QueryError:
            tokens = []
        if not tokens or key in MODES:
            raise ValueError(f"角色昵称第 {index} 行的昵称与查询参数重名，请换一个昵称。")
        server = aliases.server(server)
        if alias_key(server) in {"全部服务器", "全服搜索"}:
            raise ValueError(f"角色昵称第 {index} 行须指定角色所在的区服。")
        identity = (key, alias_key(server), alias_key(name))
        if identity in seen:
            raise ValueError(f"角色昵称第 {index} 行与前面的关联重复。")
        seen.add(identity)
        counts[key] = counts.get(key, 0) + 1
        if counts[key] > MAX_ROLES_PER_NICKNAME:
            raise ValueError(f"单个昵称最多关联 {MAX_ROLES_PER_NICKNAME} 个角色。")
        links.append({"__template_key": "link", "nickname": nickname, "server": server, "name": name})
    return links


def normalize_settings(value, current, providers, personas):
    if not isinstance(value, dict) or value.keys() != DEFAULTS.keys():
        raise ValueError("配置字段不完整，请重新载入页面。")
    settings = dict(value)
    if type(settings["natural_language_enabled"]) is not bool:
        raise ValueError("请选择是否启用自然语言查询。")
    provider = settings["natural_language_provider"]
    if not isinstance(provider, str) or (provider and provider not in providers
                                         and provider != current["natural_language_provider"]):
        raise ValueError("请从当前已加载的对话模型中选择。")
    timeout = settings["natural_language_timeout"]
    if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"AI 响应超时须为 1–{MAX_TIMEOUT_SECONDS} 秒的整数。")
    if type(settings["review_enabled"]) is not bool:
        raise ValueError("请选择是否启用 AI 锐评。")
    persona = settings["review_persona"]
    if not isinstance(persona, str) or (persona and persona not in personas and persona != current["review_persona"]):
        raise ValueError("请从 AstrBot 已保存的人格中选择。")
    if settings["review_enabled"] and not persona:
        raise ValueError("开启 AI 锐评前，请先选择人格。")
    if settings["review_enabled"] and not current["review_enabled"] and persona not in personas:
        raise ValueError("所选人格当前不可用，请重新选择。")

    for key, (label, reserved) in ALIAS_FIELDS.items():
        entries = settings[key]
        if not isinstance(entries, list):
            raise ValueError(f"{label}别名须为列表。")
        canonical = []
        for index, entry in enumerate(entries, 1):
            if not isinstance(entry, dict):
                raise ValueError(f"请检查{label}别名第 {index} 行。")
            target, aliases = entry.get("target"), entry.get("aliases")
            if not isinstance(target, str) or not isinstance(aliases, str):
                raise ValueError(f"请检查{label}别名第 {index} 行。")
            if not target.strip() and not aliases.strip():
                continue
            names = [alias for alias in ALIAS_SEPARATOR.split(aliases.strip()) if alias]
            if not target.strip() or not names:
                raise ValueError(f"{label}别名第 {index} 行需要填写正式名称和别名。")
            if any(alias_key(alias) in reserved for alias in names):
                raise ValueError(f"{label}别名第 {index} 行使用了查询保留词。")
            canonical.append({"__template_key": "rule", "target": target.strip(), "aliases": "\n".join(names)})
        settings[key] = canonical
    aliases = NameAliases.from_config(settings)
    for label, records in (("心法", aliases.hearts), ("服务器", aliases.servers)):
        for name, targets in records.items():
            if len(targets) > 1:
                raise ValueError(f"{label}别名「{name}」同时对应多个名称，请调整后再保存。")
    settings["role_nicknames"] = normalize_role_nicknames(settings["role_nicknames"], aliases)
    return settings, aliases


class SettingsPage:
    def __init__(self, plugin, config, web):
        self.plugin = plugin
        self.config = config
        self.web = web

    def providers(self):
        models = {}
        for provider in self.plugin.context.get_all_providers():
            metadata = provider.meta()
            if isinstance(metadata.id, str) and metadata.id:
                models[metadata.id] = str(getattr(metadata, "model", "") or "")
        return models

    async def personas(self):
        manager = getattr(self.plugin.context, "persona_manager", None)
        if manager is None:
            return []
        try:
            personas = await manager.get_all_personas()
        except Exception as exc:
            logger.warning(f"名剑管理页读取人格列表失败 类型={type(exc).__name__}")
            return []
        return sorted(persona.persona_id for persona in personas)

    def payload(self, settings, providers, personas):
        return {"settings": settings, "revision": revision(settings),
                "providers": [{"id": key, "model": model} for key, model in providers.items()],
                "personas": personas}

    async def read(self):
        personas = await self.personas()
        return self.web.json_response(self.payload(public_settings(self.config), self.providers(), personas))

    async def save(self):
        body = await self.web.request.json(default=None)
        if not isinstance(body, dict):
            return self.web.error_response("无法读取配置内容。", status_code=400)
        personas = await self.personas()
        current = public_settings(self.config)
        if body.get("revision") != revision(current):
            return self.web.error_response("配置已在别处更新，请重新载入后再保存。", status_code=409)
        providers = self.providers()
        try:
            settings, aliases = normalize_settings(body.get("settings"), current, providers, personas)
        except ValueError as exc:
            return self.web.error_response(str(exc), status_code=400)

        original = dict(self.config)
        try:
            disk = json.loads(Path(self.config.config_path).read_text(encoding="utf-8-sig"))
            if disk != original:
                return self.web.error_response("配置文件已变动，请重载插件后重新打开此页。", status_code=409)
            self.config.save_config(settings)
        except (OSError, ValueError) as exc:
            self.config.clear()
            self.config.update(original)
            logger.warning(f"名剑管理页保存失败 类型={type(exc).__name__}")
            return self.web.error_response("保存配置失败，请检查 AstrBot 的插件配置文件权限。", status_code=500)
        self.plugin.aliases = aliases
        self.plugin.role_nicknames = index_role_nicknames(settings["role_nicknames"])
        self.plugin.natural.configure(settings)
        return self.web.json_response(self.payload(public_settings(self.config), providers, personas))


def register_settings_page(plugin, config):
    if not callable(getattr(config, "save_config", None)):
        return
    register = getattr(plugin.context, "register_web_api", None)
    if register is None:
        return
    try:
        from astrbot.api import web
    except ImportError:
        return
    page = SettingsPage(plugin, config, web)
    register(f"/{PLUGIN}/settings", page.read, ["GET"], "读取名剑设置")
    register(f"/{PLUGIN}/settings", page.save, ["POST"], "保存名剑设置")
