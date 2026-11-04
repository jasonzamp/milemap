import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import milemap.collector.fuel_finder as col
import milemap.storage.r2 as r2mod
from milemap.config import FuelFinderSettings
from milemap.sources.fuel_finder.client import FuelFinderClient
from tests.test_r2_upload import FakeS3

S = FuelFinderSettings(client_id="cid-123456", client_secret="sec-abcdef",  # noqa: S106
                       base_url="https://ff.test")

PRICES = [{"node_id": "a", "trading_name": "A", "fuel_prices": [
              {"fuel_type": "E10", "price": 139.9,
               "price_change_effective_timestamp": "2026-11-04T10:00:00.000Z"}]},
          {"node_id": "b", "trading_name": "B", "fuel_prices": [
              {"fuel_type": "E10", "price": 141.9,
               "price_change_effective_timestamp": "2026-11-04T09:00:00.000Z"}]}]
STATIONS = [{"node_id": "a", "trading_name": "A"}, {"node_id": "b", "trading_name": "B"},
            {"node_id": "c", "trading_name": "C", "temporary_closure": True}]


@pytest.fixture
def env(tmp_path, monkeypatch):
    api = {"mode": "ok", "gets": [], "tokens": 0}

    def handler(request: httpx.Request):
        if request.url.path.endswith("generate_access_token"):
            api["tokens"] += 1
            return httpx.Response(200, json={"data": {
                "access_token": "tok-zzzzzz", "refresh_token": "ref-yyyyyy",
                "expires_in": 3600, "refresh_token_expires_in": 172800}})
        api["gets"].append((request.url.path, dict(request.url.params)))
        if api["mode"] == "429":
            return httpx.Response(429)
        b = int(request.url.params["batch-number"])
        data = PRICES if request.url.path.endswith("fuel-prices") else STATIONS
        return httpx.Response(200, json=data) if b == 1 else httpx.Response(404)

    def client_factory(settings, **kw):
        http = httpx.Client(transport=httpx.MockTransport(handler))
        return FuelFinderClient(S, http=http, sleep=lambda s: None, min_interval_s=0, **kw)

    s3 = FakeS3()
    monkeypatch.setattr(col, "fuel_finder_settings", lambda: S)
    monkeypatch.setattr(col, "FuelFinderClient", client_factory)
    monkeypatch.setattr(r2mod, "r2_client", lambda: (s3, "bucket"))
    monkeypatch.setattr(col, "mac_sleep_events", lambda a, b: [
        {"at_utc": a.isoformat(), "event": "Sleep"}])
    monkeypatch.setattr(col, "code_version", lambda: "test")
    return {"api": api, "s3": s3, "root": tmp_path}


def keys(s3, prefix):
    return sorted(k for k in s3.objects if k.startswith(prefix))


def test_first_run_then_stations_full_then_dedupe(env):
    r1 = col.tick(runner="test", root=env["root"])
    assert r1["status"] == "ok"
    assert [t["task"] for t in r1["tasks"]] == [
        "prices_incremental", "stations_incremental", "prices_full"]
    assert r1["tasks"][2]["complete_snapshot"] is True
    assert r1["note"].startswith("first run")
    assert len(keys(env["s3"], "_runs/")) == 1
    assert keys(env["s3"], "source=fuel_finder/endpoint=prices_full/")
    assert env["api"]["tokens"] == 1

    r2 = col.tick(runner="test", root=env["root"])
    assert [t["task"] for t in r2["tasks"]] == [
        "prices_incremental", "stations_incremental", "stations_full"]
    inc = r2["tasks"][0]
    assert (inc["records_returned"], inc["rows_written"], inc["skipped_unchanged"]) == (2, 0, 2)
    assert env["api"]["tokens"] == 1  # token reused across runs via the private cache
    j = r2["join_check"]
    assert j["stations_without_prices"] == 1 and j["stations_without_prices_ids"] == ["c"]
    assert j["prices_without_station_record"] == 0


def test_gap_is_recorded_and_next_poll_reaches_back(env):
    col.tick(runner="test", root=env["root"])
    state_path = env["root"] / "state/fuel_finder_state.json"
    st = json.loads(state_path.read_text())
    then = datetime.now(UTC) - timedelta(hours=5)
    st["last_run_started_at"] = then.isoformat()
    st["last_prices_poll_ok_started_at"] = then.isoformat()
    state_path.write_text(json.dumps(st))

    r = col.tick(runner="test", root=env["root"])
    assert r["gap"]["cause"] == "mac_asleep" and r["gap"]["seconds"] >= 5 * 3600 - 5
    gap_keys = keys(env["s3"], "_gaps/source=fuel_finder/")
    assert len(gap_keys) == 1
    since = datetime.fromisoformat(r["plan"]["prices_since"])
    assert since <= then - timedelta(minutes=15) + timedelta(seconds=1)  # covers the gap
    report = col.gaps_report(days=1, root=env["root"])
    assert report["gaps"][0]["cause"] == "mac_asleep" and report["runs"] == 2


def test_429_sets_cooldown_and_next_run_makes_no_calls(env):
    env["api"]["mode"] = "429"
    r = col.tick(runner="test", root=env["root"])
    assert r["status"] == "rate_limited" and r["api"]["requests"] == 1
    env["api"]["mode"] = "ok"
    before = len(env["api"]["gets"])
    r2 = col.tick(runner="test", root=env["root"])
    assert r2["status"] == "skipped_cooldown"
    assert len(env["api"]["gets"]) == before
    assert len(keys(env["s3"], "_runs/")) == 2  # skipped runs are still recorded


def test_overlapping_runs_are_prevented(env):
    paths = col.Paths(env["root"])
    with col.run_lock(paths.lock) as got:
        assert got
        assert col.tick(runner="test", root=env["root"]) == {"status": "skipped_locked"}
