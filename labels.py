from unicodedata import normalize
from urllib.parse import urljoin, urlsplit


SITE = "https://jx3.unua.top/"
FORCE_ICONS = "https://dl.pvp.xoyo.com/static/tuilan-app/images/jx3/forces/icons/"
OFFICIAL_HEART_ICONS = "https://dl.pvp.xoyo.com/static/tuilan-app/images/jx3/forces/kungfu/"
HEARTS = {
    "naihua": ("奶花", "离经易道", "lijing"),
    "naidu": ("奶毒", "补天诀"),
    "badao": ("霸刀", "北傲诀"),
    "naixiu": ("奶秀", "云裳心经"),
    "jianchun": ("剑纯", "太虚剑意"),
    "qichun": ("气纯", "紫霞功"),
    "cangyun": ("苍云", "分山劲"),
    "naige": ("奶歌", "相知"),
    "naiyao": ("奶药", "灵素"),
    "tiance": ("天策", "傲血战意", "aoxue"),
    "huajian": ("花间", "花间游"),
    "cangjian": ("藏剑", "问水诀", "山居剑意", "心法共用奇穴"),
    "lingxue": ("凌雪", "凌雪阁", "隐龙诀"),
    "penglai": ("蓬莱", "凌海诀"),
    "yantian": ("衍天", "衍天宗", "太玄经"),
    "gaibang": ("丐帮", "笑尘诀"),
    "duanshi": ("段氏", "周天功", "zhoutian"),
    "jingyu": ("惊羽", "惊羽诀", "鲸鱼"),
    "tianluo": ("田螺", "天罗诡道"),
    "wufang": ("无方",),
    "bingxin": ("冰心", "冰心诀"),
    "mingjiao": ("喵子", "焚影圣诀"),
    "mowen": ("莫问",),
    "dujing": ("毒经",),
    "heshang": ("和尚", "易筋经"),
    "wanling": ("万灵", "万灵山庄", "山海心诀"),
    "daozong": ("刀宗", "孤锋诀"),
    "wuxiang": ("无相", "无相楼", "幽罗引", "youluo"),
}
SCHOOL_IDS = {
    "少林": 1, "万花": 2, "纯阳": 4, "七秀": 5, "五毒": 6, "唐门": 7,
    "明教": 10, "长歌": 22, "药宗": 212, "北天药宗": 212, "大侠": 0,
    "无心法": 0,
}
HEART_NAMES = {alias: options[0] for options in HEARTS.values() for alias in options}
TANKS = {"铁骨衣", "明尊琉璃体", "洗髓经", "铁牢律"}
HEART_ICONS = {
    alias: f"{SITE}icons/force/{file}.png"
    for file, aliases in HEARTS.items()
    for alias in (file, *aliases)
}


def alias_key(value: str) -> str:
    return normalize("NFKC", value).strip().casefold()


for file, label in (
    ("xisui", "洗髓经"), ("tielao", "铁牢律"), ("tiegu", "铁骨衣"), ("mingzun", "明尊琉璃体"),
):
    HEART_ICONS[file] = HEART_ICONS[label] = f"{OFFICIAL_HEART_ICONS}{file}.png"


class IconLabel(str):
    def __new__(cls, parts: tuple[tuple[str, str | None], ...]):
        label = super().__new__(cls, "".join(text for text, _ in parts))
        label.parts = parts
        return label


def valid_icon(source: str) -> str:
    if not source:
        return ""
    try:
        url = urljoin(SITE, source)
        parsed = urlsplit(url)
        host = parsed.hostname or ""
    except ValueError:
        return ""
    if parsed.scheme == "https" and (host == "jx3.unua.top" or host.endswith(".xoyo.com")):
        return url
    return ""


def heart_icon(school: str) -> str:
    school = school.strip()
    if school in HEART_ICONS:
        return HEART_ICONS[school]
    if school in SCHOOL_IDS:
        return f"{FORCE_ICONS}{SCHOOL_IDS[school]}.png"
    return ""


def labeled(text: str, school: str = "", source: str = "") -> IconLabel:
    return IconLabel(((text, valid_icon(source) or heart_icon(school)),))


def heart_label(school: str, source: str = "") -> IconLabel:
    return labeled(school, school, source)


def label_parts(*pieces: str) -> IconLabel:
    return IconLabel(tuple(
        part for piece in pieces
        for part in (piece.parts if isinstance(piece, IconLabel) else ((piece, None),))
    ))


def join_labels(separator: str, labels) -> IconLabel:
    parts = []
    for index, label in enumerate(labels):
        if index:
            parts.append(separator)
        parts.append(label)
    return label_parts(*parts)


def lineup_label(lineup: str) -> IconLabel:
    return join_labels(" / ", (heart_label(member) for member in lineup.split()))
