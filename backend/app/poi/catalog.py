"""民生设施品类词表。

一个品类对应**多个检索关键词**，这是"多源"的由来：百度的地点检索按关键词召回，
单个词覆盖不全——「菜市场」查不到挂牌为「农贸市场」「生鲜超市」的店。
多词并查提升召回，代价是同一设施会被反复返回，必须在清洗阶段去重。

`exclude` 是按名称剔除的误召回词。关键词检索是模糊匹配，「小学」会连带召回
「小学生辅导班」「小学教育科技公司」——它们不是可就近上学的设施，计入会把
教育盲区掩盖掉。这类误召回只能按名称拦，因为百度返回的 POI 分类字段
在免费额度下并不稳定可用。

`key_facility` 标记命题点名的三类设施（菜市场、药店、小学）。盲区判定只对它们
做网格级的逐点测距：判定成本与品类数成正比，而这三类是评分细则明确要求的。

`entrances` 标记「有面积、按入口测距」的品类：学校的坐标点常在校园中间，居民要走到的是校门
（见 poi/entries.py）。目前只有小学：有围墙、门少，按点测和按门测差得最多；
药店是临街门店，菜市场的面积与门的位置差异还没量过，都先按点测。

`exclude_tags` 按百度的分类标签拦误召回（只对带标签的检索结果生效）：
「小学」会召回分类为「成人教育」「托管班」的机构，名字里却看不出来。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    name: str
    keywords: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    key_facility: bool = False
    entrances: bool = False
    exclude_tags: tuple[str, ...] = ()


CATEGORIES: tuple[Category, ...] = (
    Category(
        name="生鲜采买",
        keywords=("菜市场", "农贸市场", "生鲜超市"),
        exclude=("花卉", "建材", "批发市场", "家具"),
        key_facility=True,
    ),
    Category(
        name="医药",
        keywords=("药店", "药房"),
        exclude=("医药公司", "医药科技", "器械"),
        key_facility=True,
    ),
    Category(
        name="基础教育",
        keywords=("小学",),
        exclude=("辅导", "补习", "培训", "托管", "教育科技", "文化传播", "家教"),
        key_facility=True,
        entrances=True,
        exclude_tags=("成人教育", "培训机构", "托管班", "留学中介", "亲子教育", "幼儿园"),
    ),
    Category(
        name="基础医疗",
        keywords=("社区卫生服务中心", "诊所", "医院"),
        exclude=("兽医", "宠物", "医疗器械", "整形", "医美"),
    ),
    Category(
        name="养老服务",
        keywords=("养老院", "敬老院", "社区养老服务中心", "日间照料中心"),
        exclude=("养老保险", "咨询"),
    ),
    Category(
        name="文体休闲",
        keywords=("公园", "体育馆", "图书馆", "健身房"),
        exclude=("停车场", "售楼", "地产"),
    ),
)

KEY_CATEGORIES: tuple[Category, ...] = tuple(c for c in CATEGORIES if c.key_facility)


def by_name(name: str) -> Category | None:
    return next((c for c in CATEGORIES if c.name == name), None)
