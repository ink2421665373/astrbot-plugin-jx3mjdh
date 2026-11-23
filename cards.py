from dataclasses import dataclass, field

from .query import ChoiceMenu


@dataclass(frozen=True)
class Trend:
    dates: tuple[str, ...]
    series: tuple[tuple[str, tuple[int | None, ...]], ...]
    axis_label: str = "榜单人数"
    suffix: str = " 人"
    include_zero: bool = True


RADAR_GRADES = ("D", "C", "B", "A", "S")


@dataclass(frozen=True)
class Radar:
    title: str
    axes: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Bars:
    rows: tuple[tuple[str, float, str], ...]
    maximum: float | None = None


@dataclass(frozen=True)
class MenuImage:
    image: bytes
    menu: ChoiceMenu
    numbers_in_image: bool = False


@dataclass(frozen=True)
class Sparkline:
    counts: tuple[int, ...]


@dataclass
class Table:
    title: str
    columns: list[str]
    rows: list[list[str | Sparkline]]
    caption: str = ""
    compact: bool = False


@dataclass(frozen=True)
class Flow:
    entry: str
    result: str
    branches: tuple[str, ...] = ()


@dataclass(frozen=True)
class FlowSection:
    title: str
    flows: tuple[Flow, ...]


@dataclass
class Card:
    title: str
    subtitle: str = ""
    filters: list[tuple[str, str]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    groups: list[tuple[str, list[dict[str, str]]]] = field(default_factory=list)
    note: str = ""
    chart: Trend | None = None
    menu: ChoiceMenu | None = None
    tables: list[Table] = field(default_factory=list)
    bars: Bars | None = None
    radar: Radar | None = None
    flow_sections: tuple[FlowSection, ...] = ()
    leaderboard_numbers: tuple[int | None, ...] = ()
    cell_colors: dict[tuple[int, int], str] = field(default_factory=dict)


def menu_table(menu: ChoiceMenu, *, start: int = 1) -> Table:
    return Table(
        "编号选择", ["序号", "选项"],
        [[str(number), label] for number, (label, _) in enumerate(menu.options[start - 1:], start)],
        menu.prompt,
    )
