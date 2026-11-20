# geist-pdu-exporter

Prometheus exporter for Geist PDUs. One exporter instance can scrape any
number of PDUs, and auto-detects the firmware family of each:

| Firmware | Example | Endpoint used |
| --- | --- | --- |
| v3 (RCU-O, "GEIST Embedded Web Server") | `RCU-O v3.16.3` | `/data.xml` |
| v5+ (R-Series, `httpd 5.x`) | `5.8.0` | `/api/sys`, `/api/dev` (JSON) |

Each PDU is polled by its own background thread (one request at a time) and
scrapes return the latest reading instantly, so a slow PDU never fails a
Prometheus scrape. Some v3 units stop answering for several seconds every
5 minutes; with scrape-time collection that showed up as regular gaps.
Readings older than `PDU_MAX_AGE` are dropped and `pdu_up` goes to 0.
Both firmwares allow unauthenticated reads, so no credentials are needed.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `PDU_TARGETS` | — | Comma-separated addresses, optionally `name=address` to override the `pdu` label (default: the hostname/label the PDU reports). `PDU_ADDRESS` is accepted as a legacy alias. |
| `PDU_PORT` | `80` | HTTP port on the PDUs |
| `POLLING_INTERVAL_SECONDS` | `10` | How often each PDU is polled |
| `PDU_REQUEST_TIMEOUT` | `20` | Per-request timeout, seconds (applies to connect and read separately) |
| `PDU_MAX_AGE` | `60` | Seconds a reading stays valid; older readings are dropped |
| `LISTEN_PORT` | `9100` | Exporter listen port |
| `LOG_LEVEL` | `INFO` | Python log level |

```sh
docker run -p 9100:9100 -e PDU_TARGETS=192.168.4.3,192.168.4.16 ghcr.io/drewburr/geist-pdu-exporter
```

## Metrics

All PDU metrics carry `pdu`, `id` and `type` labels; outlet metrics add
`name`, `num` (1-based) and `url` (v3 only, empty on v5).

| Metric | Notes |
| --- | --- |
| `pdu_up{address,firmware}` | 1 if a reading newer than `PDU_MAX_AGE` is available |
| `pdu_last_success_timestamp_seconds{address}` | Unix time of the last successful poll |
| `pdu_scrape_duration_seconds{address}` | Duration of the last poll |
| `pdu_poll_errors_total{address}` | Failed polls since start; `increase()` shows how often a PDU stalls |
| `pdu_kwh_total`, `pdu_kwh` | Device energy, total and phase A |
| `pdu_realpower_total`, `pdu_realpower` | Real power (W) |
| `pdu_volts`, `pdu_amps`, `pdu_apparent_power`, `pdu_power_factor_percent` | Phase A |
| `pdu_volts_peak`, `pdu_amps_peak` | v3 only |
| `pdu_outlet_amps`, `pdu_outlet_watts`, `pdu_outlet_kwh_total` | Per outlet |
| `pdu_outlet_volts`, `pdu_outlet_apparent_power`, `pdu_outlet_power_factor_percent` | Per outlet, v5 only |
| `pdu_outlet_status{pdu_outlet_status="On"\|"Off"}` | State set |

On v5 firmware, an outlet's `name` is its user label when set, otherwise the
default `Outlet N`.

## Development

```sh
uv venv && uv pip install -r requirements.txt
PDU_TARGETS=192.168.4.3,192.168.4.16 .venv/bin/python src/main.py
curl localhost:9100/metrics
```
