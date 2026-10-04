"""设施入口：导航点 + 出入口子点，停车场不算，相距很近的并成一个，没有就退回坐标点。"""

from app.poi.entries import MAX_ENTRIES, destinations, facility_entries, gate_name

SCHOOL = {
    "name": "上海市朝春中心小学",
    "location": {"lat": 31.247457, "lng": 121.413386},
    "detail_info": {
        "tag": "教育培训;小学",
        # 导航点就落在东南 1 门上（相距约 5 米）
        "navi_location": {"lat": 31.247461, "lng": 121.414005},
        "children": [
            {
                "name": "上海市朝春中心小学-东南2门",
                "show_name": "东南1门",  # 实测 show_name 会错位，不能用
                "classified_poi_tag": "出入口;门",
                "location": {"lat": 31.247035, "lng": 121.413782},
            },
            {
                "name": "上海市朝春中心小学-东南1门",
                "classified_poi_tag": "出入口;门",
                "location": {"lat": 31.247442, "lng": 121.414051},
            },
            {
                "name": "上海市朝春中心小学-地上停车场",
                "classified_poi_tag": "交通设施;停车场;地上停车场",
                "location": {"lat": 31.247417, "lng": 121.414118},
            },
            {
                "name": "上海市朝春中心小学地上停车场-东出入口",
                "classified_poi_tag": "出入口;停车场出入口",
                "location": {"lat": 31.247027, "lng": 121.413731},
            },
        ],
    },
}


def test_gates_and_navigation_point_merge_and_parking_is_dropped():
    entries = facility_entries(SCHOOL)
    assert [e["name"] for e in entries] == ["东南2门", "东南1门"]


def test_navigation_point_alone_when_no_gates_are_listed():
    record = {"detail_info": {"navi_location": {"lat": 31.2, "lng": 121.4}, "children": []}}
    assert facility_entries(record) == [{"lat": 31.2, "lng": 121.4, "name": "导航点"}]


def test_no_detail_means_no_entries_and_destinations_fall_back_to_the_point():
    assert facility_entries({"name": "某小学"}) == []
    place = {"lat": 31.25, "lng": 121.42, "entries": []}
    assert destinations(place) == [(31.25, 121.42)]


def test_destinations_use_only_entries_when_present():
    place = {"lat": 31.25, "lng": 121.42, "entries": facility_entries(SCHOOL)}
    dests = destinations(place)
    assert (31.25, 121.42) not in dests
    assert len(dests) == 2


def test_entries_are_capped_and_bad_coordinates_skipped():
    children = [
        {
            "name": f"某大学-{i}号门",
            "classified_poi_tag": "出入口;门",
            "location": {"lat": 31.2 + i * 0.001, "lng": 121.4},
        }
        for i in range(8)
    ]
    children.insert(0, {"name": "坏点-门", "classified_poi_tag": "出入口;门", "location": {}})
    entries = facility_entries({"detail_info": {"children": children}})
    assert len(entries) == MAX_ENTRIES
    assert all(e["name"].endswith("号门") for e in entries)


def test_gate_name_takes_the_suffix():
    assert gate_name({"name": "上海市朝春中心小学-东南1门"}) == "东南1门"
    assert gate_name({"name": "北门"}) == "北门"
