import asyncio

from app.isochrone.validation import validate_in_stages


def test_primary_targets_all_run_before_shared_backups_and_conflicts_are_not_replaced():
    calls, known = [], []
    selections = [
        {"id": "a", "region_nodes": [], "alternatives": [{"id": "a-conflict"}, {"id": "a-easy"}]},
        {"id": "b", "region_nodes": [], "alternatives": []},
    ]

    async def evaluate(selection, nodes):
        key = selection["id"]
        calls.append(key)
        verdict = {
            "a": "unknown",
            "b": "consistent",
            "a-conflict": "conflict",
            "a-easy": "consistent",
        }[key]
        return {"id": key, "verdict": verdict, "path_upper_conflict": False}

    results = asyncio.run(validate_in_stages(selections, evaluate, lambda r: known.append(r["id"])))
    assert calls == ["a", "b", "a-conflict"]
    assert known == ["b", "a-conflict"]
    assert results[0]["verdict"] == "conflict" and len(results[0]["attempts"]) == 2


def test_path_upper_conflict_cannot_be_hidden_by_another_backup():
    calls = []
    selections = [
        {"id": "a", "region_nodes": [], "alternatives": [{"id": "danger"}, {"id": "easy"}]}
    ]

    async def evaluate(selection, nodes):
        calls.append(selection["id"])
        return {"verdict": "unknown", "path_upper_conflict": selection["id"] == "danger"}

    results = asyncio.run(validate_in_stages(selections, evaluate, lambda r: None))
    assert calls == ["a", "danger"] and results[0]["path_upper_conflict"]
