from copy import deepcopy

from app.baidu.client import BaiduMapClient
from app.config import Settings
from app.isochrone.geometry import offset_point


def test_recent_evidence_restores_only_complete_same_origin_fine_cached_routes(tmp_path):
    client = BaiduMapClient(Settings(server_ak="test-ak", browser_ak="", cache_dir=tmp_path))
    origin = (31.24, 121.41)
    target = offset_point(*origin, 0, 500)
    route = {
        "requested_origin": list(origin),
        "requested_destination": list(target),
        "response_origin": {"lat": origin[0], "lng": origin[1]},
        "duration_s": 1052,
        "steps": [
            {
                "path": [origin, target],
                "start_location": {"lat": origin[0], "lng": origin[1]},
                "end_location": {"lat": target[0], "lng": target[1]},
            }
        ],
    }
    client._cache.write(client._walking_route_cache_path(origin, target, 10), route)
    assert client.recent_walking_targets(origin) == [target]
    other = offset_point(*origin, 90, 5)
    assert client.recent_walking_targets(other) == []
    incomplete = deepcopy(route)
    incomplete["steps"][0].pop("start_location")
    client._cache.write(client._walking_route_cache_path(origin, target, 10), incomplete)
    assert client.recent_walking_targets(origin) == []
