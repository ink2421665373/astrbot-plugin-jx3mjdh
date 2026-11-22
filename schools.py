import json
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from .cards import Card, Table
from .data import DataClient, field
from .labels import HEART_NAMES, HEARTS, alias_key, heart_label, join_labels, label_parts
from .query import ChoiceMenu, QueryError
from .rankings import update_time


@lru_cache(maxsize=1)
def presets() -> dict:
    return json.loads(Path(__file__).with_name("talent_presets.json").read_text(encoding="utf8"))


def icon(item: dict) -> str:
    value = item.get("icon")
    return value.get("FileName", "") if isinstance(value, dict) else value or ""


def item_label(item: dict, detail: bool = False) -> dict:
    return {
        "name": item.get("name") or item.get("skillName") or "未命名",
        "icon": icon(item),
        "detail": item.get("desc") or "" if detail else "",
    }


def matching_specs(forces: list[dict], key: str) -> list[tuple[dict, dict]]:
    specs = [(force, spec) for force in forces for spec in force.get("kungfuList") or []]
    for fields in (("name",), ("short", "iconName")):
        matches = [
            (force, spec) for force, spec in specs
            if any(isinstance(value, str) and alias_key(value) == key
                   for value in (spec.get(field) for field in fields))
        ]
        if matches:
            return matches
    heart = HEART_NAMES.get(key)
    if not heart and key in HEARTS:
        heart = HEARTS[key][0]
    if not heart:
        return []
    return [
        (force, spec) for force, spec in specs
        if HEART_NAMES.get(spec.get("name")) == heart or heart in HEARTS.get(spec.get("iconName"), ())
    ]


def choose_specs(prompt: str, action: str, specs: list[dict], detail, tactic: str) -> ChoiceMenu:
    return ChoiceMenu(prompt, action, tuple(
        (heart_label(row["name"], icon(row)), (row["name"], detail, tactic)) for row in specs
    ))


@dataclass(frozen=True)
class CachedSchoolResult:
    card: Card | ChoiceMenu


async def school(client: DataClient, action: str, args: tuple) -> Card | ChoiceMenu | CachedSchoolResult:
    data, saved_at = await client.get_school_data()
    card = school_card(data, action, args)
    if saved_at is None:
        return card
    stamp = f"{update_time(saved_at.isoformat())}（北京时间）"
    if isinstance(card, ChoiceMenu):
        card = replace(card, prompt=label_parts(card.prompt, f"\n使用门派缓存 · 保存于 {stamp}"))
    else:
        card = replace(card, filters=[("门派缓存", f"保存于 {stamp}"), *card.filters])
    return CachedSchoolResult(card)


def school_card(data: dict, action: str, args: tuple) -> Card | ChoiceMenu:
    name, detail, tactic = args
    forces = data.get("forces")
    if not isinstance(forces, list):
        raise QueryError("原站暂未返回门派资料。")
    name = {"药宗": "北天药宗", "万灵": "万灵山庄", "无相": "无相楼"}.get(name, name)
    key = alias_key(name)
    force = next((entry for entry in forces if alias_key(entry["name"]) == key), None)
    spec = None
    if force is None:
        matches = matching_specs(forces, key)
        if len(matches) > 1:
            prompt = label_parts(heart_label(name), f"对应 {len(matches)} 个心法。请选择心法。")
            return choose_specs(prompt, action, [row for _, row in matches], detail, tactic)
        if matches:
            force, spec = matches[0]
    if force is None:
        raise QueryError(f"门派资料中没有找到「{name}」。可填写门派名、心法全名或常用简称。")
    specs = force.get("kungfuList") or []
    label = heart_label(force["name"], icon(force))
    if spec is None and len(specs) > 1:
        return choose_specs(label_parts(label, f"有 {len(specs)} 个心法。请选择心法。"), action, specs, detail, tactic)
    spec = spec or (specs[0] if specs else {})
    index = str(spec.get("index", ""))
    spec_label = heart_label(spec["name"], icon(spec)) if spec else label
    subtitle = label_parts(label, " · ", spec_label) if spec else label
    metadata = [("资料更新", update_time(data["generatedAt"]))] if data.get("generatedAt") else []
    skills = [row for row in force.get("skillData") or [] if not index or str(row.get("kungfuId")) == index]
    if force.get("forceId") == "8" and action != "奇穴":
        skills = force.get("skillData") or []
    routines = [(group.get("remark") or "技能套路", group.get("forceSkills") or [])
                for entry in skills for group in entry.get("remarks") or []]
    if action == "门派":
        details = [("心法", join_labels(" / ", (heart_label(row["name"], icon(row)) for row in specs)))] if specs else []
        options = (("查看奇穴图谱", (spec.get("name") or name, "", "")),) if force.get("qixueData") else ()
        return Card(label_parts(label, " · 门派资料"), label_parts("技能套路 · ", spec_label), [*details, *metadata],
                    groups=[(title, [item_label(item) for item in group]) for title, group in routines if group],
                    menu=ChoiceMenu("可以继续查看该心法的图谱。", "奇穴", options) if options else None,
                    note="" if routines else "暂无技能套路。")
    if action == "技能":
        matching = [(title, item) for title, group in routines for item in group
                    if detail in (item.get("name"), item.get("skillName"))]
        if not matching:
            raise QueryError(f"原站资料中没有「{detail}」。")
        title, skill = matching[0]
        fields = [("技能套路", title)]
        for key, caption in (("releaseType", "释放"), ("cd", "调息"), ("distance", "距离"),
                             ("weapon", "武器"), ("consumption", "消耗")):
            if skill.get(key):
                fields.append((caption, field(skill, key)))
        cheasts = skill.get("cheasts") or []
        tables = [Table("秘籍", ["名称", "说明"], [[field(row, "name"), field(row, "desc")] for row in cheasts])] if cheasts else []
        return Card(label_parts("技能 · ", heart_label(detail, icon(skill))), subtitle, [*metadata, *fields],
                    tables=tables, note="\n".join(dict.fromkeys(
                        field(skill, key, "") for key in ("simpleDesc", "desc", "specialDesc") if skill.get(key)
                    )))
    if action == "阵法":
        matrix = next((row["zhenFa"] for row in skills if row.get("zhenFa")), {})
        if not matrix.get("skillName"):
            raise QueryError("原站没有展示该心法的阵法。")
        rows = [[field(row, "name"), field(row, "desc")] for row in matrix.get("descs") or []]
        return Card(label_parts("阵法 · ", heart_label(matrix["skillName"], icon(matrix))), subtitle,
                    metadata, tables=[Table("阵法效果", ["层数", "效果"], rows)])
    return qixue(force, spec, spec_label, subtitle, metadata, detail, tactic)


def qixue(force: dict, spec: dict, label, subtitle, metadata, tier, tactic) -> Card | ChoiceMenu:
    index = str(spec.get("index", ""))
    rows = [row for row in force.get("qixueData") or [] if not index or str(row.get("kungfuId")) == index]
    levels = sorted((level for row in rows for level in row.get("kungfuLevel") or []), key=lambda item: item["level"])
    if not levels:
        raise QueryError("原站暂未展示该心法的奇穴图谱。")
    defaults = presets()
    choices = defaults["shared"] if force.get("forceId") == "8" else defaults.get(index, defaults["default"])
    names = [item["name"] for item in choices]
    if not tactic and len(choices) > 1:
        return ChoiceMenu(label_parts(label, f"有 {len(choices)} 套奇穴。请选择套路。"), "奇穴",
                          tuple((name, (spec.get("name") or force["name"], tier, name)) for name in names))
    if tactic and tactic.isdecimal() and 1 <= int(tactic) <= len(names):
        tactic = names[int(tactic) - 1]
    if tactic and tactic not in names:
        raise QueryError(f"没有这个奇穴套路。可选 {'、'.join(names)}。")
    choice = choices[names.index(tactic)] if tactic else choices[0]
    selections = choice["selections"]
    groups = []
    for offset, row in enumerate(levels):
        level = row["level"]
        if tier and tier != level:
            continue
        items = (row.get("kungfuSkills") or []) + (row.get("forceSkills") or [])
        labels = [item_label(item, bool(tier)) for item in items]
        last = offset == len(levels) - 1
        entries = selections[offset:] if last else selections[offset:offset + 1]
        indexes = []
        for value in entries:
            found = value - 1 if isinstance(value, int) else next(
                (i for i, item in enumerate(labels) if item["name"] == value), -1)
            if 0 <= found < len(labels) and found not in indexes:
                indexes.append(found)
        if not selections or not indexes:
            indexes = list(range(min(len(labels), 3 if last and len(levels) == 7 else 1)))
        for i in indexes:
            labels[i]["selected"] = True
        groups.append((f"第{level}重", labels))
    if not groups:
        raise QueryError(f"原站没有第 {tier} 重奇穴。")
    return Card(label_parts(label, " · 奇穴图谱"), subtitle, [*metadata, ("套路", choice["name"])],
                groups=groups, note="高亮显示所选套路，其他候选一并列出。")
