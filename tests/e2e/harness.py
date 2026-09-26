"""End-to-end Home Assistant harness for renault-mqtt's MQTT discovery.

Brings up Mosquitto and a fresh Home Assistant in Docker, onboards HA, adds the MQTT integration,
then replays the core's REAL discovery output (see core_replay) and reads the outcome back from
HA's registries over the websocket API. Scenarios and what each can and cannot show are in
README.md. Run it through `just e2e`; it is deliberately not part of `just ci`.
"""
import argparse
import json
import os
import re
import signal
import sys
import time
import traceback
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import core_replay as cr  # noqa: E402
import stack  # noqa: E402
from ha_client import HA  # noqa: E402

AREA = "Garage"
# How long an expected change is given before it counts as "did not happen".
SETTLE = 60
# (d2): how long, AFTER a discovery control has proved HA is processing retained configs again,
# we keep watching for the tombstoned entries to disappear.
D2_WATCH = 180
# (d3): how long an online re-sent tombstone is given to remove an orphan, once the same-moment
# sibling control has shown that an online tombstone is being acted on.
D3_WATCH = 90
# (d3), (d4): the device name suffix for each variant of the offline-tombstone restart.
OFFLINE = {"d3": "E2E D3 Car", "d4i": "E2E D4I Car", "d4w": "E2E D4W Car", "d4m": "E2E D4M Car",
           "d4u": "E2E D4U Car"}
# The one key each domain cannot be configured without; (d4m) resurrects with only these.
REQUIRED_TOPIC = {"sensor": "state_topic", "binary_sensor": "state_topic", "button": "command_topic",
                  "number": "command_topic"}

# One old/new entity per class for the rename scenarios, with identical friendly names.
E_OLD = {"sensor": ("SENSORS", "e2e_old_battery"), "binary_sensor": ("BINARY_SENSORS", "e2e_old_plug"),
         "button": ("ACTION_BUTTONS", "e2e_old_wake"), "number": ("NUMBERS", "e2e_old_soc")}
E_NEW = {"sensor": "e2e_new_battery", "binary_sensor": "e2e_new_plug", "button": "e2e_new_wake",
         "number": "e2e_new_soc"}
E_ROW = {"sensor": ("Battery", "battery", "%", "measurement"), "binary_sensor": ("Plug", "plug"),
         "button": ("Wake", "mdi:bell", "actions/wake"), "number": ("SoC Min", "mdi:battery", 20, 80, 5)}
RETIRED_ATTR = {"sensor": "RETIRED_SENSORS", "binary_sensor": "RETIRED_BINARY_SENSORS",
                "button": "RETIRED_BUTTONS", "number": "RETIRED_NUMBERS"}
BASE_UID = {"sensor": "e2e_battery", "binary_sensor": "e2e_plug", "button": "e2e_wake", "number": "e2e_soc_min"}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Run:
    def __init__(self, args, core):
        self.args = args
        self.core = core
        self.caps = cr.capabilities(core)
        self.ha = HA(args.http_port)
        self.broker = None
        self.rows = []
        self.notes = []
        self.later = []

    # ------------------------------------------------------------------ helpers
    def row(self, scenario, cls, result, ids, could_have_been, evidence, **extra):
        r = {"scenario": scenario, "class": cls, "result": result, "ids": ids,
             "could_have_been": could_have_been, "evidence": evidence, **extra}
        self.rows.append(r)
        log(f"  {scenario:<14} {cls:<14} {result:<14} {json.dumps(ids)[:400]}")

    def publish(self, records, present=(), absent=(), timeout=SETTLE):
        self.broker.replay(records)
        return self.ha.wait_registry(present, absent, timeout=timeout)

    def register(self, cat, sfx, uids, supported=None):
        recs = cr.isolate(cr.record(self.core, cat, supported), sfx)
        ok, snap = self.publish(recs, present=uids)
        if not ok:
            raise RuntimeError(f"{sfx}: entities never registered: {[u for u in uids if u not in snap]}")
        return recs, snap

    def to_area(self, sfx):
        dev = self.ha.device_by_identifier(f"{cr.SYNTH_NODE}_{sfx}")
        if dev is None:
            raise RuntimeError(f"{sfx}: device not found")
        self.ha.update_device(dev["id"], area_id=self.area_id)
        # Positive read-back: the area write must be visible before anything depends on it.
        if self.ha.device_by_identifier(f"{cr.SYNTH_NODE}_{sfx}").get("area_id") != self.area_id:
            raise RuntimeError(f"{sfx}: area assignment did not stick")
        return dev

    def u(self, obj, sfx):
        return f"{obj}_{sfx}"

    # ------------------------------------------------------------------ stack
    def up(self):
        a = self.args
        log(f"stack up: HA {a.ha_version}, http 127.0.0.1:{a.http_port}, mqtt 127.0.0.1:{a.mqtt_port}")
        stack.up(a.ha_version, a.http_port, a.mqtt_port)
        if "d5" in a.scenarios:
            for attempt in range(20):      # Mosquitto may not be listening the instant it starts
                try:
                    self.watch_status()
                    break
                except OSError:
                    if attempt == 19:
                        raise
                    time.sleep(0.5)
        self.ha.wait_http()
        self.ha.onboard()
        flow = self.ha.add_mqtt(stack.BROKER)
        self.notes.append(f"MQTT flow: {flow['trail']}")
        self.t_mqtt_added = time.monotonic()
        self.ha.wait_mqtt_loaded()
        self.t_mqtt_loaded_first = time.monotonic()
        if getattr(self, "watcher", None) is not None:
            # HA holds the birth until discovery has been quiet for 5 s (client.py
            # _discovery_cooldown), so publishing scenarios straight away would defer it past
            # the restart and (d5) would report no first birth when there was simply no pause.
            self.first_birth = self.watcher.wait_for("homeassistant/status", "online", self.t_setup, 60)
        cfg = self.ha.get_config()
        self.ha_version = cfg["version"]
        log(f"HA get_config version: {self.ha_version}; websocket ha_version: {self.ha.ha_version}")
        self.area_id = self.ha.ensure_area(AREA)
        self.broker = cr.Broker(a.mqtt_port)

    def watch_status(self):
        """(d5) Subscribe to HA's birth topic before HA's MQTT client exists, so the first birth is
        seen too. Runs before up() adds the MQTT integration."""
        self.watcher = cr.Watcher(self.args.mqtt_port, ["homeassistant/status"])
        self.watch_probe = self.watcher.probe()
        self.t_setup = time.monotonic()

    # ------------------------------------------------------------------ (a)
    def scenario_a(self):
        log("(a) same unique_id, new entity name")
        cat = cr.synth_catalog("E2E A Car")
        uids = [self.u(BASE_UID[c], "a") for c in cr.CLASSES]
        _, before = self.register(cat, "a", uids)
        renamed = cr.synth_catalog(
            "E2E A Car",
            sensors={"e2e_battery": ("Battery Charge", "battery", "%", "measurement"),
                     "e2e_range": ("Range", "distance", "km", "measurement")},
            binary={"e2e_plug": ("Plug Connected", "plug")},
            buttons={"e2e_wake": ("Wake Car", "mdi:bell", "actions/wake")},
            numbers={"e2e_soc_min": ("Minimum Charge", "mdi:battery", 20, 80, 5)})
        recs = cr.isolate(cr.record(self.core, renamed), "a")
        self.broker.replay(recs)
        new_names = {"sensor": "Battery Charge", "binary_sensor": "Plug Connected", "button": "Wake Car",
                     "number": "Minimum Charge"}
        deadline = time.monotonic() + SETTLE
        while True:
            snap = self.ha.entities_by_uid()
            landed = all(snap[self.u(BASE_UID[c], "a")]["original_name"] == new_names[c] for c in cr.CLASSES)
            if landed or time.monotonic() > deadline:
                break
            time.sleep(0.5)
        confs = cr.configs(recs)
        for c in cr.CLASSES:
            uid = self.u(BASE_UID[c], "a")
            b, a = before[uid], snap[uid]
            name_landed = a["original_name"] == new_names[c]
            dei = confs[uid][2].get("default_entity_id")
            result = ("NO-EVIDENCE" if not name_landed else
                      "KEPT" if a["entity_id"] == b["entity_id"] else "CHANGED")
            self.row("a", c, result, {"before": b["entity_id"], "after": a["entity_id"],
                                      "original_name_after": a["original_name"],
                                      "republished_default_entity_id": dei},
                     f"CHANGED to an id derived from '{new_names[c]}'",
                     "control: registry original_name changed to the new name, so the republish "
                     "was processed" if name_landed else "rename never reached the registry")

    # ------------------------------------------------------------------ (b), (c)
    def _new_entities_in_area(self, sfx, device, keep_dei):
        cat = cr.synth_catalog(device)
        uids = [self.u(BASE_UID[c], sfx) for c in cr.CLASSES]
        _, before = self.register(cat, sfx, uids)
        self.to_area(sfx)
        extra = cr.synth_catalog(
            device,
            sensors={**cat.SENSORS, "e2e_extra": ("Extra", None, None, None)},
            binary={**cat.BINARY_SENSORS, "e2e_extra_flag": ("Extra Flag", None)},
            buttons={**cat.ACTION_BUTTONS, "e2e_extra_press": ("Extra Press", "mdi:bell", "actions/wake")},
            numbers={**cat.NUMBERS, "e2e_extra_level": ("Extra Level", "mdi:battery", 20, 80, 5)})
        new = {"sensor": "e2e_extra", "binary_sensor": "e2e_extra_flag", "button": "e2e_extra_press",
               "number": "e2e_extra_level"}
        recs = cr.isolate(cr.record(self.core, extra), sfx)
        new_uids = [self.u(new[c], sfx) for c in cr.CLASSES]
        if not keep_dei:
            recs = cr.strip_default_entity_id(recs, set(new_uids))
        ok, snap = self.publish(recs, present=new_uids)
        return before, snap, new, cr.configs(recs), ok

    def scenario_b(self):
        log("(b) new unique_id WITHOUT default_entity_id, device already in area")
        before, snap, new, confs, ok = self._new_entities_in_area("b", "E2E B Car", keep_dei=False)
        for c in cr.CLASSES:
            uid = self.u(new[c], "b")
            pre = before[self.u(BASE_UID[c], "b")]["entity_id"]
            eid = snap.get(uid, {}).get("entity_id")
            area_prefixed = bool(eid) and eid.split(".", 1)[1].startswith("garage_")
            self.row("b", c, "MISSING" if not eid else "AREA-PREFIXED" if area_prefixed else "NOT-PREFIXED",
                     {"new": eid, "pre_area_sibling": pre,
                      "default_entity_id_in_payload": "default_entity_id" in confs[uid][2]},
                     "an id without the garage_ prefix (as the pre-area sibling has)",
                     "control: the sibling registered before the area assignment has no prefix")

    def scenario_c(self):
        if not self.caps["default_entity_id"]:
            self.row("c", "all", "N/A", {}, "", "core under test does not emit default_entity_id")
            return
        log("(c) new unique_id WITH default_entity_id, device already in area")
        _, snap, new, confs, ok = self._new_entities_in_area("c", "E2E C Car", keep_dei=True)
        for c in cr.CLASSES:
            uid = self.u(new[c], "c")
            dei = confs[uid][2].get("default_entity_id")
            eid = snap.get(uid, {}).get("entity_id")
            self.row("c", c, "MISSING" if not eid else "EXACT" if eid == dei else "DIFFERENT",
                     {"new": eid, "default_entity_id": dei},
                     "garage_-prefixed (as (b) shows without it) or a _2 suffix",
                     "control: scenario (b) is the same sequence without default_entity_id")

    # ------------------------------------------------------------------ (g)
    def scenario_g(self):
        if not self.caps["default_entity_id"]:
            self.row("g", "all", "N/A", {}, "", "core under test does not emit default_entity_id")
            return
        log("(g) area-prefixed entity re-discovered WITH default_entity_id")
        before, snap, new, confs, ok = self._new_entities_in_area("g", "E2E G Car", keep_dei=False)
        garage = {c: snap[self.u(new[c], "g")]["entity_id"] for c in cr.CLASSES}
        extra = cr.synth_catalog(
            "E2E G Car",
            sensors={"e2e_battery": ("Battery", "battery", "%", "measurement"),
                     "e2e_range": ("Range", "distance", "km", "measurement"),
                     "e2e_extra": ("Extra", None, None, None)},
            binary={"e2e_plug": ("Plug", "plug"), "e2e_extra_flag": ("Extra Flag", None)},
            buttons={"e2e_wake": ("Wake", "mdi:bell", "actions/wake"),
                     "e2e_extra_press": ("Extra Press", "mdi:bell", "actions/wake")},
            numbers={"e2e_soc_min": ("SoC Min", "mdi:battery", 20, 80, 5),
                     "e2e_extra_level": ("Extra Level", "mdi:battery", 20, 80, 5)})
        recs = cr.isolate(cr.record(self.core, extra), "g")
        since = time.time() - 1
        self.broker.replay(recs)
        time.sleep(15)
        after = self.ha.entities_by_uid()
        logs = stack.ha_logs(since)
        full = cr.configs(recs)
        for c in cr.CLASSES:
            uid = self.u(new[c], "g")
            eid = after[uid]["entity_id"]
            processed = f"Updating component: {garage[c]}" in logs
            self.row("g", c, ("NO-EVIDENCE" if not processed else
                              "LEFT" if eid == garage[c] else "RENAMED"),
                     {"before": garage[c], "after_15s": eid, "default_entity_id": full[uid][2].get("default_entity_id")},
                     "renamed to default_entity_id",
                     "control: HA logged 'Updating component: <id>' for this entity, so the payload "
                     "that added default_entity_id was processed" if processed else
                     "HA never logged the update, so the result is not evidence")
            self.later.append(("g-after-restart", c, uid, garage[c], full[uid][2].get("default_entity_id")))

    # ------------------------------------------------------------------ tombstones
    def tombstone_records(self, cat, sfx, classes, v1_recs):
        """Produce, via the core wherever it can, the discovery pass that removes `classes`.
        Returns (records, {class: how the tombstone was produced})."""
        v2 = cr.synth_catalog(cat.DEVICE["name"], sensors=dict(cat.SENSORS), binary=dict(cat.BINARY_SENSORS),
                              buttons=dict(cat.ACTION_BUTTONS), numbers=dict(cat.NUMBERS))
        supported = cr.synth_endpoints(cat)
        source = {}
        harness = []
        for c in classes:
            obj = BASE_UID[c]
            table = {"sensor": "SENSORS", "binary_sensor": "BINARY_SENSORS", "button": "ACTION_BUTTONS",
                     "number": "NUMBERS"}[c]
            if c == "sensor" or self.caps["retire"][c]:
                getattr(v2, table).pop(obj)
                setattr(v2, RETIRED_ATTR[c], [*getattr(v2, RETIRED_ATTR[c], []), obj])
                source[c] = f"core {RETIRED_ATTR[c]}"
            elif c == "button":
                supported = supported - {cat.ACTION_BUTTONS[obj][2]}
                source[c] = "core: endpoint unsupported"
            elif c == "number":
                supported = supported - {cat.SOC_ENDPOINT}
                source[c] = "core: SOC endpoint unsupported"
            else:
                # Drop it from the pass too. Left in, the core re-sends its CONFIG ahead of this
                # tombstone, which is a resurrect-then-tombstone and not a tombstone at all: that
                # made (d3)'s binary_sensor look removed by the tombstone alone.
                getattr(v2, table).pop(obj)
                topic = cr.topic_of(v1_recs, self.u(obj, sfx))
                harness.append({"topic": topic, "payload": "", "retain": True, "qos": 0,
                                "edited": "HARNESS tombstone: core has no retirement for this class"})
                source[c] = "HARNESS empty retained payload on the core's own topic"
        recs = cr.isolate(cr.record(self.core, v2, supported), sfx) + harness
        return recs, source

    def scenario_d1(self):
        log("(d1) HA online: empty retained payload removes the entry, per domain")
        cat = cr.synth_catalog("E2E D1 Car")
        uids = {c: self.u(BASE_UID[c], "d1") for c in cr.CLASSES}
        v1, before = self.register(cat, "d1", list(uids.values()))
        recs, source = self.tombstone_records(cat, "d1", cr.CLASSES, v1)
        keep = self.u("e2e_range", "d1")
        since = time.time() - 1
        ok, snap = self.publish(recs, absent=list(uids.values()))
        logs = stack.ha_logs(since)
        for c in cr.CLASSES:
            gone = uids[c] not in snap
            self.row("d1", c, "REMOVED" if gone else "NOT-REMOVED",
                     {"entity_id": before[uids[c]]["entity_id"], "tombstone_source": source[c],
                      "ha_logged_removal": f"Removing component: {before[uids[c]]['entity_id']}" in logs},
                     "still registered after the tombstone",
                     f"control: untouched sibling {keep} still registered: {keep in snap}")
        self.d1_order = cr.describe(recs)

    def d2_prepare(self):
        """Register now; the offline tombstone happens at the end, because it restarts HA."""
        log("(d2) registering entities for the offline-tombstone run")
        cat = cr.synth_catalog("E2E D2 Car")
        self.d2_uids = {c: self.u(BASE_UID[c], "d2") for c in cr.CLASSES}
        self.d2_v1, self.d2_before = self.register(cat, "d2", list(self.d2_uids.values()))
        self.d2_cat = cat

    def scenario_d2(self):
        log("(d2) HA OFFLINE when the tombstone is published")
        cat = self.d2_cat
        recs, source = self.tombstone_records(cat, "d2", cr.CLASSES, self.d2_v1)
        # Positive control that HA processes retained discovery after the restart: a NEW entity,
        # produced by the core, published in the same offline window.
        late_cat = cr.synth_catalog(cat.DEVICE["name"], sensors={**cat.SENSORS, "e2e_late": ("Late", None, None, None)})
        late = [r for r in cr.isolate(cr.record(self.core, late_cat), "d2") if "/e2e_late/" in r["topic"]]
        late_uid = self.u("e2e_late", "d2")
        # Registry must be persisted with the entries present before we stop.
        pre = self.ha.entities_by_uid()
        assert all(u in pre for u in self.d2_uids.values())
        stack.stop_ha()
        self.ha.ws_close()
        self.broker.replay(recs + late)
        retained = self.broker.retained(f"homeassistant/+/{cr.SYNTH_NODE}_d2/+/config")
        topics = {c: cr.topic_of(self.d2_v1, self.d2_uids[c]) for c in cr.CLASSES}
        late_topic = late[0]["topic"]
        t0 = time.monotonic()
        stack.start_ha()
        self.ha.wait_api()
        self.ha.wait_mqtt_loaded()
        ok_ctl, snap = self.ha.wait_registry(present=[late_uid], timeout=120)
        t_ctl = time.monotonic() - t0
        ok, snap = self.ha.wait_registry(absent=list(self.d2_uids.values()), timeout=D2_WATCH)
        for c in cr.CLASSES:
            uid = self.d2_uids[c]
            self.row("d2", c, "REMOVED" if uid not in snap else "NOT-REMOVED",
                     {"entity_id": self.d2_before[uid]["entity_id"], "tombstone_source": source[c],
                      "broker_retained_after_tombstone": retained.get(topics[c], "<nothing retained>")[:40],
                      "still_registered": snap.get(uid, {}).get("entity_id")},
                     "REMOVED after restart",
                     f"controls: (1) d1 shows this instrument sees removal; (2) new entity {late_uid} "
                     f"published in the same offline window {'appeared' if ok_ctl else 'DID NOT appear'} "
                     f"{t_ctl:.0f}s after start, then watched {D2_WATCH}s more; (3) broker retained on "
                     f"the control topic: {'yes' if late_topic in retained else 'NO'}")

    # ------------------------------------------------------------------ (d3), (d4), (d5)
    # The question: if HA missed a tombstone while it was down, does re-sending discovery when HA
    # announces itself (the birth message, HA's documented trigger) remove the orphan? HA's
    # discovery only acts on an empty payload for a discovery hash it has discovered IN THIS RUN,
    # and after the restart nothing is retained for the orphan's topic, so the doubt is that a
    # re-sent tombstone alone is a no-op. (d4) tests the sequence that would satisfy that rule.
    def offline_prepare(self):
        """Register one device per variant, in the area; tombstone and restart come at the end."""
        log("(d3/d4) registering entities for the offline-tombstone + birth re-publish run")
        self.off = {}
        for sfx, name in OFFLINE.items():
            cat = cr.synth_catalog(name)
            uids = {c: self.u(BASE_UID[c], sfx) for c in cr.CLASSES}
            v1, before = self.register(cat, sfx, [*uids.values(), self.u("e2e_range", sfx)])
            dev = self.to_area(sfx)
            self.off[sfx] = types.SimpleNamespace(cat=cat, uids=uids, v1=v1, before=before, device_id=dev["id"],
                                                  topics={c: cr.topic_of(v1, uids[c]) for c in cr.CLASSES})

    @staticmethod
    def _received_empty(logs, topic):
        """Count HA's own debug lines for a zero-length message on `topic`: proof it arrived."""
        pat = re.compile(r"message on " + re.escape(topic) + r" \(qos=\d\).*: b''")
        return sum(1 for ln in logs.splitlines() if pat.search(ln))

    @staticmethod
    def _found_line(topic):
        domain, node, obj = cr.config_topic_parts(topic)
        return f"Found new component: {domain} {node} {obj}"

    def _ids(self, o, uid, snap, states=None):
        b = o.before[uid]
        ids = {"entity_id": b["entity_id"], "registry_id": b["id"], "device_id": b["device_id"],
               "area_of_device": AREA, "still_registered": (snap.get(uid) or {}).get("entity_id")}
        if states is not None:
            st = states.get(b["entity_id"])
            ids["state_after_restart"] = None if st is None else {
                "state": st["state"], "restored": st["attributes"].get("restored", False)}
        return ids

    def _resurrect_then_tombstone(self, sfx, tombstone_pass, minimal, wait, retain=True):
        """(d4) Re-publish the old config so HA discovers it in this run, then the tombstone pass.
        retain=False sends the resurrect unretained, so a crash between the two publishes cannot
        leave the old config retained for the next subscriber."""
        o = self.off[sfx]
        confs = cr.configs(o.v1)
        resurrect = []
        for c in cr.CLASSES:
            domain, rec, conf = confs[o.uids[c]]
            if minimal:
                conf = {"unique_id": conf["unique_id"], "device": conf["device"],
                        REQUIRED_TOPIC[domain]: conf[REQUIRED_TOPIC[domain]]}
                rec = dict(rec, payload=json.dumps(conf),
                           edited="HARNESS minimal resurrect config: unique_id, device, required topic")
            if not retain:
                rec = dict(rec, retain=False, edited=f"{rec.get('edited', 'HARNESS resurrect')}; UNRETAINED")
            resurrect.append(rec)
        since = time.time() - 1
        t0 = time.monotonic()
        timing = {}
        if wait:
            self.broker.replay(resurrect)
            want = [self._found_line(r["topic"]) for r in resurrect]
            deadline = time.monotonic() + SETTLE
            while True:
                logs = stack.ha_logs(since)
                found = all(line in logs for line in want)
                if found or time.monotonic() > deadline:
                    break
                time.sleep(0.5)
            timing["resurrect_to_all_found_s"] = round(time.monotonic() - t0, 1) if found else None
            mid = self.ha.entities_by_uid()
            timing["entity_id_after_resurrect"] = {c: (mid.get(o.uids[c]) or {}).get("entity_id") for c in cr.CLASSES}
            t1 = time.monotonic()
            self.broker.replay(tombstone_pass)
        else:
            t1 = t0
            self.broker.replay(resurrect + tombstone_pass)
        ok, snap = self.ha.wait_registry(absent=list(o.uids.values()), timeout=SETTLE)
        timing["tombstone_to_removed_s"] = round(time.monotonic() - t1, 1) if ok else None
        logs = stack.ha_logs(since)
        order = cr.describe(resurrect) + ([f"--- waited for 'Found new component' x{len(resurrect)}"] if wait else []) \
            + cr.describe(tombstone_pass)
        return snap, logs, timing, order

    def scenario_offline(self):
        log("(d3/d4/d5) tombstone published while HA is down; re-publish after HA's birth message")
        if getattr(self, "watcher", None) is None:
            self.watcher = cr.Watcher(self.args.mqtt_port, ["homeassistant/status"])
            self.watch_probe = self.watcher.probe()
        w = self.watcher
        passes, sources = {}, {}
        for sfx, o in self.off.items():
            passes[sfx], sources[sfx] = self.tombstone_records(o.cat, sfx, cr.CLASSES, o.v1)
        d3 = self.off["d3"]
        # Positive control that HA processes retained discovery after the restart, as in (d2).
        late_cat = cr.synth_catalog(OFFLINE["d3"], sensors={**d3.cat.SENSORS, "e2e_late": ("Late", None, None, None)})
        late = [r for r in cr.isolate(cr.record(self.core, late_cat), "d3") if "/e2e_late/" in r["topic"]]
        late_uid, late_topic = self.u("e2e_late", "d3"), late[0]["topic"]
        pre = self.ha.entities_by_uid()
        assert all(u in pre for o in self.off.values() for u in o.uids.values())

        t_stop = time.monotonic()
        stack.stop_ha()
        self.ha.ws_close()
        self.broker.replay([r for sfx in self.off for r in passes[sfx]] + late)
        retained = self.broker.retained("homeassistant/#")
        since = time.time() - 1
        t0 = time.monotonic()
        stack.start_ha()
        self.ha.wait_api()
        t_api = time.monotonic()
        self.ha.wait_mqtt_loaded()
        t_mqtt = time.monotonic()
        ok_ctl, snap = self.ha.wait_registry(present=[late_uid], timeout=120)
        t_ctl = time.monotonic()
        birth = w.wait_for("homeassistant/status", "online", after=t0, timeout=180)
        t_birth = birth["t"] if birth else None
        # Read now: (d3/manual) deletes the d3 device, which clears the control's retained config.
        status_retained = self.broker.retained("homeassistant/status")
        ctl_retained = self.broker.retained(late_topic)
        states = self.ha.states()
        snap = self.ha.entities_by_uid()
        self.offline_restart = {"t0": t0, "t_stop": t_stop, "t_api": t_api, "t_mqtt": t_mqtt, "t_ctl": t_ctl,
                                "ok_ctl": ok_ctl, "birth": birth, "since": since, "late_topic": late_topic,
                                "status_retained": status_retained, "ctl_retained": ctl_retained}
        rel = lambda t: None if t is None else round(t - t0, 1)  # noqa: E731
        restart_ctl = (f"control: new entity {late_uid}, published in the same offline window, "
                       f"{'registered' if ok_ctl else 'DID NOT register'} {rel(t_ctl)}s after start; "
                       f"birth 'online' seen {rel(t_birth)}s after start")
        not_retained = {sfx: {c: retained.get(o.topics[c], "<nothing retained>")[:30] for c in cr.CLASSES}
                        for sfx, o in self.off.items()}

        # ---- (d3): the birth-triggered re-publish, i.e. the core's same pass again, HA online.
        # Same-moment control: a sibling HA DID discover in this run (its config stayed retained)
        # is tombstoned in the same replay. It must go, or this run could not see a removal at all.
        ctl_uid = self.u("e2e_range", "d3")
        ctl_topic = cr.topic_of(d3.v1, ctl_uid)
        resend = passes["d3"] + [{"topic": ctl_topic, "payload": "", "retain": True, "qos": 0,
                                  "edited": "HARNESS control tombstone: sibling discovered in this run"}]
        self.d3_order = cr.describe(resend)
        s3 = time.time() - 1
        t3 = time.monotonic()
        self.broker.replay(resend)
        ok_c, snap = self.ha.wait_registry(absent=[ctl_uid], timeout=SETTLE)
        t_c = round(time.monotonic() - t3, 1)
        ok3, snap = self.ha.wait_registry(absent=list(d3.uids.values()), timeout=D3_WATCH)
        logs3 = stack.ha_logs(s3)
        self.row("d3/control", "sensor", "REMOVED" if ctl_uid not in snap else "NOT-REMOVED",
                 {"entity_id": d3.before[ctl_uid]["entity_id"], "registry_id": d3.before[ctl_uid]["id"],
                  "removed_after_s": t_c if ok_c else None,
                  "ha_logged_removal": f"Removing component: {d3.before[ctl_uid]['entity_id']}" in logs3,
                  "ha_received_empty": self._received_empty(logs3, ctl_topic)},
                 "NOT-REMOVED", "this IS the positive control: discovered in this run, tombstoned online")
        for c in cr.CLASSES:
            uid = d3.uids[c]
            self.row("d3", c, "REMOVED" if uid not in snap else "NOT-REMOVED",
                     {**self._ids(d3, uid, snap, states), "tombstone_source": sources["d3"][c],
                      "broker_retained_after_offline_tombstone": not_retained["d3"][c],
                      "ha_received_empty_online": self._received_empty(logs3, d3.topics[c]),
                      "ha_logged_removal": f"Removing component: {d3.before[uid]['entity_id']}" in logs3},
                     "REMOVED, as the same-moment sibling control was",
                     f"{restart_ctl}; sibling control {'removed' if ok_c else 'NOT removed'} {t_c}s after the "
                     f"re-send; orphans then watched {D3_WATCH}s; d1 is the cross-run control")

        # ---- (d4): resurrect the old config first, then the tombstone pass.
        for sfx, minimal, wait, retain, label in (
                ("d4i", False, False, True, "d4/immediate"), ("d4w", False, True, True, "d4/waited"),
                ("d4m", True, True, True, "d4/minimal"), ("d4u", True, False, False, "d4/unretained")):
            o = self.off[sfx]
            snap4, logs4, timing, order = self._resurrect_then_tombstone(sfx, passes[sfx], minimal, wait, retain)
            timing["broker_retained_after"] = {c: self.broker.retained(o.topics[c], wait=1.0).get(o.topics[c],
                                                                                             "<nothing retained>")[:30]
                                               for c in cr.CLASSES}
            setattr(self, f"{sfx}_order", order)
            for c in cr.CLASSES:
                uid = o.uids[c]
                self.row(label, c, "REMOVED" if uid not in snap4 else "NOT-REMOVED",
                         {**self._ids(o, uid, snap4, states), "tombstone_source": sources[sfx][c],
                          "broker_retained_after_offline_tombstone": not_retained[sfx][c],
                          "ha_found_in_this_run": self._found_line(o.topics[c]) in logs4,
                          "ha_logged_removal": f"Removing component: {o.before[uid]['entity_id']}" in logs4,
                          **timing},
                         "NOT-REMOVED, as (d3) shows for a tombstone alone",
                         f"{restart_ctl}; (d3) is the same pass without the resurrect")

        # ---- (d3/manual): what 'status quo + document a manual delete' asks of the user: the
        # device page's Delete. Run only on the d3 device, and only after (d3) has been read.
        leftover = [u for u in d3.uids.values() if u in self.ha.entities_by_uid()]
        if leftover:
            # First the gentler instruction: delete one orphan entity from its settings dialog.
            one = leftover.pop(0)
            eid = d3.before[one]["entity_id"]
            self.ha.ws_call({"type": "config/entity_registry/remove", "entity_id": eid})
            ok_e, snap_e = self.ha.wait_registry(absent=[one], timeout=SETTLE)
            self.row("d3/manual-entity", "all", "REMOVED" if ok_e else "NOT-REMOVED",
                     {"entity_id": eid, "registry_id": d3.before[one]["id"],
                      "siblings_untouched": all(u in snap_e for u in leftover)},
                     "orphan still registered", "config/entity_registry/remove, the entity dialog's Delete")
        if leftover:
            entry_id = d3.before[d3.uids["sensor"]]["config_entry_id"]
            self.ha.remove_device_from_entry(d3.device_id, entry_id)
            ok_m, snap_m = self.ha.wait_registry(absent=leftover, timeout=SETTLE)
            dev_after = self.ha.device_by_identifier(f"{cr.SYNTH_NODE}_d3")
            self.row("d3/manual-device", "all", "REMOVED" if ok_m else "NOT-REMOVED",
                     {"device_id": d3.device_id, "orphans": {u: d3.before[u]["entity_id"] for u in leftover},
                      "device_still_registered": dev_after is not None},
                     "orphans still registered",
                     "config/device_registry/remove_config_entry, the call behind the device page's Delete")
        else:
            self.row("d3/manual", "all", "N/A", {}, "", "no orphan left by (d3) to delete by hand")

        self.report_d5()
        if self.args.out:
            # The raw HA log from the restart on: what every 'ha_*' field above was read from.
            with open(self.args.out + ".ha.log", "w") as fh:
                fh.write(stack.ha_logs(since))

    def report_d5(self):
        """(d5) What HA publishes on homeassistant/status, when, and whether retained."""
        w, r = self.watcher, self.offline_restart
        t0 = r["t0"]
        self.row("d5/probe", "instrument", "OK" if self.watch_probe.get("probe-retained") is True
                 and self.watch_probe.get("probe-plain") is False else "BROKEN",
                 {"seen": self.watch_probe}, "both flags reading the same value",
                 "the watcher reports the publisher's retain flag (MQTT 5 retain-as-published), and "
                 "reports True and False for a retained and a plain publish")
        status = w.on("homeassistant/status")
        first = [m for m in status if m["t"] < r["t_stop"]]
        base = getattr(self, "t_mqtt_added", None)
        self.row("d5/first-setup", "status", "SEEN" if first else "NONE",
                 {"messages": [{"payload": m["payload"], "retain": m["retain"],
                                "s_after_mqtt_flow_created": None if base is None else round(m["t"] - base, 1)}
                               for m in first]},
                 "no message", "watcher subscribed before the MQTT integration was added"
                 if hasattr(self, "t_setup") else "watcher started late: first setup not observed")
        stop = [m for m in status if r["t_stop"] <= m["t"] < t0]
        self.row("d5/stop", "status", "SEEN" if stop else "NONE",
                 {"messages": [{"payload": m["payload"], "retain": m["retain"],
                                "s_after_stop_requested": round(m["t"] - r["t_stop"], 1)} for m in stop]},
                 "no message", "docker stop -t 60 (clean shutdown)")
        after = [m for m in status if m["t"] >= t0]
        logs = stack.ha_logs(r["since"])
        self.row("d5/restart", "status", "SEEN" if after else "NONE",
                 {"messages": [{"payload": m["payload"], "retain": m["retain"],
                                "s_after_start": round(m["t"] - t0, 1),
                                "s_after_api_up": round(m["t"] - r["t_api"], 1),
                                "s_after_mqtt_loaded": round(m["t"] - r["t_mqtt"], 1),
                                "s_after_retained_control_registered": round(m["t"] - r["t_ctl"], 1)}
                               for m in after],
                  "ha_logged_birth_sent": "MQTT client initialized, birth message sent" in logs},
                 "no birth, or a birth before retained discovery was processed",
                 "negative s_after_retained_control_registered would mean the birth came first")
        snap, ctl = r["status_retained"], r["ctl_retained"]
        self.row("d5/retained", "status", "RETAINED" if "homeassistant/status" in snap else "NOT-RETAINED",
                 {"fresh_subscriber_got": snap},
                 "RETAINED",
                 f"control: the same fresh-subscriber read of a retained config topic "
                 f"{'returned it' if r['late_topic'] in ctl else 'RETURNED NOTHING'}")

    # ------------------------------------------------------------------ (e)
    def scenario_e(self, variant, sfx):
        classes = [c for c in cr.CLASSES if c == "sensor" or self.caps["retire"][c]]
        skipped = [c for c in cr.CLASSES if c not in classes]
        log(f"(e) rename with unchanged friendly name, variant={variant}, classes={classes}")
        dev = f"E2E {sfx.upper()} Car"

        def cat_with(objs_by_class, **retired):
            tables = {"SENSORS": {}, "BINARY_SENSORS": {}, "ACTION_BUTTONS": {}, "NUMBERS": {}}
            for c, obj in objs_by_class.items():
                tables[E_OLD[c][0]][obj] = E_ROW[c]
            return cr.synth_catalog(dev, sensors=tables["SENSORS"], binary=tables["BINARY_SENSORS"],
                                    buttons=tables["ACTION_BUTTONS"], numbers=tables["NUMBERS"], **retired)

        v1 = cat_with({c: E_OLD[c][1] for c in classes})
        old_uids = {c: self.u(E_OLD[c][1], sfx) for c in classes}
        _, before = self.register(v1, sfx, list(old_uids.values()))
        x = {c: before[old_uids[c]]["entity_id"] for c in classes}
        self.to_area(sfx)
        retired = {RETIRED_ATTR[c]: [E_OLD[c][1]] for c in classes}
        v2 = cat_with({c: E_NEW[c] for c in classes}, **retired)
        recs = cr.isolate(cr.record(self.core, v2), sfx)
        new_uids = {c: self.u(E_NEW[c], sfx) for c in classes}
        confs = cr.configs(recs)
        dei_source = {}
        for c in classes:
            core_dei = confs[new_uids[c]][2].get("default_entity_id")
            if core_dei is None:
                recs = cr.edit_config(recs, new_uids[c], lambda conf, v=x[c]: conf.__setitem__("default_entity_id", v),
                                      "INJECTED default_entity_id = X (core has none)")
                dei_source[c] = "injected (simulated: core has no default_entity_id)"
            else:
                dei_source[c] = f"core ({core_dei}; {'== X' if core_dei == x[c] else '!= X'})"
        dead = {r["topic"] for r in cr.tombstones(recs)}
        v1_recs = cr.isolate(cr.record(self.core, v1), sfx)
        old_topics = {cr.topic_of(v1_recs, u) for u in old_uids.values()}
        if not old_topics <= dead:
            raise RuntimeError(f"(e) core did not tombstone {sorted(old_topics - dead)}")
        if variant == "control":
            # Same pass without the tombstones: the new unique_id asks for X while X is still held.
            # Must produce X_2, or this instrument could not detect a collision at all.
            passes = [[r for r in recs if r["topic"] not in old_topics]]
        elif variant == "one-pass":
            passes = [recs]
        else:
            passes = [[r for r in recs if r["topic"] in old_topics],
                      [r for r in recs if r["topic"] not in old_topics]]
        order = []
        for i, p in enumerate(passes):
            order.append(f"--- pass {i + 1}")
            order += cr.describe(p)
            if variant == "two-pass" and i == 0:
                ok, _ = self.publish(p, absent=list(old_uids.values()))
                order.append(f"--- waited: old entries gone from registry = {ok}")
                if not ok:
                    raise RuntimeError("(e) two-pass: tombstone never removed the old entries")
            else:
                absent = [] if variant == "control" else list(old_uids.values())
                ok, snap = self.publish(p, present=list(new_uids.values()), absent=absent)
        for c in classes:
            eid = snap.get(new_uids[c], {}).get("entity_id")
            if eid is None:
                result = "MISSING"
            elif eid == x[c]:
                result = "GOT-X"
            elif eid.startswith(x[c] + "_"):
                result = "GOT-X_n"
            else:
                result = "OTHER"
            self.row(f"e/{variant}", c, result,
                     {"X_old": x[c], "new": eid, "old_still_registered": old_uids[c] in snap,
                      "default_entity_id_source": dei_source[c]},
                     "X_2 (collision) or X" if variant != "control" else "X (no collision)",
                     "collision control is e/control" if variant != "control" else
                     "this IS the control: X still held by the old unique_id")
        for c in skipped:
            self.row(f"e/{variant}", c, "N/A", {}, "", f"core under test has no {RETIRED_ATTR[c]}")
        setattr(self, f"e_order_{variant}", order)

    # ------------------------------------------------------------------ (f)
    def scenario_f(self):
        for spec in self.args.catalog:
            cat = cr.load_catalog(spec)
            env = {cat.ENV_PREFIX + "PUBLISH_LOCATION": "true", cat.ENV_PREFIX + "ENABLE_REFRESH_LOCATION": "true"}
            recs = cr.record(self.core, cat, env=env)
            confs = cr.configs(recs)
            expected = {uid: conf.get("default_entity_id") for uid, (_, _, conf) in confs.items()}
            log(f"(f) plain install, real catalog {cat.NODE}: {len(confs)} entities, "
                f"default_entity_id in payload: {any(expected.values())}")
            recs = cr.strip_default_entity_id(recs)
            ok, snap = self.publish(recs, present=list(confs))
            derived = {uid: snap.get(uid, {}).get("entity_id") for uid in confs}
            mismatches = {uid: {"ha_derived": derived[uid], "core_default_entity_id": expected[uid]}
                          for uid in confs if expected[uid] is not None and derived[uid] != expected[uid]}
            self.row(f"f/{cat.NODE}", "all",
                     "BASELINE" if not any(expected.values()) else ("MATCH" if not mismatches else "MISMATCH"),
                     {"count": len(confs), "registered": sum(1 for v in derived.values() if v),
                      "mismatches": mismatches, "ids": dict(sorted(derived.items()))},
                     "a mismatch between HA's own derivation and the core's slug",
                     "default_entity_id was REMOVED before publishing, so HA derived every id itself; "
                     "comparing that to the core's value can fail, unlike reading it back")

    # ------------------------------------------------------------------ after restart
    def after_restart(self):
        snap = self.ha.entities_by_uid()
        for scen, c, uid, before, dei in self.later:
            eid = snap.get(uid, {}).get("entity_id")
            self.row(scen, c, "LEFT" if eid == before else "RENAMED" if eid == dei else "OTHER",
                     {"before": before, "after_restart": eid, "default_entity_id": dei},
                     "renamed to default_entity_id on restart",
                     "HA was restarted by (d2); entities re-initialise from retained discovery")

    def run(self):
        wanted = self.args.scenarios
        self.up()
        steps = [("a", self.scenario_a), ("b", self.scenario_b), ("c", self.scenario_c),
                 ("g", self.scenario_g), ("d1", self.scenario_d1), ("d2", self.d2_prepare),
                 ("offline", self.offline_prepare),
                 ("e", lambda: self.scenario_e("control", "e0")),
                 # The one-pass variant is a race between a tombstone and a config microseconds
                 # apart, so a single trial could pass by luck; --e-repeats runs it on fresh devices.
                 *[("e", lambda n=n: self.scenario_e("one-pass", "e1" if n == 0 else f"e1r{n}"))
                   for n in range(self.args.e_repeats)],
                 ("e", lambda: self.scenario_e("two-pass", "e2")),
                 ("f", self.scenario_f), ("d2", self.scenario_d2), ("offline", self.scenario_offline)]
        # d3, d4 and d5 share one HA restart, so selecting any of them runs all three.
        offline = bool(wanted & {"d3", "d4", "d5"})
        for name, fn in steps:
            if name not in wanted and not (name == "offline" and offline):
                continue
            try:
                fn()
            except Exception as err:     # one broken scenario must not hide the others' results
                self.row(name, "all", "ERROR", {}, "", f"{type(err).__name__}: {err}")
                traceback.print_exc()
        if "d2" in wanted and self.later:
            try:
                self.after_restart()
            except Exception as err:
                self.row("after-restart", "all", "ERROR", {}, "", f"{type(err).__name__}: {err}")


# What a sound run of the current core gives. Anything else fails the run: a result string is only
# evidence if something reads it. d2 pins the known offline-tombstone gap; flip it with the fix.
EXPECTED = {"a": {"KEPT"}, "b": {"AREA-PREFIXED"}, "c": {"EXACT"}, "d1": {"REMOVED"},
            "d2": {"NOT-REMOVED"}, "e/control": {"GOT-X_n"}, "e/one-pass": {"GOT-X"},
            "e/two-pass": {"GOT-X"}, "f": {"BASELINE", "MATCH"}, "g": {"LEFT"},
            "g-after-restart": {"LEFT"}}


def unexpected(rows):
    """Rows whose result is neither the expected one nor N/A (a scenario the core cannot express)."""
    def key(scenario):
        parts = scenario.split("/")
        return "/".join(parts[:2]) if parts[0] == "e" else parts[0]
    return [r for r in rows if r["result"] != "N/A" and r["result"] not in EXPECTED.get(key(r["scenario"]), ())]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ha-version", required=True)
    p.add_argument("--core", required=True, help="path to a renault-mqtt checkout to import the core from")
    p.add_argument("--catalog", action="append", default=[],
                   help="real catalog for (f) as SYS_PATH_ENTRY:MODULE; repeatable")
    p.add_argument("--scenarios", default="a,b,c,g,d1,d2,d3,d4,d5,e,f")
    p.add_argument("--e-repeats", type=int, default=1, help="trials of the (e) one-pass variant")
    p.add_argument("--http-port", type=int, default=18131)
    p.add_argument("--mqtt-port", type=int, default=18831)
    p.add_argument("--out", help="write the full JSON result here")
    p.add_argument("--keep", action="store_true", help="leave the stack running (debugging only)")
    args = p.parse_args()
    args.scenarios = set(args.scenarios.split(","))

    core = cr.load_core(args.core)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    run = Run(args, core)
    log(f"core {core.path} version {core.version}; capabilities {run.caps}")
    try:
        run.run()
    finally:
        if run.broker:
            run.broker.close()
        if getattr(run, "watcher", None):
            run.watcher.close()
        run.ha.ws_close()
        if not args.keep:
            stack.teardown()
            log("stack torn down")
    result = {"ha_version_requested": args.ha_version, "ha_version_running": getattr(run, "ha_version", None),
              "image": stack.image_digest(args.ha_version), "core": core.path, "core_version": core.version,
              "capabilities": run.caps, "notes": run.notes, "rows": run.rows,
              "order": {k: getattr(run, k) for k in ("d1_order", "d3_order", "d4i_order", "d4w_order", "d4m_order",
                                                     "e_order_control", "e_order_one-pass",
                                                     "e_order_two-pass") if hasattr(run, k)}}
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(result, fh, indent=2)
        log(f"wrote {args.out}")
    bad = unexpected(run.rows)
    for r in bad:
        log(f"UNEXPECTED {r['scenario']} {r['class']}: {r['result']}")
    log(f"{len(run.rows) - len(bad)}/{len(run.rows)} rows as expected")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
