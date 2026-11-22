import re
from collections.abc import Mapping
from dataclasses import dataclass
from .labels import alias_key
from .query import QueryError


ALIAS_SEPARATOR = re.compile(r"[\s,，、;；]+")
LINEUP_SEPARATOR = re.compile(r"([,，、/|+\-]+)")
ROLE_NICKNAME_FIELDS = ("nickname", "server", "name")
MAX_ROLES_PER_NICKNAME = 998  # The shared numbered menu also needs one original-name option.


def role_nickname_rows(rows: object) -> list[dict[str, str]]:
    if not isinstance(rows, list):
        return []
    return [{field: row[field] if isinstance(row.get(field), str) else "" for field in ROLE_NICKNAME_FIELDS}
            for row in rows if isinstance(row, dict)]


def index_role_nicknames(rows: object) -> dict[str, tuple[tuple[str, str], ...]]:
    index: dict[str, dict[tuple[str, str], tuple[str, str]]] = {}
    for row in role_nickname_rows(rows):
        nickname, server, name = (row[field].strip() for field in ROLE_NICKNAME_FIELDS)
        if all(value and value.isprintable() for value in (nickname, server, name)):
            index.setdefault(alias_key(nickname), {}).setdefault((alias_key(server), alias_key(name)), (server, name))
    return {key: tuple(roles.values()) for key, roles in index.items()}


def index_rules(rows: object, reserved: set[str]) -> dict[str, tuple[str, ...]]:
    targets: dict[str, dict[str, str]] = {}
    if not isinstance(rows, list):
        return {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        target, aliases = row.get("target"), row.get("aliases")
        if not isinstance(target, str) or not target.strip() or not isinstance(aliases, str):
            continue
        target = target.strip()
        for alias in ALIAS_SEPARATOR.split(aliases):
            key = alias_key(alias)
            if key and key not in reserved:
                targets.setdefault(key, {}).setdefault(alias_key(target), target)
    return {key: tuple(values.values()) for key, values in targets.items()}


def resolve(index: dict[str, tuple[str, ...]], value: str, label: str) -> str:
    candidates = index.get(alias_key(value), ())
    if len(candidates) > 1:
        names = "、".join(candidates)
        raise QueryError(f"{label}别名「{value}」对应多个目标（{names}）。请在插件配置中只保留一个目标。")
    return candidates[0] if candidates else value


@dataclass(frozen=True)
class NameAliases:
    hearts: dict[str, tuple[str, ...]]
    servers: dict[str, tuple[str, ...]]

    @classmethod
    def from_config(cls, config: Mapping | None) -> "NameAliases":
        settings = config or {}
        return cls(
            index_rules(settings.get("heart_aliases"), {"全部心法"}),
            index_rules(settings.get("server_aliases"), {"全部服务器", "全服搜索"}),
        )

    def heart(self, value: str) -> str:
        return resolve(self.hearts, value, "心法")

    def server(self, value: str) -> str:
        return resolve(self.servers, value, "服务器")

    def lineup(self, value: str) -> str:
        if not self.hearts:
            return value
        parts = []
        for token in value.split():
            if alias_key(token) in self.hearts:
                parts.append(self.heart(token))
            else:
                pieces = LINEUP_SEPARATOR.split(token)
                parts.append("".join(self.heart(piece) if i % 2 == 0 else piece
                                     for i, piece in enumerate(pieces)))
        return " ".join(parts)
