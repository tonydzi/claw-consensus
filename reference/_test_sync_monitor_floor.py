"""Tests for sync_monitor.floor_verdict: the health floor must go RED when the daemon's universe vanishes.
Run: python3 reference/_test_sync_monitor_floor.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sync_monitor as sm

FLOOR = {"min_folders": 2, "min_peers_configured": 2, "expect_folder_ids": ["a", "b"],
         "device_id": "ME", "zero_peers_red_after_s": 100}
HEALTHY = dict(folders=[{"id": "a"}, {"id": "b"}], devices=["ME", "P1", "P2"],
               conns={"P1": {"connected": True}, "P2": {"connected": False}}, myid="ME")


def run(floor=FLOOR, state=None, now=1000, **over):
    kw = dict(HEALTHY); kw.update(over)
    return sm.floor_verdict(floor, kw["folders"], kw["devices"], kw["conns"], kw["myid"], state or {}, now)


def test_healthy_is_green():
    red, info, _ = run()
    assert red == [] and "cannot judge" in info


def test_vanished_universe_is_red():
    # the 4-day incident: zero folders, zero peers, empty conns
    red, _, _ = run(folders=[], devices=["ME"], conns={})
    assert len(red) >= 3, red


def test_missing_floor_file_is_red():
    red, info, _ = run(floor={"__error__": "no such file"})
    assert red and info.startswith("blind")


def test_foreign_identity_is_red():
    red, _, _ = run(myid="OTHER")
    assert any("myID" in r for r in red)


def test_zero_peers_only_after_threshold():
    conns = {"P1": {"connected": False}}
    red, _, patch = run(conns=conns, now=1000)
    assert not any("ZERO" in r for r in red) and patch["zero_since"] == 1000
    red, _, _ = run(conns=conns, now=1200, state={"__floor__": patch})
    assert any("ZERO" in r for r in red)


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_"):
            v(); n += 1
    print("ok", n)
