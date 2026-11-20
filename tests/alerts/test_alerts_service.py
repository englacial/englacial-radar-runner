"""The alerts service in this process, with a simulated clock, hand-written inputs and the stand-in
ntfy server on 127.0.0.1."""
import json
import os

import pytest

from alerts_rig import BOOT_B, LAT, LON, POSITION_STRINGS, TOKEN, TOPIC, Rig
from fake_ntfy import FakeNtfy, closed_port
from radar_runner.alerts import logic as L


@pytest.fixture
def server():
    with FakeNtfy() as s:
        yield s


def titles(server):
    return [b["title"] for b in server.bodies()]


def without_summary(server):
    return [b for b in server.bodies() if "summary" not in b["title"]]


def test_the_boot_summary_goes_out_once_per_boot(tmp_path, server):
    rig = Rig(tmp_path, server.url)
    rig.start()
    rig.tick(3)
    [summary] = server.bodies()
    assert summary["title"] == "test-unit: boot 42 summary" and summary["priority"] == L.DEFAULT
    assert summary["topic"] == TOPIC and server.received[0]["headers"]["authorization"] == "Bearer " + TOKEN
    rig.stop()
    rig.start()                     # a restart in the same boot: no second summary
    rig.tick(3)
    assert len(server.bodies()) == 1
    rig.reboot()
    rig.tick(3)
    assert len(server.bodies()) == 2 and "Previous boot 42" in server.bodies()[1]["message"]


def test_the_summary_waits_for_its_uptime(tmp_path, server):
    rig = Rig(tmp_path, server.url, summary_after_s=1100.0)
    rig.start()
    rig.tick(50)
    assert server.bodies() == []
    rig.tick(60)
    assert len(server.bodies()) == 1


def test_boot_summary_content_with_a_previous_boot_ended_by_the_power_service(tmp_path, server):
    rig = Rig(tmp_path, server.url)
    col = rig.collections / "b00041_s002_20261119T101500Z"
    col.mkdir()
    (col / "collection.json").write_text(json.dumps({
        "recording_started": {"uptime_s": 100.0}, "data": {"file": "rx_samps.bin"},
        "end": {"uptime_s": 3700.0, "how": "stopped", "reason": "power loss"}}))
    (col / "rx_samps.bin.0").write_bytes(b"\0" * 2_000_000)
    (rig.collections / "b00041_s001_20261119T090000Z").mkdir()
    rig.start()
    rig.tick(2)
    rig.power["guard"]["last_shutdown"] = {"uptime_s": 36000.0, "early": False,
                                           "why": "loss agreed for 8.2 s; both signals say loss", "wall_time": 1.0}
    rig.manager = {"state": "fault", "not_ready": [], "fault": {"kind": "start_failures", "reason": "build failed"}}
    rig.telemetry["clock"]["synced"] = 0
    rig.reboot()
    rig.tick(2)
    text = server.bodies()[-1]["message"]
    for piece in ("Boot 42", "CLOCK NOT SET", "Previous boot 42", "shut down by the power service (loss agreed for 8.2 s",
                  "900.0 GB free", "97.0 GB free", "Last collection: b00041_s002_20261119T101500Z, stopped (power loss)",
                  "recorded 60 min", "0.00 GB", "2 collections", "FAULT start_failures: build failed",
                  "Safety watcher: ok", "Telemetry: ok", "Power service: normal, armed", "Alert queue:"):
        assert piece in text, (piece, text)


def test_a_previous_boot_that_ended_without_a_clean_stop(tmp_path, server):
    rig = Rig(tmp_path, server.url)
    rig.start()
    rig.tick(400)                   # past the heartbeat: the state file says how long this boot lived
    rig.service = None              # a power cut: no cleanup
    rig.reboot()
    rig.tick(2)
    text = server.bodies()[-1]["message"]
    assert "without a clean stop" in text and "Previous boot 42: up at least" in text


def test_alerts_raised_offline_are_sent_when_the_link_returns_urgent_first(tmp_path, server):
    rig = Rig(tmp_path, closed_port())
    rig.start()
    rig.tick(2)
    rig.manager_event(event="fault", kind="start_failures", reason="build failed")
    rig.watcher_event(event="trip", severity="warning", condition="amp_enabled", text="enabled with no radar")
    rig.watcher_event(event="stuck", severity="urgent", text="AMPLIFIER STUCK ENABLED: 2.5 A")
    status = rig.tick(5)
    assert status["link"]["state"] == "failing" and status["queue"]["length"] == 4      # with the summary
    assert len(rig.queued()) == 4
    rig.stop()                      # and a restart while offline: nothing is lost
    rig.start()
    rig.tick(3)
    assert len(rig.queued()) == 4 and server.bodies() == []
    rig.service.ntfy["server"] = server.url         # the link returns
    rig.tick(10)
    assert rig.queued() == []
    got = titles(server)
    assert got[0] == "test-unit: AMPLIFIER STUCK ENABLED"          # urgent first
    assert [b["priority"] for b in server.bodies()] == [5, 4, 3, 3]
    assert rig.service.tick()["link"]["state"] in ("ok", "idle")


def test_backoff_doubles_and_is_bounded_and_429_waits_as_asked(tmp_path, server):
    rig = Rig(tmp_path, server.url, **{"ntfy.retry_s": 2.0, "ntfy.retry_max_s": 8.0})
    server.mode, server.status = "status", 503
    rig.start()
    attempts = []
    for _ in range(60):
        rig.tick()
        attempts.append(server.attempts)
    gaps = [i for i in range(1, 60) if attempts[i] > attempts[i - 1]]
    spacing = [b - a for a, b in zip(gaps, gaps[1:])]
    assert spacing[:3] == [2, 4, 8] and max(spacing) == 8
    server.status, server.retry_after = 429, 30
    before = server.attempts
    rig.tick(10)
    assert server.attempts - before == 1           # then it waits 30 s, not 8
    rig.tick(25)
    assert server.attempts - before == 2


def test_when_every_message_is_refused_nothing_is_dropped(tmp_path, server):
    # A 400 for everything is the settings (a refused topic, a change at the server), not the alerts.
    rig = Rig(tmp_path, server.url)
    server.mode, server.status = "status", 400
    rig.start()
    rig.watcher_event(event="stuck", severity="urgent", text="AMPLIFIER STUCK ENABLED")
    rig.manager_event(event="fault", kind="start_failures", reason="x")
    status = rig.tick(400)
    assert len(rig.queued()) == 3 and status["counters"]["rejected"] == 0
    assert status["link"]["state"] == "failing" and len(server.refused) <= 60      # once per retry_max_s (8 s)
    server.mode = "ok"
    rig.tick(30)
    assert rig.queued() == [] and len(server.bodies()) == 3
    assert any("summary" in b["title"] for b in server.bodies())


def test_one_message_the_server_refuses_while_others_go_through_is_dropped_after_three_tries(tmp_path, server):
    rig = Rig(tmp_path, server.url, **{"ntfy.retry_max_s": 10.0})
    server.refuse = lambda body: "BAD" in body["message"]
    rig.start()
    rig.tick(3)
    rig.manager_event(event="fault", kind="a", reason="BAD")
    rig.tick(2)
    for i in range(3):                      # others keep going through in between
        rig.manager_event(event="fault", kind=f"good{i}", reason="fine")
        rig.tick(12)
    status = rig.tick(12)
    # The first refusal proves nothing (it may be the server); three more with others accepted between.
    assert status["counters"]["rejected"] == 1 and len(server.refused) == 4 and rig.queued() == []
    assert [t for t in titles(server) if "good" in t] == [f"test-unit: radar FAULT: good{i}" for i in range(3)]


def test_a_restart_between_the_send_and_the_deletion_repeats_that_one_alert_only(tmp_path, server):
    rig = Rig(tmp_path, server.url)
    rig.start()
    for i in range(3):
        rig.manager_event(event="fault", kind=f"k{i}", reason="x")

    class Killed(BaseException):
        pass

    def killed(name):
        raise Killed()
    rig.service.queue.remove = killed           # dies right after ntfy accepted the first alert
    with pytest.raises(Killed):
        rig.tick()
    assert len(server.bodies()) == 1
    rig.service = None
    rig.start()
    rig.tick(5)
    got = titles(server)
    assert len(got) == 5 and got[0] == got[1]           # summary + 3 faults, the first of them twice
    assert rig.queued() == []


def test_positions_never_reach_the_server(tmp_path, server):
    rig = Rig(tmp_path, server.url, **{"conditions.gps_s": 0.0, "conditions.not_ready_s": 0.0})
    rig.start()
    rig.telemetry["sources"]["gps"].update(health="stale", reason=f"no fix time at lat {LAT} lon {LON}")
    rig.manager = {"state": "not_ready", "not_ready": [f"gps: last fix {LAT}, {LON}"]}
    rig.watcher_event(event="warning", severity="warning", warning="x", text=f"truck at {LAT},{LON}",
                      lat=float(LAT), lon=float(LON))
    rig.watcher_event(event="trip", severity="urgent", condition="amp_temp",
                      text=f"amplifier temperature: reached 55 C at position {LAT} {LON} (gps_Lat -778463000)")
    rig.manager_event(event="fault", kind="start_failures", reason=f"moved to {LAT}°S {LON}°E", position=[LAT, LON])
    rig.manager_event(event="ended", failure=f"radar exited at 77°50'47\"S 166°40'06\"E", collection="b00042_s001_x")
    rig.tick(10)
    assert len(server.received) >= 6
    for r in server.received:
        everything = r["raw"].decode() + json.dumps(r["headers"])
        for piece in POSITION_STRINGS + ["77°50", "166°40"]:
            assert piece not in everything, (piece, everything)
        assert TOKEN not in json.dumps(r["body"])
    # And what was queued on disk had none either.
    assert all(p not in (tmp_path / "state" / "alerts-state.json").read_text() for p in POSITION_STRINGS)


def test_an_alert_storm_gives_a_few_notifications_and_a_count(tmp_path, server):
    rig = Rig(tmp_path, server.url)
    rig.start()
    rig.tick()
    for i in range(300):                    # a flapping reading
        rig.watcher_event(event="trip", severity="urgent", condition="amp_temp", text=f"trip {i}")
        rig.watcher_event(event="clear", severity="info", condition="amp_temp", text=f"clear {i}")
    rig.tick(10)
    sent = without_summary(server)
    assert len(sent) == 6                   # three trips, and the three "over" that belong to them
    assert [b["message"].split("\n")[0] for b in sent if b["priority"] == 5] == ["trip 0", "trip 1", "trip 2"]
    rig.tick(3600, dt=1.0)
    notes = [b for b in without_summary(server)[6:]]
    assert len(notes) == 1 and notes[0]["message"].startswith("297 more alerts of this kind were held back")
    assert "trip 299" in notes[0]["message"] and notes[0]["priority"] == 5


def test_clock_steps_change_nothing(tmp_path, server):
    rig = Rig(tmp_path, server.url, **{"conditions.gps_s": 100.0})
    rig.start()
    rig.tick(2)
    for i in range(5):
        rig.watcher_event(event="trip", severity="urgent", condition="amp_temp", text=f"trip {i}")
    rig.tick(2)
    assert len(without_summary(server)) == 3
    rig.telemetry["sources"]["gps"].update(health="stale", reason="no fix time")
    rig.tick(10)
    rig.clock.wall_s += 10 * 365 * 86400        # GPS sets the clock: ten years forward
    rig.tick(10)
    rig.clock.wall_s -= 20 * 365 * 86400        # and a bad step back
    rig.tick(10)
    assert len(without_summary(server)) == 3    # the storm limit holds; the condition has not fired early
    rig.tick(75)
    assert len(without_summary(server)) == 4 and "GPS" in without_summary(server)[-1]["title"]
    assert len([b for b in server.bodies() if "summary" in b["title"]]) == 1


def test_a_supply_trip_during_a_power_loss_is_not_sent(tmp_path, server):
    rig = Rig(tmp_path, server.url)
    rig.start()
    rig.tick()
    rig.power.update(state="loss", power_present=False, radar_stop_requested=True)
    rig.watcher_event(event="trip", severity="warning", condition="voltage", text="28 V supply: below 26 V")
    rig.tick(3)
    assert without_summary(server) == [] and rig.service.counters["voltage_trips_in_power_loss"] == 1
    rig.power.update(state="normal", power_present=True, radar_stop_requested=False)
    rig.watcher_event(event="trip", severity="warning", condition="voltage", text="28 V supply: below 26 V")
    rig.tick(3)
    assert len(without_summary(server)) == 1


def test_events_files_rotating_while_followed(tmp_path, server):
    rig = Rig(tmp_path, server.url)
    rig.start()
    rig.tick()
    rig.manager_event(event="fault", kind="a", reason="r")
    os.replace(rig.manager_events, str(rig.manager_events) + ".1")
    rig.manager_event(event="fault", kind="b", reason="r")
    rig.tick(3)
    rig.manager_event(event="fault", kind="c", reason="r")
    os.replace(rig.manager_events, str(rig.manager_events) + ".1")
    rig.tick(3)
    rig.manager_event(event="fault", kind="d", reason="r")
    rig.tick(3)
    assert [t.split("FAULT: ")[1] for t in titles(server) if "FAULT" in t] == ["a", "b", "c", "d"]


def test_events_written_while_the_service_was_down_or_the_pi_off_are_sent_afterwards(tmp_path, server):
    rig = Rig(tmp_path, server.url)
    rig.start()
    rig.tick(2)
    rig.stop()
    rig.watcher_event(event="stuck", severity="urgent", text="AMPLIFIER STUCK ENABLED: 2.5 A")
    rig.reboot()
    rig.tick(3)
    [stuck] = [b for b in server.bodies() if "STUCK" in b["title"]]
    assert "2026-11-19 20:00:00Z" in stuck["message"]          # when it happened, not when it was sent


def test_a_restart_of_a_service_without_a_clean_stop_is_reported(tmp_path, server):
    rig = Rig(tmp_path, server.url)
    rig.start()
    rig.tick()
    rig.manager_event(event="manager_started", pid=1)
    rig.manager_event(event="state", old="not_ready", new="ready")
    rig.tick(30)
    rig.manager_event(event="manager_started", pid=2)       # killed in between: no manager_stopped
    rig.watcher_event(event="start", severity="info", text="safety watcher started")
    rig.watcher_event(event="stop", severity="info", text="safety watcher stopping")
    rig.watcher_event(event="start", severity="info", text="safety watcher started")
    rig.tick(3)
    assert [b["title"] for b in without_summary(server)] == ["test-unit: radar manager restarted"]


def test_conditions_from_status_files(tmp_path, server):
    rig = Rig(tmp_path, server.url, **{"conditions.status_stale_s": 30.0, "conditions.clear_s": 5.0})
    rig.start()
    rig.tick(2)
    rig.watcher = None                          # the watcher stops writing
    rig.tick(25)                                # stale after 5 s, then 30 s more
    assert without_summary(server) == []
    rig.tick(15)
    [down] = without_summary(server)
    assert down["title"] == "test-unit: safety watcher not running" and down["priority"] == L.HIGH
    rig.watcher = {"ok": True, "tripped": False, "reason": ""}
    rig.tick(10)
    assert without_summary(server)[-1]["title"] == "test-unit: over: safety watcher not running"
    assert without_summary(server)[-1]["priority"] == L.LOW


def test_disk_levels_from_telemetry(tmp_path, server):
    rig = Rig(tmp_path, server.url)
    rig.start()
    rig.tick()
    rig.telemetry["sources"]["pi"]["values"]["nvme_free_gb"] = 40.0
    rig.tick(2)
    rig.telemetry["sources"]["pi"]["values"]["nvme_free_gb"] = 20.0
    rig.tick(2)
    got = without_summary(server)
    assert [b["title"] for b in got] == ["test-unit: data drive below 50 GB free",
                                         "test-unit: data drive below 25 GB free"]
    assert [b["priority"] for b in got] == [L.DEFAULT, L.HIGH]
    rig.reboot()
    rig.tick(3)
    assert len(without_summary(server)) == 2            # not again after a reboot
    assert "data drive below 25 GB" in server.bodies()[-1]["message"]


def test_damaged_huge_or_odd_inputs_do_not_stop_the_service(tmp_path, server):
    rig = Rig(tmp_path, server.url)
    rig.start()
    rig.tick()
    rig.telemetry = rig.watcher = rig.manager = rig.power = None
    (rig.run_dir / "telemetry.json").write_text('{"state": "ok", "sources": {"pi": ')              # half written
    (rig.run_dir / "watcher.json").write_bytes(b"{" + b" " * (2 << 20))                            # huge
    (rig.run_dir / "manager.json").write_text(json.dumps({"uptime_s": 1e12, "state": ["x"]}))      # odd
    (rig.run_dir / "power.json").write_text(json.dumps({"uptime_s": rig.clock.up + 2, "time": 1,
                                                        "warnings": "not a dict", "guard": 5}))
    with open(rig.watcher_events, "ab") as f:
        f.write(b'{"event": "trip", "condition": {"x": 1}, "severity": 7}\n[1]\n"text"\n{"event": 5}\n')
        f.write(b"\xff\xfe garbage\n" + b"x" * 100000 + b"\n")
    status = rig.tick(5)
    assert status["errors"] == {} and status["running"] is True
    rig.service.config["status"]["stale_after_s"] = 1e9
    rig.tick(2)
    assert rig.service.tick()["errors"] == {}


def test_a_damaged_state_file_starts_afresh_without_resending_history(tmp_path, server):
    rig = Rig(tmp_path, server.url)
    rig.start()
    rig.manager_event(event="fault", kind="a", reason="r")
    rig.tick(2)
    rig.stop()
    (rig.state_dir / "alerts-state.json").write_bytes(b'{"version": 1, "tails": {"manag')     # cut off
    rig.start()
    rig.tick(2)
    assert [t for t in titles(server) if "FAULT" in t] == ["test-unit: radar FAULT: a"]
    assert "damaged" in rig.service.tick()["state_file"]["note"]


@pytest.mark.parametrize("same_boot", [True, False])
def test_a_state_file_with_nonsense_values_never_stops_the_service(tmp_path, server, same_boot):
    rig = Rig(tmp_path, server.url)
    rig.start()
    rig.tick(2)
    rig.stop()
    state = rig.state()
    state.update(seq="x", boot="b", alerts={"x": "y", "5": 1}, previous=7, uptime_s="long",
                 conditions={"gps": {"since": "x"}, "pi_hot": 5},
                 limiter={"kinds": {"k": {"times": ["a", None], "held": "many", "priority": 99}}, "recent": [None],
                          "passed": [], "held_total": -4},
                 tails={"watcher": {"position": {"inode": "x", "offset": -1}}, "manager": 5}, disk_levels="x")
    if not same_boot:
        state["boot_id"] = "another-boot"
    (rig.state_dir / "alerts-state.json").write_text(json.dumps(state))
    rig.start()
    rig.manager_event(event="fault", kind="a", reason="r")
    status = rig.tick(3)
    assert status["errors"] == {}
    assert any("FAULT" in t for t in titles(server))
    if not same_boot:
        assert any("Previous boot ?" in b["message"] for b in server.bodies())


def test_test_alerts_from_the_inbox_go_through_the_queue(tmp_path, server):
    rig = Rig(tmp_path, server.url)
    rig.start()
    inbox = rig.state_dir / "alerts-inbox"
    (inbox / "1-1.json").write_text(json.dumps({"id": "abc123", "priority": "urgent", "text": "hello"}))
    (inbox / "2-1.json").write_text("not json")
    (inbox / ".3-1.json.tmp").write_text("{}")         # still being written by alertctl
    status = rig.tick(3)
    test = [b for b in server.bodies() if "test alert" in b["title"]]
    assert len(test) == 1 and test[0]["priority"] == 5 and "abc123" in test[0]["message"]
    assert sorted(os.listdir(inbox)) == [".3-1.json.tmp"]
    assert any(r.get("test_id") == "abc123" and r["result"] == "sent" for r in status["recent"])


def test_the_status_file_says_what_the_service_does(tmp_path, server):
    rig = Rig(tmp_path, server.url)
    rig.start()
    rig.tick(2)
    d = json.loads((rig.run_dir / "alerts.json").read_text())
    assert d["ntfy"] == {"configured": True, "problem": ""} and d["link"]["state"] in ("ok", "idle")
    assert d["queue"]["length"] == 0 and d["counters"]["sent"] == 1 and d["summary_sent"] is True
    assert set(d["events"]) == {"watcher", "manager"} and d["boot"] == 42
    text = json.dumps(d)
    assert TOKEN not in text and TOPIC not in text and server.url not in text      # no secret in the status
    rig.stop()
    assert json.loads((rig.run_dir / "alerts.json").read_text())["running"] is False
    assert rig.state()["clean_stop"] is True


def test_without_ntfy_settings_alerts_are_queued_only(tmp_path):
    rig = Rig(tmp_path, "-")
    rig.start()
    rig.manager_event(event="fault", kind="a", reason="r")
    status = rig.tick(3)
    assert status["ntfy"]["configured"] is False and "secrets.yaml" in status["ntfy"]["problem"]
    assert status["link"]["state"] == "unconfigured" and len(rig.queued()) == 2


def test_a_full_queue_keeps_the_urgent_alerts(tmp_path):
    rig = Rig(tmp_path, closed_port(), queue_max=10, **{"storm.burst": 100, "storm.max_per_hour": 100})
    rig.start()
    rig.watcher_event(event="stuck", severity="urgent", text="AMPLIFIER STUCK ENABLED")
    for i in range(30):
        rig.manager_event(event="ended", failure=f"f{i}")
    rig.tick(2)
    names = rig.queued()
    assert len(names) == 10 and "p5-000000000001.json" in names
    status = rig.tick()             # 1 urgent, 30 low, the summary: 22 dropped, the lowest first
    assert status["queue"]["dropped"] == 22 and status["queue"]["by_priority"] == {"urgent": 1, "default": 1, "low": 8}


def test_no_other_services_files_are_changed(tmp_path, server):
    rig = Rig(tmp_path, server.url)
    rig.start()
    rig.manager_event(event="fault", kind="a", reason="r")
    rig.watcher_event(event="stuck", severity="urgent", text="AMPLIFIER STUCK ENABLED")
    others = [rig.watcher_events, rig.manager_events] + [rig.run_dir / f"{n}.json" for n in
                                                         ("telemetry", "watcher", "manager", "power")]
    before = {p: (p.stat().st_ino, p.read_bytes()) for p in others}
    for _ in range(5):                  # the inputs are not rewritten here: they must stay byte for byte
        rig.clock.advance(1.0)
        rig.service.tick()
    assert {p: (p.stat().st_ino, p.read_bytes()) for p in others} == before
    assert len(server.bodies()) == 3
    own = {"alerts-state.json", "alerts-queue", "alerts-inbox", "watcher-events.jsonl", "manager-events.jsonl"}
    assert set(os.listdir(rig.state_dir)) == own


def test_the_secrets_never_appear_in_what_is_sent_or_kept(tmp_path, server):
    rig = Rig(tmp_path, server.url)
    rig.start()
    rig.manager_event(event="fault", kind="a", reason=f"posting {TOKEN} to {server.url}/{TOPIC} failed")
    rig.tick(3)
    for r in server.received:
        body = json.dumps(r["body"]["message"]) + r["body"]["title"]
        assert TOKEN not in body        # the token only in the header; topic and server are ntfy's own
    for name in os.listdir(rig.state_dir / "alerts-queue"):
        assert TOKEN not in (rig.state_dir / "alerts-queue" / name).read_text()


def test_a_new_boot_is_told_apart_by_the_boot_id_not_the_clock(tmp_path, server):
    rig = Rig(tmp_path, server.url)
    rig.start()
    rig.tick(2)
    rig.stop()
    rig.clock.up += 5                                # same boot, the clock stepped back by a year
    rig.clock.wall_s -= 365 * 86400
    rig.start()
    rig.tick(2)
    assert len(server.bodies()) == 1
    rig.reboot(boot_id=BOOT_B, uptime=10_000.0)      # a new boot with a larger uptime than before
    rig.tick(2)
    assert len(server.bodies()) == 2


def test_settings_that_cannot_work_revert_to_the_defaults_with_a_warning(tmp_path, server):
    rig = Rig(tmp_path, server.url, **{"storm.window_s": -5.0, "ntfy.retry_max_s": 0.5, "queue_max": 0,
                                       "message_max_chars": 10})
    svc = rig.start()
    assert svc.cfg["storm"]["window_s"] == 3600.0 and svc.cfg["queue_max"] == 200
    assert svc.cfg["message_max_chars"] == 1500 and svc.cfg["ntfy"]["retry_s"] == 10.0
    assert len(svc.config_warnings) == 4 and all("defaults used" in w for w in svc.config_warnings)


def test_long_messages_are_cut_to_what_ntfy_takes(tmp_path, server):
    rig = Rig(tmp_path, server.url, message_max_chars=3000)
    rig.start()
    rig.manager_event(event="fault", kind="a", reason="ü" * 5000)
    rig.tick(3)
    [body] = [b for b in server.bodies() if "FAULT" in b["title"]]
    assert len(body["message"].encode()) <= 4000
    assert "-- test-unit" in body["message"] and "..." in body["message"]


# ---- failure paths found in review

def test_a_manager_log_cut_short_by_a_power_cut_is_not_replayed(tmp_path, server):
    rig = Rig(tmp_path, server.url)
    rig.start()
    rig.tick(2)
    for i in range(12):                     # weeks of history, all read and sent
        rig.clock.advance(4000)
        rig.manager_event(event="fault", kind="start_failures", reason=f"old fault {i}")
        rig.manager_event(event="fault_cleared", kind="start_failures", by="command")
        rig.tick(2)
    synced = os.path.getsize(rig.manager_events)
    rig.manager_event(event="stop", kind="power", reason="power loss")      # read, never on the drive
    rig.manager_event(event="manager_stopped", radar_stopped=True)
    rig.tick(2)
    rig.stop()
    os.truncate(rig.manager_events, synced)
    before = len(server.bodies())
    rig.reboot(BOOT_B, uptime=20.0)
    rig.manager_event(event="manager_started", pid=1)
    rig.tick(5)
    rig.clock.advance(60)
    rig.manager_event(event="fault", kind="start_failures", reason="REAL NEW FAULT")
    status = rig.tick(3)
    new = [b for b in server.bodies()[before:] if "summary" not in b["title"]]
    assert [b["message"].split("\n")[0][:60] for b in new] == [
        "The radar manager is in fault (start_failures): REAL NEW FAULT"[:60]]
    assert status["events"]["manager"]["lost"] == 1


def test_a_sent_alert_that_cannot_be_deleted_is_sent_once(tmp_path, server, monkeypatch):
    rig = Rig(tmp_path, closed_port())
    rig.start()
    rig.tick(2)
    rig.watcher_event(event="stuck", severity="urgent", text="AMPLIFIER STUCK ENABLED: 2.5 A")
    rig.tick(3)
    queue_dir = str(rig.state_dir / "alerts-queue")
    real_unlink = os.unlink

    def read_only(path, *a, **k):
        if str(path).startswith(queue_dir):
            raise OSError(30, "Read-only file system")
        return real_unlink(path, *a, **k)
    monkeypatch.setattr(os, "unlink", read_only)
    rig.service.ntfy["server"] = server.url
    rig.service.next_send_at = 0
    status = rig.tick(20)
    assert sorted(b["title"] for b in server.bodies()) == ["test-unit: AMPLIFIER STUCK ENABLED",
                                                           "test-unit: boot 42 summary"]
    assert len(status["sent_not_deleted"]) == 2 and "Read-only" in status["queue"]["error"]
    monkeypatch.setattr(os, "unlink", real_unlink)          # the card is back
    status = rig.tick(70)
    assert status["sent_not_deleted"] == [] and rig.queued() == [] and len(server.bodies()) == 2


def test_an_inbox_request_that_cannot_be_deleted_is_not_sent_again_and_again(tmp_path, server, monkeypatch):
    rig = Rig(tmp_path, server.url)
    rig.start()
    rig.tick(2)
    inbox = rig.state_dir / "alerts-inbox"
    (inbox / "1.json").write_text(json.dumps({"id": "abc", "priority": "urgent"}))
    real_unlink = os.unlink

    def denied(path, *a, **k):
        if str(path).startswith(str(inbox)):
            raise PermissionError(13, "Permission denied")
        return real_unlink(path, *a, **k)
    monkeypatch.setattr(os, "unlink", denied)
    status = rig.tick(20)
    assert [b for b in server.bodies() if "test alert" in b["title"]] == []
    assert "Permission denied" in status["inbox"]["problem"]
    monkeypatch.setattr(os, "unlink", real_unlink)          # the directory is put right
    status = rig.tick(3)
    assert len([b for b in server.bodies() if "test alert" in b["title"]]) == 1
    assert status["inbox"]["problem"] == "" and os.listdir(inbox) == []


def test_records_of_queue_files_that_are_gone_are_pruned_and_inbox_leftovers_removed(tmp_path, server):
    rig = Rig(tmp_path, server.url)
    (rig.state_dir / "alerts-inbox").mkdir(parents=True)
    (rig.state_dir / "alerts-inbox" / ".123-4.json.tmp").write_text("{")     # an alertctl killed half way
    svc = rig.start()
    assert not (rig.state_dir / "alerts-inbox" / ".123-4.json.tmp").exists()
    svc.held["p3-000000000099.json"] = 1e12
    svc.rejections["p3-000000000099.json"] = {"count": 1, "accepts": 0, "at": 0.0}
    svc.unreadable["p3-000000000099.json"] = 1e12
    svc.unremovable.add("p3-000000000099.json")
    rig.tick(2)
    assert not (svc.held or svc.rejections or svc.unreadable or svc.unremovable)


def test_an_unreadable_queue_file_does_not_hold_up_the_others(tmp_path, server, monkeypatch):
    rig = Rig(tmp_path, closed_port())
    rig.start()
    rig.watcher_event(event="stuck", severity="urgent", text="AMPLIFIER STUCK ENABLED")
    rig.manager_event(event="fault", kind="a", reason="r")
    rig.tick(3)
    first = os.path.join(str(rig.state_dir / "alerts-queue"), "p5-000000000001.json")
    import builtins
    real_open = builtins.open

    def eio(path, *a, **k):
        if str(path) == first:
            raise OSError(5, "Input/output error")
        return real_open(path, *a, **k)
    monkeypatch.setattr(builtins, "open", eio)
    rig.service.ntfy["server"] = server.url
    rig.service.next_send_at = 0
    status = rig.tick(5)
    assert len(server.bodies()) == 2 and status["unreadable"] == ["p5-000000000001"]


def test_a_restart_is_only_judged_from_events_written_in_this_boot(tmp_path, server):
    rig = Rig(tmp_path, server.url)
    rig.start()
    rig.tick(2)
    rig.clock.up = 37.1                     # the end of the previous boot: no "stop" (a hard cut)
    rig.watcher_event(event="ok", severity="info", text="the radar may run again")
    rig.stop()
    rig.reboot(BOOT_B, uptime=40.0)         # read in the next boot, at about the same uptime
    rig.watcher_event(event="start", severity="info", text="safety watcher started")
    rig.tick(3)
    assert [b["title"] for b in without_summary(server)] == []


def test_a_trip_that_ends_in_the_next_boot_still_gets_its_over(tmp_path, server):
    rig = Rig(tmp_path, server.url)
    rig.start()
    rig.tick(2)
    rig.watcher_event(event="trip", severity="urgent", condition="amp_temp", text="amplifier temperature 56.0 C")
    rig.tick(2)
    rig.reboot(BOOT_B, uptime=20.0)
    rig.watcher_event(event="clear", severity="info", condition="amp_temp", text="below 50 C for 60 s")
    rig.tick(3)
    assert "test-unit: safety trip over: amplifier temperature" in titles(server)


def test_held_back_text_is_clipped_so_the_state_stays_small(tmp_path, server):
    rig = Rig(tmp_path, server.url)
    rig.start()
    rig.tick(2)
    for i in range(40):
        for _ in range(5):
            rig.manager_event(event="fault", kind=f"k{i}", reason="R" * 60000)
        rig.tick()
    assert os.path.getsize(rig.state_dir / "alerts-state.json") < 200_000
    rig.stop()
    rig.start()
    assert rig.service.state_note == ""


def test_leftover_temporary_state_files_are_removed(tmp_path, server):
    rig = Rig(tmp_path, server.url)
    (rig.state_dir / ".alerts-state.json.4242.tmp").write_text("{")
    rig.start()
    assert not (rig.state_dir / ".alerts-state.json.4242.tmp").exists()


def test_a_topic_that_is_part_of_the_host_name_does_not_spoil_the_titles(tmp_path, server):
    rig = Rig(tmp_path, server.url)
    rig.changes["unit_name"] = "radar-1"
    rig.start()
    rig.service.ntfy["topic"] = "radar"
    rig.service.unit = "radar-1"
    rig.tick(3)
    assert server.bodies()[0]["title"] == "radar-1: boot 42 summary"


@pytest.mark.parametrize("cause, health, title", [
    ("gpsd_unidentified", "error", "GPS receiver sends no position (not identified by gpsd)"),
    ("no_position_messages", "error", "GPS receiver sends no position messages"),
    ("no_fix", "ok", "GPS: no fix (antenna or sky?)"),
    ("", "missing", "GPS: no receiver, or gpsd not answering"),
])
def test_gps_causes_from_telemetry(tmp_path, server, cause, health, title):
    rig = Rig(tmp_path, server.url, **{"conditions.gps_s": 30.0})
    rig.start()
    rig.telemetry["sources"]["gps"].update(health=health, reason=f"the reason for {cause or health}",
                                           no_position=cause, receiver_identified=cause != "gpsd_unidentified",
                                           no_position_s=700.0, receiver_check={"runs": 3})
    rig.tick(40)
    [gps] = [b for b in without_summary(server)]
    assert gps["title"] == f"test-unit: {title}" and gps["priority"] == L.DEFAULT
    assert f"the reason for {cause or health}" in gps["message"]


@pytest.mark.parametrize("bad, good", [(L.HIGH, L.HIGH), (L.HIGH, L.DEFAULT)])
def test_an_alert_refused_for_a_long_time_does_not_hold_up_a_new_one(tmp_path, server, bad, good):
    rig = Rig(tmp_path, server.url, **{"ntfy.retry_max_s": 300.0})
    server.refuse = lambda body: "BAD" in body["message"]
    rig.start()
    rig.tick(3)                                 # the summary goes out
    rig.manager_event(event="fault", kind="bad", reason="BAD")
    rig.tick(1800)                              # refused, alone in the queue, for 30 minutes
    if good == L.HIGH:
        rig.manager_event(event="fault", kind="good", reason="fine")
    else:
        rig.watcher_event(event="warning", severity="warning", warning="good", text="good")   # priority 3
    rig.tick(5)
    new = [b for b in server.bodies() if "good" in b["title"] or "good" in b["message"]]
    assert len(new) == 1 and new[0]["priority"] == good


def test_refusals_while_the_server_refused_everything_do_not_count_towards_dropping(tmp_path, server):
    rig = Rig(tmp_path, server.url, **{"ntfy.retry_max_s": 8.0})
    server.mode, server.status = "status", 400
    rig.start()
    rig.manager_event(event="fault", kind="x", reason="BAD")
    rig.tick(400)                               # every message refused, many times each
    server.mode = "ok"
    server.refuse = lambda body: "BAD" in body["message"]      # from now on only that one
    status = rig.tick(30)
    assert any("summary" in b["title"] for b in server.bodies())
    assert status["counters"]["rejected"] == 0                  # one later 400 drops nothing
    assert any(n.startswith("p4-") for n in rig.queued())


def test_sends_stuck_in_dns_lookups_for_ten_minutes_make_the_service_exit_for_a_restart(tmp_path, server):
    import threading
    from radar_runner.alerts import service as alerts_service
    rig = Rig(tmp_path, server.url)
    svc = rig.start()
    rig.tick(3)

    class Stuck:                                # a send whose DNS lookup never returns
        done = threading.Event()
        result = None

        def cancel(self):
            pass
    svc.stale = [{"send": Stuck(), "name": f"p3-00000000000{i}.json", "item": {}, "since": 0.0} for i in range(2)]
    rig.manager_event(event="fault", kind="a", reason="r")
    rig.tick(int(alerts_service.STALE_EXIT_S) - 5)
    assert not svc._stop.is_set() and not [b for b in server.bodies() if "FAULT" in b["title"]]
    rig.tick(10)
    assert svc._stop.is_set() and svc.exit_status == 1
    assert any(n.startswith("p4-") for n in rig.queued())      # the alert waits on disk for the next process
