import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import lru_cache, partial
from http.client import IncompleteRead
from io import BytesIO
from math import ceil, cos, floor, pi, sin
from pathlib import Path
from time import monotonic
from urllib.request import Request, urlopen

from astrbot.api import logger
from PIL import Image, ImageDraw, ImageFont

from .cards import RADAR_GRADES, Card, FlowSection, Sparkline, Table
from .data import USER_AGENT, error_kind
from .icon_cache import IconCache
from .labels import OFFICIAL_HEART_ICONS, SITE, IconLabel, valid_icon
from .query import QueryError


WIDTH = 880
MAX_REPLAY_HEIGHT = 60_000
LEFT = 30
RIGHT = WIDTH - LEFT
BG, PAPER, INK, MUTED = "#f8f1e4", "#fdf8ef", "#282c27", "#605749"
GREEN, ACCENT, LINE, FRAME = "#375b4b", "#913c33", "#dfd3bf", "#ad9472"
WASH, SHADE, JADE = "#efe5d4", "#f6eee1", "#e3e8dc"
COLORS = ("#387063", "#6552a4", "#a84336", "#296b8b", "#855719",
          "#9c4c70", "#51732d", "#544e89", "#a15c32", "#247a78")
ROOT = Path(__file__).parent
FONT = ROOT / "fonts" / "NotoSansSC.ttf"
DISPLAY_FONT = ROOT / "fonts" / "MingjianDisplay.ttf"
LANDSCAPE = ROOT / "assets" / "ink-landscape.png"
HEART_ASSET_ROOT = ROOT / "assets" / "heart-icons"
HEART_ASSET_SOURCES = {SITE + "icons/force/": "site", OFFICIAL_HEART_ICONS: "official"}
HEART_ICON_FILENAME = re.compile(r"[A-Za-z0-9_-]+\.png")
LEADERBOARD_COLUMNS = ["名次", "心法", "角色", "服务器", "积分", "胜率"]
COLUMN_WIDTH = {
    "序号": 0.83, "编号": 0.55, "名次": 0.65, "排名": 0.65, "心法": 1.3,
    "角色": 1.85, "选项": 9, "队伍成员": 5, "阵容": 5, "首位配队": 3.6,
    "时间": 1.2, "目标": 2, "技能": 2.1, "施法者": 2.05, "耗时": .85,
    "关键伤害／治疗": 1.65, "历周走势": 1.45, "说明": 3, "效果": 4,
    "指令或操作": 2.7,
    "指令示例": 4.45, "可改参数与默认值": 5.55,
}
FIELD_SPLIT = re.compile(r"[\x21-\x7e]+|\n|.", re.DOTALL)
COMMAND_SPLIT = re.compile(r"\S+|[^\S\n]+|\n")


@lru_cache(maxsize=24)
def font(size: int, bold: bool = False, display: bool = False):
    if display:
        return ImageFont.truetype(str(DISPLAY_FONT), size)
    face = ImageFont.truetype(str(FONT), size)
    face.set_variation_by_axes([650 if bold else 420])
    return face


@lru_cache(maxsize=1)
def display_glyphs():
    return frozenset(DISPLAY_FONT.with_suffix(".txt").read_text(encoding="utf-8"))


def label_font(text: str, size: int, bold: bool, display: bool):
    return font(size, bold, display and all(char in display_glyphs() for char in text))


@lru_cache(maxsize=1)
def landscape():
    with Image.open(LANDSCAPE) as image:
        return image.convert("RGB")


@lru_cache(maxsize=6000)
def width(text: str, size: int, bold: bool, display: bool = False) -> float:
    return label_font(text, size, bold, display).getlength(text)


@lru_cache(maxsize=4096)
def text_mask(text: str, size: int, bold: bool, anchor: str, x_fraction: float, y_fraction: float, display: bool):
    face = label_font(text, size, bold, display)
    left, top, right, bottom = face.getbbox(text, anchor=anchor)
    left, top = floor(left) - 2, floor(top) - 2
    mask = Image.new("L", (ceil(right) + 2 - left, ceil(bottom) + 2 - top))
    ImageDraw.Draw(mask).text((x_fraction - left, y_fraction - top), text, font=face, fill=255, anchor=anchor)
    return mask, left, top


def download_icon(url: str):
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*"})
    with urlopen(request, timeout=4) as response:
        if not valid_icon(response.geturl()):
            raise ValueError("unrecognized image source")
        raw = response.read(384 * 1024 + 1)
    if len(raw) > 384 * 1024:
        raise ValueError("icon is too large")
    with Image.open(BytesIO(raw)) as image:
        if image.width > 512 or image.height > 512:
            raise ValueError("icon dimensions are too large")
        result = image.convert("RGBA")
        result.thumbnail((64, 64), Image.Resampling.LANCZOS)
        return result


@lru_cache(maxsize=512)
def memory_icon(url: str):
    return download_icon(url)


@lru_cache(maxsize=128)
def bundled_heart_icon(url: str) -> Image.Image | None:
    prefix, _, filename = url.rpartition("/")
    folder = HEART_ASSET_SOURCES.get(prefix + "/")
    if folder and HEART_ICON_FILENAME.fullmatch(filename):
        try:
            with Image.open(HEART_ASSET_ROOT / folder / filename, formats=("PNG",)) as image:
                return image.convert("RGBA")
        except (OSError, ValueError, Image.DecompressionBombError):
            pass
    return None


@dataclass(frozen=True)
class IconFailure:
    kind: str


def icon_bitmap(url: str, cache: IconCache | None = None) -> Image.Image | IconFailure:
    bundled = bundled_heart_icon(url)
    if bundled is not None:
        return bundled
    try:
        return cache.load(url, download_icon) if cache is not None else memory_icon(url)
    except (OSError, ValueError, IncompleteRead, Image.DecompressionBombError) as exc:
        return IconFailure(error_kind(exc))


def label_parts(value):
    return value.parts if isinstance(value, IconLabel) else ((str(value), None),)


def sources(card: Card):
    radar_title = card.radar.title if card.radar else ""
    for label in (card.title, card.subtitle, card.note, radar_title, *(value for _, value in card.filters)):
        for _, url in label_parts(label):
            if asset := valid_icon(url or ""):
                yield asset
    for table in [Table("", card.columns, card.rows), *card.tables]:
        for heading in (table.title, table.caption):
            for _, url in label_parts(heading):
                if asset := valid_icon(url or ""):
                    yield asset
        for row in table.rows:
            for value in row:
                if isinstance(value, IconLabel):
                    for _, url in value.parts:
                        if asset := valid_icon(url or ""):
                            yield asset
    for title, items in card.groups:
        for _, url in label_parts(title):
            if asset := valid_icon(url or ""):
                yield asset
        for item in items:
            if asset := valid_icon(item.get("icon") or ""):
                yield asset
    if card.chart:
        for label, _ in card.chart.series:
            for _, url in label_parts(label):
                if asset := valid_icon(url or ""):
                    yield asset
    if card.bars:
        for label, _, _ in card.bars.rows:
            for _, url in label_parts(label):
                if asset := valid_icon(url or ""):
                    yield asset


def label_lines(value, limit: float, size: int, bold: bool = False, *, keep_words: bool = False, display: bool = False):
    lines = [[]]
    used = 0.0
    for text, icon in label_parts(value):
        pieces = (COMMAND_SPLIT if keep_words else FIELD_SPLIT).findall(text)
        tokens = [(piece, icon if i == 0 else None) for i, piece in enumerate(pieces)]
        for piece, asset in tokens:
            if piece == "\n":
                lines.append([])
                used = 0
                continue
            icon_width = size + 6 if asset is not None else 0
            words = list(piece) if width(piece, size, bold, display) + icon_width > limit else [piece]
            for i, word in enumerate(words):
                mark = asset if i == 0 else None
                span = width(word, size, bold, display) + (icon_width if i == 0 else 0)
                if used + span > limit and lines[-1]:
                    lines.append([])
                    used = 0
                if word.isspace() and not lines[-1] and mark is None:
                    continue
                lines[-1].append((word, mark, span))
                used += span
    return lines


class Layout:
    def __init__(self, image: Image.Image, icons: dict, height_limit: int | None = None):
        self.image = image
        self.draw = ImageDraw.Draw(image)
        self.icons = icons
        self.scaled_icons = {}
        self.y = 0
        self.measure = image.height == 1
        self.height_limit = height_limit

    def rect(self, coords, fill, radius=0, outline=None):
        if self.measure:
            return
        if radius:
            self.draw.rounded_rectangle(coords, radius, fill=fill, outline=outline)
        else:
            self.draw.rectangle(coords, fill=fill, outline=outline)

    def text(self, x, y, message, size=20, color=INK, bold=False, anchor="lt", *, display=False):
        if self.measure:
            return
        message = str(message)
        if not message.strip():
            return
        if len(message) > 24 or "\n" in message or x < 0 or y < 0:
            self.draw.text((x, y), message, font=label_font(message, size, bold, display), fill=color, anchor=anchor)
            return
        left, top = int(x), int(y)
        mask, dx, dy = text_mask(message, size, bold, anchor, x - left, y - top, display)
        self.image.paste(color, (left + dx, top + dy), mask)

    def asset(self, x, y, size, url):
        if self.measure:
            return
        key = (url, size)
        if key not in self.scaled_icons:
            src = self.icons.get(valid_icon(url or ""))
            self.scaled_icons[key] = src.resize((size, size), Image.Resampling.LANCZOS) if src is not None else None
        icon = self.scaled_icons[key]
        if icon is None:
            self.rect((x, y, x+size-1, y+size-1), JADE, 4)
            self.text(x+size/2, y+size/2, "?", max(12, round(size*.70)), GREEN, True, "mm")
            return
        self.image.paste(icon, (round(x), round(y)), icon)

    def lines(self, lines, x, y, size, color=INK, bold=False, *, display=False):
        height = size + 9
        for row in lines:
            if not self.measure:
                cx = x
                for text, icon, span in row:
                    if icon is not None:
                        self.asset(cx, y, size, icon)
                    self.text(cx + (size + 6 if icon is not None else 0), y + 1, text, size, color, bold, display=display)
                    cx += span
            y += height
        return y

    def block(self, label, x, y, space, size=20, color=INK, bold=False, *, display=False):
        lines = label_lines(label, space, size, bold, display=display)
        return self.lines(lines, x, y, size, color, bold, display=display)

    def heading(self, title):
        self.rect((LEFT + 2, self.y + 4, LEFT + 5, self.y + 23), ACCENT)
        self.y = self.block(title, LEFT + 14, self.y, RIGHT - LEFT - 24, 25, INK, True, display=True) + 8

    def header(self, card: Card, leaderboard: bool):
        if not self.measure:
            self.image.paste(landscape(), (26, 22))
        self.rect((LEFT + 21, 43, LEFT + 61, 83), ACCENT, 3)
        self.rect((LEFT + 24, 46, LEFT + 58, 80), ACCENT, 2, "#efcfaf")
        for x, y, char in ((LEFT + 49, 54, "名"), (LEFT + 49, 71, "剑"),
                           (LEFT + 33, 54, "大"), (LEFT + 33, 71, "会")):
            self.text(x, y, char, 15, PAPER, anchor="mm", display=True)

        subtitle = card.subtitle
        fields = card.filters
        if leaderboard:
            subtitle = next((value for key, value in fields if key == "周期"), subtitle)
            fields = [(key, value) for key, value in fields if key != "周期"]
        size = 56 if leaderboard else 46
        bottom = self.block(card.title, LEFT + 19, 103, 630, size, INK, True, display=True)
        self.rect((LEFT + 20, bottom + 4, LEFT + 221, bottom + 5), FRAME)
        if not self.measure:
            x, y = LEFT + 231, bottom + 5
            self.draw.polygon(((x-4, y), (x, y-4), (x+4, y), (x, y+4)), fill=ACCENT)
        if subtitle:
            bottom = self.block(subtitle, LEFT + 20, bottom + 19, 540, 23, MUTED, True)
        self.y = max(239, bottom + 12)
        if leaderboard and fields:
            self.filter_strip(fields)
        else:
            self.filters(fields)

    def filter_strip(self, fields):
        column = (RIGHT - LEFT) / len(fields)
        height = 0
        for index, (key, value) in enumerate(fields):
            x = LEFT + index * column
            self.text(x + column / 2, self.y + 17, key, 19, MUTED, anchor="mm", display=True)
            lines = label_lines(value, column - 18, 21, True)
            for number, line in enumerate(lines):
                left = x + (column - sum(span for _, _, span in line)) / 2
                self.lines([line], left, self.y + 34 + number * 30, 21, ACCENT if key == "模式" else INK, True)
            height = max(height, 43 + 30 * len(lines))
        for index in range(1, len(fields)):
            x = LEFT + index * column
            self.rect((x, self.y + 12, x, self.y + height - 10), LINE)
        self.rect((LEFT, self.y, RIGHT, self.y), FRAME)
        self.rect((LEFT, self.y + height, RIGHT, self.y + height), LINE)
        self.y += height + 22

    def filters(self, fields):
        left = LEFT
        for key, value in fields:
            if isinstance(value, IconLabel):
                label = IconLabel(((key + "  ", None), *value.parts))
            else:
                label = f"{key}  {value}"
            lines = label_lines(label, RIGHT-LEFT-22, 17)
            length = max(sum(token[2] for token in line) for line in lines) + 22
            if left + length > RIGHT and left > LEFT:
                self.y += 42
                left = LEFT
            if len(lines) > 1 and left != LEFT:
                self.y += 42
                left = LEFT
            height = len(lines)*26 + 9
            self.rect((left, self.y, left+length, self.y+height), SHADE, 4, LINE)
            self.lines(lines, left+11, self.y+8, 17)
            if len(lines) > 1:
                self.y += height + 7
                left = LEFT
            else:
                left += length + 8
        if left != LEFT:
            self.y += 44
        if fields:
            self.y += 9

    def table(self, table: Table):
        if table.title:
            self.heading(table.title)
        if table.caption:
            self.y = self.block(table.caption, LEFT+10, self.y, RIGHT-LEFT-20, 17, MUTED) + 8
        if not table.rows:
            self.y = self.block("暂无数据", LEFT+12, self.y+5, RIGHT-LEFT-24, 19, MUTED) + 16
            return
        weights = [COLUMN_WIDTH.get(name, 1.0) for name in table.columns]
        if not weights:
            return
        if table.compact and table.columns[0] == "时间":
            weights[0] = .84
        total = sum(weights)
        col_widths = [(RIGHT - LEFT) * value / total for value in weights]
        size = 17 if table.compact else 20
        padding = 8 if table.compact else 11
        for number, row in enumerate([table.columns, *table.rows]):
            bold = number == 0
            boxes = [label_lines(value, length-2*padding, size, bold, keep_words=column == "指令示例", display=bold)
                     if not isinstance(value, Sparkline) else [[]]
                     for value, length, column in zip(row, col_widths, table.columns)]
            height = max(map(len, boxes), default=1) * (size+9) + 2 * padding - 2
            fill = WASH if bold else (PAPER if number % 2 else SHADE)
            self.rect((LEFT, self.y, RIGHT, self.y + height), fill, outline=LINE if bold else None)
            x = LEFT
            for value, lines, length in zip(row, boxes, col_widths):
                if isinstance(value, Sparkline):
                    self.sparkline(value, x+padding, self.y+padding, length-2*padding, height-2*padding)
                else:
                    self.lines(lines, x+padding, self.y+padding, size, INK, bold, display=bold)
                x += length
            self.y += height
            if self.height_limit is not None and self.y > self.height_limit:
                return
        self.y += 22

    def leaderboard(self, card: Card):
        number_x, rank_x, heart_x, role_x, server_x, score_x, rate_x = 62, 122, 164, 215, 454, 660, 825
        numbers = card.leaderboard_numbers
        if not numbers:
            rank_x, heart_x, role_x, server_x = 77, 127, 179, 441
        self.rect((LEFT, self.y, RIGHT, self.y + 48), WASH, 4, LINE)
        if numbers:
            self.text(number_x, self.y + 24, "序号", 20, ACCENT, anchor="mm", display=True)
        for x, label, anchor in ((rank_x, "名次", "mm"), (heart_x, "角色 / 心法", "lm"), (server_x, "服务器", "lm"),
                                 (score_x, "积分", "rm"), (rate_x, "胜率", "rm")):
            self.text(x, self.y + 24, label, 21, MUTED, anchor=anchor, display=True)
        self.y += 48
        medals = {"1": ("#f7ede0", ACCENT), "2": ("#f0f1e5", "#597166"), "3": ("#f7f0e5", "#886342")}
        for index, (rank, heart, role, server, score, rate) in enumerate(card.rows):
            role_lines = label_lines(str(role), server_x - role_x - 14, 25, True)
            heart_lines = label_lines(str(heart), server_x - role_x - 14, 19)
            server_lines = label_lines(server, 129, 21)
            height = max(73, 14 + 34 * len(role_lines) + 28 * len(heart_lines), 14 + 30 * len(server_lines))
            mid = self.y + height / 2
            fill, medal = medals.get(str(rank), (PAPER, None))
            self.rect((LEFT, self.y, RIGHT, self.y + height), fill, outline=LINE)
            if numbers:
                number = numbers[index]
                self.text(number_x, mid, number if number is not None else "—", 19, ACCENT, True, "mm")
            if medal:
                self.rect((rank_x-18, mid-18, rank_x+18, mid+18), medal, 3)
            self.text(rank_x, mid, rank, 23, PAPER if medal else MUTED, True, "mm")
            icon = label_parts(role)[0][1] or label_parts(heart)[0][1]
            self.rect((heart_x-2, mid-21, heart_x+40, mid+21), JADE, 6)
            self.asset(heart_x, mid-19, 38, icon)
            role_top = mid - (34 * len(role_lines) + 28 * len(heart_lines)) / 2 + 3
            label_bottom = self.lines(role_lines, role_x, role_top, 25, INK, True)
            self.lines(heart_lines, role_x, label_bottom - 2, 19, MUTED)
            self.lines(server_lines, server_x, mid - 15 * len(server_lines) + 3, 21, MUTED)
            self.text(score_x, mid, score, 30, INK, True, "rm")
            percent = re.fullmatch(r"(\d{1,3}(?:\.\d+)?)\s*%", str(rate).strip())
            self.text(rate_x, mid - (6 if percent else 0), rate, 23, GREEN, True, "rm")
            if percent:
                amount = min(100.0, float(percent[1])) / 100
                self.rect((rate_x-64, mid+14, rate_x, mid+17), JADE, 1)
                if amount:
                    self.rect((rate_x-64, mid+14, rate_x-64+64*amount, mid+17), GREEN, 1)
            self.y += height
        if card.subtitle:
            self.rect((LEFT, self.y, RIGHT, self.y + 41), SHADE, outline=LINE)
            self.text(LEFT+13, self.y+20, card.subtitle, 18, MUTED, anchor="lm")
            self.y += 41
        self.y += 22

    def sparkline(self, line: Sparkline, x, y, w, h):
        data = line.counts
        if len(data) < 2 or any(not isinstance(value, (int, float)) for value in data):
            self.text(x, y, "—", 17, MUTED)
            return
        low, high = min(data), max(data)
        coords = [(x+w*i/(len(data)-1), y+h-(value-low)*h/max(1, high-low)-2)
                  for i, value in enumerate(data)]
        if not self.measure:
            self.draw.line(coords, fill=GREEN, width=3, joint="curve")

    def group(self, title, items):
        self.heading(title)
        x = LEFT
        space = RIGHT-LEFT
        row_height = 0
        for item in items:
            detail = item.get("detail")
            selected = item.get("selected")
            name = str(item["name"])
            w = space if detail else min(space, 60 + width(name, 19, bool(selected)))
            if x + w > RIGHT:
                self.y += row_height + 8
                x, row_height = LEFT, 0
            name_lines = label_lines(name, w-55, 19, bool(selected))
            detail_lines = label_lines(detail, w-55, 17) if detail else []
            height = max(43, len(name_lines) * 28 + len(detail_lines) * 26 + 18)
            self.rect((x, self.y, x+w, self.y+height), JADE if selected else PAPER, 5, GREEN if selected else LINE)
            self.asset(x+9, self.y+10, 30, item.get("icon") or "")
            offset = self.lines(name_lines, x+47, self.y+11, 19, GREEN if selected else INK, bool(selected))
            self.lines(detail_lines, x+47, offset + 2, 17, MUTED)
            x += w + 8
            row_height = max(row_height, height)
        self.y += row_height+24

    def chart(self, chart):
        points = len(chart.dates)
        values = [value for _, data in chart.series for value in data if isinstance(value, (int, float))]
        if not points or not values:
            return
        low = min(0, min(values)) if chart.include_zero else min(values) - 2
        high = max(0, max(values)) if chart.include_zero else max(values) + 2
        span = max(4, high-low)
        marks = sorted({round(i*(points-1)/5) for i in range(6)})
        labels = [(i, re.sub(r"^\d{4}-(\d{2}-\d{2})$", r"\1", chart.dates[i])) for i in marks]
        padding = max(32, 14 + max(width(label, 15, False) for _, label in labels)/2)
        x, y, h = LEFT+88, self.y+59, 240
        w = RIGHT - x - padding
        top = self.y
        self.rect((LEFT, top, RIGHT, y+h+50), PAPER, 5, LINE)
        self.text(LEFT+18, top+16, chart.axis_label, 20, INK, True)
        for step in range(5):
            pos = y + step*h/4
            if not self.measure:
                self.draw.line((x, pos, x+w, pos), fill=LINE, width=1)
            self.text(x-12, pos, f"{round(low+span*(4-step)/4):,}", 15, MUTED, False, "rm")
        if low < 0 and high > 0 and not self.measure:
            zero = y+h-h*(0-low)/span
            self.draw.line((x, zero, x+w, zero), fill=LINE, width=2)
        for i, label in labels:
            xpos = x+i*w/max(1, points-1)
            self.text(xpos, y+h+16, label, 15, MUTED, False, "mt")
        self.y = y+h+62
        legend_height = 0
        for index, (label, data) in enumerate(chart.series):
            color = COLORS[index % len(COLORS)]
            line = [(x+i*w/max(1, points-1), y+h-int(h*(value-low)/span)) if isinstance(value, (int, float)) else None
                    for i, value in enumerate(data[:points])]
            if not self.measure:
                previous = None
                for pt in line:
                    if pt is not None:
                        if previous is not None:
                            self.draw.line((previous, pt), fill=color, width=3)
                        if points <= 12:
                            self.draw.ellipse((pt[0]-3, pt[1]-3, pt[0]+3, pt[1]+3), fill=color)
                    previous = pt
            col = index % 4
            cx = LEFT + 14 + col * 204
            self.rect((cx, self.y+8, cx+13, self.y+11), color)
            end = self.block(label, cx+20, self.y, 178, 16, INK)
            legend_height = max(legend_height, end - self.y)
            if col == 3 or index == len(chart.series)-1:
                self.y += legend_height + 11
                legend_height = 0
        self.y += 10

    def radar(self, radar):
        self.heading(radar.title)
        top = self.y
        center_x, center_y, radius = WIDTH / 2, top + 186, 116
        self.rect((LEFT, top, RIGHT, top + 398), PAPER, 5, LINE)
        angles = [2*pi*i/len(radar.axes) - pi/2 for i in range(len(radar.axes))]

        def point(angle, distance):
            return center_x + cos(angle)*distance, center_y + sin(angle)*distance

        levels = len(RADAR_GRADES)
        if not self.measure:
            for level in range(1, levels+1):
                self.draw.polygon([point(a, radius*level/levels) for a in angles], outline=LINE)
            for angle in angles:
                self.draw.line(((center_x, center_y), point(angle, radius)), fill=LINE)
            values = [point(a, radius*(RADAR_GRADES.index(grade)+1)/levels)
                      for a, (_, grade) in zip(angles, radar.axes)]
            self.draw.polygon(values, fill=JADE, outline=GREEN, width=2)
            for x, y in values:
                self.draw.ellipse((x-3, y-3, x+3, y+3), fill=GREEN)
        for angle, (name, grade) in zip(angles, radar.axes):
            x, y = point(angle, radius+32)
            anchor = "lm" if cos(angle) > .2 else "rm" if cos(angle) < -.2 else "mm"
            self.text(x, y-5, name, 17, INK, False, anchor)
            self.text(x, y+17, grade, 15, GREEN, True, anchor)
        self.text(WIDTH/2, top+376, "由内向外为 D、C、B、A、S", 16, MUTED, False, "mm")
        self.y = top + 419

    def bars(self, chart):
        peak = chart.maximum or max((value for _, value, _ in chart.rows), default=1) or 1
        width = RIGHT-LEFT
        start = self.y
        self.rect((LEFT, start, RIGHT, start + 24 + 36*len(chart.rows)), PAPER, 5, LINE)
        line_x, line_w = LEFT+162, width-326
        for index, (label, value, caption) in enumerate(chart.rows):
            y = start+14+index*36
            self.block(label, LEFT+14, y+1, 134, 17)
            self.rect((line_x, y+5, line_x+line_w, y+20), JADE, 3)
            if value > 0:
                end = line_x+line_w*min(1, value/peak)
                self.rect((line_x, y+5, end, y+20), GREEN, 5)
            self.text(RIGHT-18, y+12, caption, 17, INK, True, "rm")
        self.y = start+42+36*len(chart.rows)

    def flow_section(self, section: FlowSection):
        self.heading(section.title)
        entry_x = LEFT + 12
        entry_width = 285
        result_x = entry_x + entry_width + 47
        result_width = RIGHT - result_x - 15
        for flow in section.flows:
            entry = label_lines(flow.entry, entry_width - 24, 19, True, keep_words=True)
            result = label_lines(flow.result, result_width, 20, True)
            branches = [label_lines(branch, result_width - 21, 18) for branch in flow.branches]
            entry_height = len(entry) * 28 + 20
            result_height = len(result) * 29 + sum(len(branch) * 27 + 4 for branch in branches)
            height = max(entry_height + 20, result_height + 24)
            top = self.y
            self.rect((LEFT, top, RIGHT, top + height), PAPER, 5, LINE)
            self.rect((entry_x, top + 10, entry_x + entry_width, top + 10 + entry_height), WASH, 4)
            self.lines(entry, entry_x + 12, top + 20, 19, INK, True)
            self.text(entry_x + entry_width + 12, top + 17, "→", 22, ACCENT, True)
            line_y = self.lines(result, result_x, top + 12, 20, INK, True)
            for branch in branches:
                self.rect((result_x + 2, line_y + 3, result_x + 3, line_y + 13), GREEN)
                self.rect((result_x + 2, line_y + 12, result_x + 13, line_y + 13), GREEN)
                line_y = self.lines(branch, result_x + 21, line_y, 18, MUTED) + 4
            self.y += height + 9
        self.y += 12

    def frame(self):
        if self.measure:
            return
        bottom = self.image.height - 16
        self.draw.rectangle((13, 13, WIDTH - 14, bottom + 3), outline=LINE)
        self.draw.rectangle((19, 19, WIDTH - 20, bottom - 3), outline=FRAME)
        corner = ((0, 14), (0, 0), (14, 0), (14, 5), (5, 5), (5, 12))
        for x, y, dx, dy in ((23, 23, 1, 1), (WIDTH-24, 23, -1, 1),
                             (23, bottom-7, 1, -1), (WIDTH-24, bottom-7, -1, -1)):
            self.draw.line([(x+dx*a, y+dy*b) for a, b in corner], fill=FRAME)

    def paint(self, card: Card) -> int:
        ranking = card.title == "竞技场排行榜" and card.columns == LEADERBOARD_COLUMNS
        self.header(card, ranking)
        if card.chart:
            self.chart(card.chart)
        if card.bars:
            self.bars(card.bars)
        if ranking and card.rows:
            self.leaderboard(card)
        elif card.columns:
            self.table(Table("", card.columns, card.rows))
        if card.radar:
            self.radar(card.radar)
        for table in card.tables:
            self.table(table)
        for title, items in card.groups:
            self.group(title, items)
        for section in card.flow_sections:
            self.flow_section(section)
        if card.note:
            self.y = self.block(card.note, LEFT+9, self.y, RIGHT-LEFT-18, 17, MUTED)+10
        if card.flow_sections:
            self.rect((LEFT, self.y+4, RIGHT, self.y+5), LINE)
            self.text(LEFT+9, self.y+20, "名剑查询 · /名剑帮助", 16, MUTED)
        self.frame()
        return self.y + (64 if card.flow_sections else 36)


@dataclass(frozen=True)
class RenderedImage:
    png: bytes
    has_icon_failures: bool


def render_image(card: Card, icon_cache: IconCache | None = None) -> RenderedImage:
    height_limit = MAX_REPLAY_HEIGHT if card.menu and card.menu.action == "回放流水" else None
    with Image.new("RGB", (1, 1)) as probe:
        height = Layout(probe, {}, height_limit).paint(card)
        if height_limit is not None and height > height_limit:
            card = replace(
                card, tables=[table for table in card.tables if table.title == "编号选择"],
                note=card.note + "本次流水过长，请按玩家筛选或查看击杀点，再生成长图。",
            )
            height = Layout(probe, {}, height_limit).paint(card)
            if height > height_limit:
                raise QueryError("回放图尺寸过大，暂时无法生成图片。")
    urls = list(dict.fromkeys(sources(card)))
    started = monotonic()
    load_icon = partial(icon_bitmap, cache=icon_cache) if icon_cache is not None else icon_bitmap
    with ThreadPoolExecutor(max_workers=2) as executor:
        icons = dict(zip(urls, executor.map(load_icon, urls)))
    failures = Counter(icon.kind for icon in icons.values() if isinstance(icon, IconFailure))
    if failures:
        kinds = ", ".join(f"{kind}:{count}" for kind, count in sorted(failures.items()))
        logger.warning(f"名剑图标加载失败 数量={sum(failures.values())}/{len(urls)} "
                       f"耗时={monotonic() - started:.2f}s 类型={kinds}")
        icons = {url: icon for url, icon in icons.items() if not isinstance(icon, IconFailure)}
    with Image.new("RGB", (WIDTH, height), BG) as image:
        Layout(image, icons).paint(card)
        buffer = BytesIO()
        image.save(buffer, format="PNG", compress_level=2)
        return RenderedImage(buffer.getvalue(), bool(failures))


def render(card: Card) -> bytes:
    return render_image(card).png
