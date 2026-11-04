from datetime import UTC, datetime

from milemap.sources.fuel_finder.probe import join_check
from milemap.storage.raw import RawRunWriter

T = datetime(2026, 11, 4, 22, 0, tzinfo=UTC)


def _full_run(root, endpoint, records, run_id):
    w = RawRunWriter(root, "fuel_finder", endpoint, run_id=run_id, started_at=T)
    w.add_batch(number=1, records=records, path="/x", params={"batch-number": 1},
                fetched_at=T, status=200)
    w.close(status="ok", extra={"request_params": {}})


def test_join_check_reports_both_directions(tmp_path):
    _full_run(tmp_path, "prices_full",
              [{"node_id": "a", "fuel_prices": []}, {"node_id": "b", "fuel_prices": []},
               {"node_id": "p_only", "fuel_prices": []}], "run_p")
    _full_run(tmp_path, "stations_full",
              [{"node_id": "a"}, {"node_id": "b"},
               {"node_id": "s_only", "trading_name": "X", "temporary_closure": True}], "run_s")
    j = join_check(tmp_path)
    assert (j["in_both"], j["prices_without_station_record"], j["stations_without_prices"]) == (
        2, 1, 1)
    assert j["examples_prices_without_station"][0]["node_id"] == "p_only"
    assert j["examples_stations_without_prices"][0]["temporary_closure"] is True
    assert j["closure_flags_of_stations_without_prices"]["temporary_closure"] == {"True": 1}
