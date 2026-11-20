"""Prometheus exporter for Geist PDUs.

Supports two firmware families, auto-detected per target:

* v3 ("RCU-O" / OWL web server) - exposes ``/data.xml``
* v5+ ("R-Series", ``httpd 5.x``) - exposes a JSON API at ``/api/dev``

Metrics are collected at scrape time (no background polling), so a scrape
always reflects the device's current state and a dead PDU shows up as
``pdu_up 0`` rather than as stale values.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import requests
from prometheus_client import start_http_server
from prometheus_client.core import (
    REGISTRY,
    GaugeMetricFamily,
    StateSetMetricFamily,
)
from prometheus_client.registry import Collector

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
log = logging.getLogger("geist-pdu-exporter")

DEVICE_LABELS = ["pdu", "id", "type"]
OUTLET_LABELS = ["name", "num", "url"] + DEVICE_LABELS

# metric name -> help text. Names are kept compatible with the original
# exporter so existing dashboards keep working.
DEVICE_METRICS = {
    "pdu_kwh_total": "Total device energy (kWh)",
    "pdu_kwh": "Phase A energy (kWh)",
    "pdu_realpower_total": "Total device real power (W)",
    "pdu_realpower": "Phase A real power (W)",
    "pdu_volts": "Phase A voltage (V)",
    "pdu_volts_peak": "Phase A peak voltage (V, v3 firmware only)",
    "pdu_amps": "Phase A current (A)",
    "pdu_amps_peak": "Phase A peak current (A, v3 firmware only)",
    "pdu_apparent_power": "Phase A apparent power (VA)",
    "pdu_power_factor_percent": "Phase A power factor (%)",
}

OUTLET_METRICS = {
    "pdu_outlet_amps": "Outlet current (A)",
    "pdu_outlet_kwh_total": "Outlet energy (kWh)",
    "pdu_outlet_watts": "Outlet real power (W)",
    "pdu_outlet_volts": "Outlet voltage (V, v5 firmware only)",
    "pdu_outlet_apparent_power": "Outlet apparent power (VA, v5 firmware only)",
    "pdu_outlet_power_factor_percent": "Outlet power factor (%, v5 firmware only)",
}

OUTLET_STATES = ["On", "Off"]


@dataclass
class Outlet:
    num: str
    name: str
    url: str
    status: str  # "On" / "Off"
    values: dict[str, float] = field(default_factory=dict)


@dataclass
class Device:
    pdu: str
    id: str
    type: str
    values: dict[str, float] = field(default_factory=dict)
    outlets: list[Outlet] = field(default_factory=list)


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# v3 firmware: /data.xml
# --------------------------------------------------------------------------

V3_DEVICE_FIELDS = {
    "KWatt-hrs-Total": "pdu_kwh_total",
    "KWatt-hrs-A": "pdu_kwh",
    "RealPower-Total": "pdu_realpower_total",
    "RealPower-A": "pdu_realpower",
    "Volts-A": "pdu_volts",
    "Volt-Pk-A": "pdu_volts_peak",
    "Amps-A": "pdu_amps",
    "Amps-Pk-A": "pdu_amps_peak",
    "ApPower-A": "pdu_apparent_power",
    "Pwr-Factor%-A": "pdu_power_factor_percent",
}

V3_OUTLET_ATTRS = {
    "amps": "pdu_outlet_amps",
    "kwatthrs": "pdu_outlet_kwh_total",
    "watts": "pdu_outlet_watts",
}


def parse_v3(text: str, pdu_name: str | None) -> list[Device]:
    root = ET.fromstring(text)
    host = pdu_name or root.get("host") or root.get("address", "")
    devices = []
    for dev in root.iterfind("devices/device"):
        if dev.get("available", "1") != "1":
            continue
        device = Device(pdu=host, id=dev.get("id", ""), type=dev.get("type", ""))
        for fld in dev.iterfind("field"):
            metric = V3_DEVICE_FIELDS.get(fld.get("key", ""))
            val = _float(fld.get("value"))
            if metric and val is not None:
                device.values[metric] = val
        for out in dev.iterfind("outlets/outlet"):
            outlet = Outlet(
                num=out.get("num", ""),
                name=out.get("name", ""),
                url=out.get("url", ""),
                status=out.get("status", "").capitalize(),
            )
            for attr, metric in V3_OUTLET_ATTRS.items():
                val = _float(out.get(attr))
                if val is not None:
                    outlet.values[metric] = val
            device.outlets.append(outlet)
        devices.append(device)
    return devices


# --------------------------------------------------------------------------
# v5 firmware: /api/dev (JSON)
# --------------------------------------------------------------------------

V5_PHASE_MEASUREMENTS = {
    "energy": "pdu_kwh",
    "realPower": "pdu_realpower",
    "voltage": "pdu_volts",
    "current": "pdu_amps",
    "apparentPower": "pdu_apparent_power",
    "powerFactor": "pdu_power_factor_percent",
}

V5_TOTAL_MEASUREMENTS = {
    "energy": "pdu_kwh_total",
    "realPower": "pdu_realpower_total",
}

V5_OUTLET_MEASUREMENTS = {
    "current": "pdu_outlet_amps",
    "energy": "pdu_outlet_kwh_total",
    "realPower": "pdu_outlet_watts",
    "voltage": "pdu_outlet_volts",
    "apparentPower": "pdu_outlet_apparent_power",
    "powerFactor": "pdu_outlet_power_factor_percent",
}


def _v5_measurements(entity: dict, mapping: dict[str, str]) -> dict[str, float]:
    out = {}
    for meas in (entity.get("measurement") or {}).values():
        metric = mapping.get(meas.get("type"))
        val = _float(meas.get("value"))
        if metric and val is not None:
            out[metric] = val
    return out


def parse_v5(payload: dict, pdu_name: str | None, model: str) -> list[Device]:
    devices = []
    for dev_id, dev in (payload.get("data") or {}).items():
        if not isinstance(dev, dict) or "outlet" not in dev:
            continue  # sensors etc. without outlets aren't PDUs
        device = Device(
            pdu=pdu_name or dev.get("label") or dev_id,
            id=dev_id,
            type=model or dev.get("name", ""),
        )
        entities = dev.get("entity") or {}
        if "phase0" in entities:
            device.values.update(
                _v5_measurements(entities["phase0"], V5_PHASE_MEASUREMENTS)
            )
        if "total0" in entities:
            device.values.update(
                _v5_measurements(entities["total0"], V5_TOTAL_MEASUREMENTS)
            )
        else:
            # Single-phase units without a total entity: total == phase A
            for src, dst in (
                ("pdu_kwh", "pdu_kwh_total"),
                ("pdu_realpower", "pdu_realpower_total"),
            ):
                if src in device.values:
                    device.values[dst] = device.values[src]

        for key, out in sorted(
            (dev.get("outlet") or {}).items(), key=lambda kv: int(kv[0])
        ):
            num = int(key) + 1  # v5 keys are 0-based; v3 and the panel are 1-based
            outlet = Outlet(
                num=str(num),
                name=out.get("label") or out.get("name") or f"Outlet {num}",
                url="",
                status=str(out.get("state", "")).capitalize(),
                values=_v5_measurements(out, V5_OUTLET_MEASUREMENTS),
            )
            device.outlets.append(outlet)
        devices.append(device)
    return devices


# --------------------------------------------------------------------------
# Target handling
# --------------------------------------------------------------------------


class Target:
    """A single PDU. Firmware family is detected on first contact and cached."""

    def __init__(self, address: str, name: str | None, port: int, timeout: float):
        self.address = address
        self.name = name
        self.base = f"http://{address}" + (f":{port}" if port != 80 else "")
        self.timeout = timeout
        self.session = requests.Session()
        self.firmware: str | None = None  # "v3" | "v5"
        self.model = ""
        self.lock = threading.Lock()

    def _get(self, path: str) -> requests.Response:
        resp = self.session.get(self.base + path, timeout=self.timeout)
        resp.raise_for_status()
        return resp

    def _detect(self) -> None:
        try:
            sys_info = self._get("/api/sys").json()
            self.model = (sys_info.get("data") or {}).get("model", "")
            self.firmware = "v5"
        except (requests.HTTPError, ValueError):
            # v3 firmware 404s here; confirm data.xml exists instead.
            self._get("/data.xml")
            self.firmware = "v3"
        log.info("%s: detected %s firmware", self.address, self.firmware)

    def fetch(self) -> list[Device]:
        # Embedded web servers are slow and single-threaded; never overlap
        # requests to the same device.
        with self.lock:
            if self.firmware is None:
                self._detect()
            try:
                if self.firmware == "v5":
                    return parse_v5(self._get("/api/dev").json(), self.name, self.model)
                return parse_v3(self._get("/data.xml").text, self.name)
            except (requests.HTTPError, ValueError, ET.ParseError):
                self.firmware = None  # unexpected payload: re-detect next time
                raise


class PDUCollector(Collector):
    def __init__(self, targets: list[Target]):
        self.targets = targets
        self.pool = ThreadPoolExecutor(max_workers=max(1, len(targets)))

    def describe(self):
        # Without this, REGISTRY.register() calls collect() to discover metric
        # names, i.e. scrapes every PDU before the HTTP server even starts.
        return []

    @staticmethod
    def _scrape(target: Target):
        start = time.monotonic()
        try:
            devices, ok = target.fetch(), True
        except Exception as e:  # noqa: BLE001 - any failure means "down"
            log.warning("%s: scrape failed: %s", target.address, e)
            devices, ok = [], False
        return target, devices, ok, time.monotonic() - start

    def collect(self):
        up = GaugeMetricFamily(
            "pdu_up",
            "Whether the last scrape of the PDU succeeded",
            labels=["address", "firmware"],
        )
        duration = GaugeMetricFamily(
            "pdu_scrape_duration_seconds",
            "Time taken to scrape the PDU",
            labels=["address"],
        )
        dev_fams = {
            n: GaugeMetricFamily(n, h, labels=DEVICE_LABELS)
            for n, h in DEVICE_METRICS.items()
        }
        out_fams = {
            n: GaugeMetricFamily(n, h, labels=OUTLET_LABELS)
            for n, h in OUTLET_METRICS.items()
        }
        status = StateSetMetricFamily(
            "pdu_outlet_status", "Outlet status", labels=OUTLET_LABELS
        )

        for target, devices, ok, took in self.pool.map(self._scrape, self.targets):
            up.add_metric([target.address, target.firmware or ""], 1 if ok else 0)
            duration.add_metric([target.address], took)
            for dev in devices:
                dlabels = [dev.pdu, dev.id, dev.type]
                for metric, val in dev.values.items():
                    dev_fams[metric].add_metric(dlabels, val)
                for out in dev.outlets:
                    olabels = [out.name, out.num, out.url] + dlabels
                    for metric, val in out.values.items():
                        out_fams[metric].add_metric(olabels, val)
                    if out.status in OUTLET_STATES:
                        status.add_metric(
                            olabels, {s: out.status == s for s in OUTLET_STATES}
                        )

        yield up
        yield duration
        yield from dev_fams.values()
        yield from out_fams.values()
        yield status


def parse_targets(raw: str) -> list[tuple[str, str | None]]:
    """``"192.168.4.3,pdu02=192.168.4.16"`` -> [(address, name or None), ...]"""
    targets = []
    for item in raw.replace("\n", ",").split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, addr = item.partition("=")
        targets.append((addr.strip(), name.strip()) if sep else (item, None))
    return targets


def main():
    raw = os.getenv("PDU_TARGETS") or os.getenv("PDU_ADDRESS")  # PDU_ADDRESS: legacy
    if not raw:
        raise SystemExit("Set PDU_TARGETS (comma separated, optional name=address)")
    port = int(os.getenv("PDU_PORT", "80"))
    timeout = float(os.getenv("PDU_REQUEST_TIMEOUT", "4"))
    listen = int(os.getenv("LISTEN_PORT", "9100"))

    targets = [Target(addr, name, port, timeout) for addr, name in parse_targets(raw)]
    REGISTRY.register(PDUCollector(targets))
    start_http_server(listen)
    log.info("Listening on :%d for %s", listen, ", ".join(t.address for t in targets))

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()


if __name__ == "__main__":
    main()
