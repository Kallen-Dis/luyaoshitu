"""综合结论：按模板由算法结果生成。事实清单不含坐标，每个数字都直接取自结果，导出与页面同一段话。"""

import json
import re

from app.export import build_markdown
from app.narrate import build_facts, narrate, template_text

FEATURE = {
    "type": "Feature",
    "geometry": {"type": "Polygon", "coordinates": [[]]},
    "properties": {
        "name": "上海市普陀区桃浦镇",
        "minutes": 15.0,
        "area_km2": 1.1814,
        "min_radius_m": 402.3,
        "max_radius_m": 804.9,
        "compactness": 0.5,
        "center": {"lat": 31.28, "lng": 121.36},
        "coverage": {"categories": {"医药": 0}, "nearby_categories": {"医药": 17}},
        "report": {
            "total": 35.3,
            "grade": "弱",
            "ideal_area_km2": 3.664,
            "area_ratio": 0.322,
            "dimensions": {"reach": 50.6, "cover": 0.0},
            "categories": {"医药": 0},
            "failed_categories": [],
            "cell_count": 116,
            "blind_cell_count": 116,
            "blind_ratio": {"医药": 0.81},
            "prescriptions": [
                {"action": "site", "category": "医药", "title": "补设医药", "reason": "覆盖 21 格"}
            ],
        },
    },
}


def test_facts_carry_numbers_but_no_coordinates():
    facts = build_facts(FEATURE)
    text = json.dumps(facts, ensure_ascii=False)
    assert "31.28" not in text and "121.36" not in text
    assert facts["地点"] == "桃浦镇"
    assert facts["时间阈值_分钟"] == 15
    assert facts["网格盲区"]["各类缺失占比_百分比"]["医药"] == 81.0


def test_template_has_four_sections_and_only_numbers_from_the_result():
    text = narrate(FEATURE)["text"]
    heads = re.findall(r"【([^】]+)】", text)
    assert heads == ["总体结论", "主要问题", "诊疗建议", "数据边界"]
    assert "桃浦镇" in text and "35.3" in text and "1.1814" in text and "32.2" in text
    # 没有设施时写明「附近有、走不进圈」
    assert "医药 17 处" in text and "补设医药" in text


def test_same_result_always_reads_the_same():
    assert narrate(FEATURE)["text"] == narrate(FEATURE)["text"]


def test_simulated_data_is_called_out_in_the_first_paragraph():
    fake = json.loads(json.dumps(FEATURE))
    fake["properties"]["simulated"] = True
    first = narrate(fake)["text"].split("\n\n")[0]
    assert "离线模拟数据" in first and "不代表真实路网" in first


def test_export_always_carries_the_same_conclusion():
    md = build_markdown(FEATURE)
    assert "## 综合结论" in md
    assert template_text(build_facts(FEATURE)) in md
