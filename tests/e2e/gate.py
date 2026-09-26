"""The measurement gate for the offline-tombstone RFC (RFC 0009): what option C's sequence does
to Home Assistant, measured before the core sends it.

- m1: churn on ids this install never had (the sequence sent for an unsupported endpoint):
  what HA keeps afterwards, whether a real entity later published with that unique_id gets its
  intended id, and whether the churn reaches history or the logbook.
- m2: orphans of disabled entities, as HA's discovery handles an empty payload for a disabled
  entity on a separate path (discovery.py, `discovery_discovered_and_disabled`).
- m3: the back-to-back sequence repeated on fresh orphans, one at a time and as one burst.

The sequences are built by the harness (core_replay.cycle), not by the core, so the gate is
independent of whether the core under test has option C; every such record carries `edited`.
Two variants of the minimal config are measured: `spec` (unique_id, device, required topic, as
the RFC proposes) and `disabled` (the same plus enabled_by_default: false).
"""
import copy
import datetime
import json
import time

import core_replay as cr
import stack

VARIANTS = {"spec": False, "disabled": True}
M1_CYCLES = 3
M3_TRIALS = 20
# What the add-ons' READMEs list as forbidden on both cars, plus hvac-settings, which the A290
# does not advertise (catalog.py). Everything else is treated as supported.
TYPICAL_UNSUPPORTED = {"pressure", "charge-mode", "hvac-settings"}
REGISTRY_FILE = "/config/.storage/core.entity_registry"
SHORT = {"sensor": "s", "binary_sensor": "b", "button": "p", "number": "n"}


def iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat()


def gate_catalog(device, rows, disabled=()):
    """A synthetic catalog holding exactly `rows` ({class: {obj: name}})."""
    cat = cr.synth_catalog(
        device,
        sensors={o: (n, None, None, None) for o, n in rows.get("sensor", {}).items()},
        binary={o: (n, None) for o, n in rows.get("binary_sensor", {}).items()},
        buttons={o: (n, "mdi:bell", "actions/wake") for o, n in rows.get("button", {}).items()},
        numbers={o: (n, "mdi:battery", 20, 80, 5) for o, n in rows.get("number", {}).items()})
    cat.DEFAULT_DISABLED_SENSORS = set(disabled)
    return cat


def retire_all(cat):
    """The next release of `cat`: every entity moved to its RETIRED_* list."""
    v2 = copy.copy(cat)
    v2.RETIRED_SENSORS, v2.SENSORS = list(cat.SENSORS), {}
    v2.RETIRED_BINARY_SENSORS, v2.BINARY_SENSORS = list(cat.BINARY_SENSORS), {}
    v2.RETIRED_BUTTONS, v2.ACTION_BUTTONS = list(cat.ACTION_BUTTONS), {}
    v2.RETIRED_NUMBERS, v2.NUMBERS = list(cat.NUMBERS), {}
    return v2


class Gate:
    """Mixed into harness.Run; uses its register/publish/row helpers."""

    def _deleted_records(self, uids, settle=15):
        """HA's deleted-entity records for `uids`. No REST or websocket API returns them, so this
        reads the registry's storage file, after the registry's delayed save has had time to run."""
        time.sleep(settle)
        data = json.loads(stack.ha_file(REGISTRY_FILE))["data"]
        return {e["unique_id"]: e for e in data.get("deleted_entities", []) if e["unique_id"] in uids}

    # ------------------------------------------------------------------ m1
    # One device per variant, so each variant's transient entity has its own id and its history
    # can be told apart: without a name HA calls every one "<device> MQTT <Domain>".
    M1_GROUPS = {"spec": "m1s", "disabled": "m1d", "nodei": "m1n", "fresh": "m1f"}

    def scenario_m1(self):
        log = self.log
        log("(m1) churn on ids this install never had")
        anchor_sfx, anchor_dev = "m1a", "E2E M1A Car"
        anchors = {"sensor": {"e2e_battery": "Battery"}, "binary_sensor": {"e2e_plug": "Plug"},
                   "button": {"e2e_wake": "Wake"}, "number": {"e2e_soc_min": "SoC Min"}}
        self.register(gate_catalog(anchor_dev, anchors), anchor_sfx,
                      [self.u(o, anchor_sfx) for c in cr.CLASSES for o in anchors[c]])
        uids, confs, full = {}, {}, {}
        for v, sfx in self.M1_GROUPS.items():
            rows = {c: {f"e2e_{sfx}_{SHORT[c]}": f"Churn {c.replace('_', ' ')}"} for c in cr.CLASSES}
            full[v] = cr.isolate(cr.record(self.core, gate_catalog(f"E2E {sfx.upper()} Car", rows)), sfx)
            confs.update(cr.configs(full[v]))
            for c in cr.CLASSES:
                uids[(c, v)] = self.u(f"e2e_{sfx}_{SHORT[c]}", sfx)
        churn = {k: u for k, u in uids.items() if k[1] != "fresh"}

        # Positive controls for history and the logbook: real state changes on the anchors.
        node = f"{cr.SYNTH_NODE}_{anchor_sfx}"
        self.broker.replay([{"topic": f"{node}/availability", "payload": "online", "retain": True, "qos": 0}])
        t_start = time.time()
        for payload in ({"battery": 41, "plug": "on"}, {"battery": 42, "plug": "off"}):
            self.broker.replay([{"topic": f"{node}/state", "payload": json.dumps(payload), "retain": True, "qos": 0}])
            time.sleep(2)

        registered_lines = []
        for k in range(M1_CYCLES):
            seq = []
            for (_c, v), uid in churn.items():
                _, rec, conf = confs[uid]
                seq += cr.cycle(rec, conf, f"HARNESS m1 churn {v}", disabled=VARIANTS.get(v, False))
            since = time.time() - 1
            self.broker.replay(seq)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                lines = [ln for ln in stack.ha_logs(since).splitlines() if "Registered new" in ln and ".mqtt entity:" in ln]
                if len(lines) >= len(churn):
                    break
                time.sleep(0.5)
            ok, snap = self.ha.wait_registry(absent=list(churn.values()), timeout=30)
            registered_lines.append(len(lines))
            log(f"  m1 cycle {k + 1}: {len(lines)} 'Registered new' lines, all removed={ok}")
        deleted = self._deleted_records(set(churn.values()))
        t_end = time.time()

        for (c, v), uid in churn.items():
            d = deleted.get(uid)
            self.row(f"m1/transient/{v}", c, "CREATED-AND-REMOVED" if d and uid not in snap else
                     "STILL-REGISTERED" if uid in snap else "NO-RECORD",
                     {"unique_id": uid, "deleted_record": d and {k2: d.get(k2) for k2 in
                      ("entity_id", "disabled_by", "hidden_by", "orphaned_timestamp", "created_at", "id")},
                      "registered_lines_per_cycle": registered_lines},
                     "NO-RECORD (HA never created the entity)",
                     "deleted-entity record read from .storage/core.entity_registry; no API exposes it")

        # (b) history and logbook, with the anchors as positive controls.
        time.sleep(8)       # the recorder commits on an interval
        by_variant = {v: sorted({deleted[u]["entity_id"] for (_, v2), u in churn.items() if v2 == v and u in deleted})
                      for v in ("spec", "disabled", "nodei")}
        transient_ids = sorted({e for ids in by_variant.values() for e in ids})
        ctl_ids = [f"sensor.{cr_slug(anchor_dev)}_battery", f"binary_sensor.{cr_slug(anchor_dev)}_plug"]
        hist = self.ha.history(iso(t_start - 5), transient_ids + ctl_ids, iso(time.time()))
        book = self.ha.logbook(iso(t_start - 5), iso(time.time()))
        for v, ids in by_variant.items():
            h = [s for s in hist if s["entity_id"] in ids]
            b = [e for e in book if e.get("entity_id") in ids]
            self.row(f"m1/history/{v}", "all", "NONE" if not h else "ENTRIES",
                     {"transient_ids": ids, "rows": len(h), "rows_per_id_per_cycle": round(len(h) / max(1, len(ids)) / M1_CYCLES, 2),
                      "entries": [(s["entity_id"], s["state"], s["last_changed"]) for s in h]},
                     "ENTRIES", f"window {iso(t_start - 5)} .. {iso(t_end)}; see m1/history-control")
            self.row(f"m1/logbook/{v}", "all", "NONE" if not b else "ENTRIES",
                     {"transient_ids": ids, "entries": b}, "ENTRIES", "see m1/logbook-control")
        h_ctl = [s for s in hist if s["entity_id"] in ctl_ids]
        b_ctl = [e for e in book if e.get("entity_id") == ctl_ids[1]]
        self.row("m1/history-control", "all", "SEEN" if len({s["state"] for s in h_ctl}) >= 2 else "NOT-SEEN",
                 {"entries": [(s["entity_id"], s["state"]) for s in h_ctl]}, "NOT-SEEN",
                 "anchor sensor and binary_sensor were given two states in the same window")
        self.row("m1/logbook-control", "all", "SEEN" if b_ctl else "NOT-SEEN",
                 {"entries": [(e.get("entity_id"), e.get("state"), e.get("when")) for e in b_ctl]}, "NOT-SEEN",
                 "binary_sensor anchor went on then off in the same window")

        # (a) the real entity later published with each churned unique_id.
        nodei = {uids[(c, "nodei")] for c in cr.CLASSES}
        later = [r for v in self.M1_GROUPS for r in full[v]]
        later = cr.strip_default_entity_id(later, nodei)
        want = list(uids.values())
        ok, snap = self.publish(later, present=want)
        deadline = time.monotonic() + 20      # MQTT renames a restored entry once it is added
        while time.monotonic() < deadline:
            snap = self.ha.entities_by_uid()
            if all(snap.get(u, {}).get("entity_id") == confs[u][2]["default_entity_id"]
                   for u in want if u not in nodei):
                break
            time.sleep(0.5)
        for (c, v), uid in uids.items():
            e = snap.get(uid, {})
            eid, dei = e.get("entity_id"), confs[uid][2]["default_entity_id"]
            d = deleted.get(uid) or {}
            if v == "nodei":
                result = ("RESTORED-TRANSIENT-ID" if eid and eid == d.get("entity_id") else
                          "NAME-DERIVED" if eid == dei else "OTHER")
            else:
                result = ("MISSING" if not eid else "DISABLED" if e.get("disabled_by") else
                          "INTENDED" if eid == dei else "SUFFIXED" if eid.startswith(dei + "_") else "CHANGED")
            self.row(f"m1/later/{v}", c, result,
                     {"unique_id": uid, "entity_id": eid, "default_entity_id": dei, "transient_entity_id": d.get("entity_id"),
                      "disabled_by": e.get("disabled_by"), "hidden_by": e.get("hidden_by")},
                     "a changed, _2 or disabled id" if v != "nodei" else "the name-derived id",
                     "control: m1/later/nodei (default_entity_id removed) shows whether HA restores the "
                     "transient id from the deleted record; m1/later/fresh was never churned")

    def scenario_m1_count(self):
        """How many cycles one pass of the core sends for each real catalog, typical endpoints."""
        for spec in self.args.catalog:
            cat = cr.load_catalog(spec)
            supported = cr.all_endpoints(cat) - TYPICAL_UNSUPPORTED
            env = {cat.ENV_PREFIX + "PUBLISH_LOCATION": "true", cat.ENV_PREFIX + "ENABLE_REFRESH_LOCATION": "false"}
            recs = cr.record(self.core, cat, supported, env=env)
            tombs = cr.tombstones(recs)
            self.row(f"m1/count/{cat.NODE}", "all", "COUNTED",
                     {"tombstones_per_pass": len(tombs), "resurrected_per_pass": len(cr.resurrected(recs)),
                      "topics": [r["topic"] for r in tombs], "supported": sorted(supported),
                      "unsupported": sorted(cr.all_endpoints(cat) - supported)},
                     "", "location on, refresh-location off (both defaults); one pass per HA birth and "
                     "per add-on connect")

    # ------------------------------------------------------------------ m2 + m3
    M2_CASES = ("dis_online", "on_spec", "on_disabled", "dis_orphan", "dis_spec", "dis_disabled",
                "en_orphan", "en_spec", "en_disabled", "usr_orphan", "usr_spec", "usr_disabled")
    M2_ONLINE = {"dis_online", "on_spec", "on_disabled"}
    M3_GROUPS = {"m3s": ("spec", "sequential"), "m3b": ("spec", "burst"),
                 "m3ds": ("disabled", "sequential"), "m3db": ("disabled", "burst")}

    def gate_prepare(self):
        log = self.log
        log("(m2/m3) registering entities for the gate restart")
        cases = {k: f"e2e_m2_{k}" for k in self.M2_CASES}
        cat = gate_catalog("E2E M2 Car", {"sensor": {o: f"M2 {k}" for k, o in cases.items()}},
                           disabled={o for k, o in cases.items() if k.startswith("dis_")})
        uids = {k: self.u(o, "m2") for k, o in cases.items()}
        v1, before = self.register(cat, "m2", list(uids.values()))
        self.m2 = {"cat": cat, "uids": uids, "v1": v1, "before": before}
        wrong = {k: before[u]["disabled_by"] for k, u in uids.items()
                 if (before[u]["disabled_by"] == "integration") != k.startswith("dis_")}
        if wrong:
            raise RuntimeError(f"m2: enabled_by_default did not register as expected: {wrong}")

        # Control: the plain tombstone, HA online, for a disabled entity discovered in this run.
        u = uids["dis_online"]
        since = time.time() - 1
        ok, snap = self.publish([{"topic": cr.topic_of(v1, u), "payload": "", "retain": True, "qos": 0}], absent=[u])
        self.row("m2/online-plain", "sensor", "REMOVED" if ok else "NOT-REMOVED",
                 {"entity_id": before[u]["entity_id"], "disabled_by": before[u]["disabled_by"],
                  "ha_logged": [ln for ln in stack.ha_logs(since).splitlines() if before[u]["entity_id"] in ln][:3]},
                 "NOT-REMOVED", "plain empty retained payload, HA online, entity discovered in this run")

        # The ordinary online retirement: an enabled entity HA discovered in this run.
        for k in ("on_spec", "on_disabled"):
            u = uids[k]
            _, rec, conf = cr.configs(v1)[u]
            since = time.time() - 1
            ok, _ = self.publish(cr.cycle(rec, conf, f"HARNESS m2 {k}", disabled=VARIANTS[k[3:]]), absent=[u])
            self.row(f"m2/online-{k[3:]}", "sensor", "REMOVED" if ok else "NOT-REMOVED",
                     {"entity_id": before[u]["entity_id"],
                      "ha_logged_removal": f"Removing component: {before[u]['entity_id']}" in stack.ha_logs(since)},
                     "NOT-REMOVED", "HA online, enabled entity discovered in this run")

        for k in ("usr_orphan", "usr_spec", "usr_disabled"):
            self.ha.update_entity(before[uids[k]]["entity_id"], disabled_by="user")
        snap = self.ha.entities_by_uid()
        if any(snap[uids[k]]["disabled_by"] != "user" for k in ("usr_orphan", "usr_spec", "usr_disabled")):
            raise RuntimeError("m2: user disable did not stick")
        # Disabling an entity reloads its config entry after 30 s; let that happen now, not
        # in the middle of a later scenario.
        time.sleep(40)
        self.ha.wait_mqtt_loaded()

        self.m3 = {}
        for sfx, (variant, mode) in self.M3_GROUPS.items():
            rows = {c: {f"e2e_{sfx}_{SHORT[c]}{i:02d}": f"{sfx.upper()} {SHORT[c].upper()} {i:02d}"
                        for i in range(M3_TRIALS)} for c in cr.CLASSES}
            cat = gate_catalog(f"E2E {sfx.upper()} Car", rows)
            uids = {(c, o): self.u(o, sfx) for c in cr.CLASSES for o in rows[c]}
            v1, _ = self.register(cat, sfx, list(uids.values()))
            self.m3[sfx] = {"cat": cat, "uids": uids, "v1": v1, "variant": variant, "mode": mode}

    def gate_restart(self):
        log = self.log
        log("(m2/m3) tombstones published while HA is down, then option C's sequence after the birth")
        m2 = self.m2
        keep = {"sensor": {}}
        v2 = gate_catalog("E2E M2 Car", keep)
        v2.RETIRED_SENSORS = [f"e2e_m2_{k}" for k in self.M2_CASES if k not in self.M2_ONLINE]
        passes = cr.isolate(cr.record(self.core, v2), "m2")
        for sfx, g in self.m3.items():
            passes += cr.isolate(cr.record(self.core, retire_all(g["cat"])), sfx)
        late_cat = gate_catalog("E2E M2 Car", {"sensor": {"e2e_m2_late": "M2 late"}})
        late = [r for r in cr.isolate(cr.record(self.core, late_cat), "m2") if "/e2e_m2_late/" in r["topic"]]
        late_uid = self.u("e2e_m2_late", "m2")
        watcher = self.ensure_watcher()

        stack.stop_ha()
        self.ha.ws_close()
        self.broker.replay(passes + late)
        t0 = time.monotonic()
        stack.start_ha()
        self.ha.wait_api()
        self.ha.wait_mqtt_loaded()
        ok_ctl, _ = self.ha.wait_registry(present=[late_uid], timeout=120)
        birth = watcher.wait_for(cr.BIRTH_TOPIC, "online", after=t0, timeout=180)
        restart_ctl = (f"control: {late_uid}, published in the offline window, "
                       f"{'registered' if ok_ctl else 'DID NOT register'}; birth "
                       f"{'seen' if birth else 'NOT seen'} {round(birth['t'] - t0, 1) if birth else None}s after start")
        log(f"  {restart_ctl}")

        # m2: the sequence per case, all at once, then one window for every case.
        seq, targets = [], []
        for k, u in m2["uids"].items():
            variant = k.split("_", 1)[1]
            if variant in VARIANTS and k not in self.M2_ONLINE:
                _, rec, conf = cr.configs(m2["v1"])[u]
                seq += cr.cycle(rec, conf, f"HARNESS m2 {variant}", disabled=VARIANTS[variant])
                targets.append(u)
        pre = self.ha.entities_by_uid()
        since = time.time() - 1
        self.broker.replay(seq)
        _, snap = self.ha.wait_registry(absent=targets, timeout=60)
        logs = stack.ha_logs(since)
        for k, u in m2["uids"].items():
            if k in self.M2_ONLINE:
                continue
            eid = m2["before"][u]["entity_id"]
            self.row(f"m2/{k}", "sensor", "REMOVED" if u not in snap else "NOT-REMOVED",
                     {"entity_id": eid, "disabled_by_at_birth": pre.get(u, {}).get("disabled_by", "<not registered>"),
                      "sent_after_birth": k.split("_", 1)[1] if u in targets else "nothing (orphan control)",
                      "ha_found_in_this_run": f"Found new component: sensor {cr.SYNTH_NODE}_m2 e2e_m2_{k}" in logs,
                      "ha_logged_removal": f"Removing component: {eid}" in logs},
                     "the other outcome", f"{restart_ctl}; watched 60 s after the sequence")

        # m3: sequential trials, then bursts.
        for g in self.m3.values():
            confs = cr.configs(g["v1"])
            label = f"HARNESS m3 {g['variant']} {g['mode']}"
            results = {c: [] for c in cr.CLASSES}
            if g["mode"] == "sequential":
                for (c, _), u in g["uids"].items():
                    _, rec, conf = confs[u]
                    t = time.monotonic()
                    self.broker.replay(cr.cycle(rec, conf, label, disabled=VARIANTS[g["variant"]]))
                    ok, _ = self.ha.wait_registry(absent=[u], timeout=15, interval=0.2)
                    results[c].append((u, ok, round(time.monotonic() - t, 2)))
            else:
                seq = []
                for u in g["uids"].values():
                    _, rec, conf = confs[u]
                    seq += cr.cycle(rec, conf, label, disabled=VARIANTS[g["variant"]])
                t = time.monotonic()
                self.broker.replay(seq)
                ok, snap = self.ha.wait_registry(absent=list(g["uids"].values()), timeout=60)
                for (c, _), u in g["uids"].items():
                    results[c].append((u, u not in snap, round(time.monotonic() - t, 2)))
            for c in cr.CLASSES:
                n_ok = sum(1 for _, ok, _ in results[c] if ok)
                self.row(f"m3/{g['variant']}/{g['mode']}", c, f"{n_ok}/{len(results[c])}",
                         {"failed": [u for u, ok, _ in results[c] if not ok],
                          "max_s": max(s for _, _, s in results[c]), "first": results[c][0][0], "last": results[c][-1][0]},
                         "any trial left registered", f"{restart_ctl}; fresh orphan per trial")


def cr_slug(text):
    return text.lower().replace(" ", "_")
