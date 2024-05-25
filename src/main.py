"""Application exporter"""

import os
import time
import logging
from prometheus_client import start_http_server, Gauge, Counter, Enum
import requests
import xml.etree.ElementTree as ET
from xml.etree.ElementTree import Element

logging.basicConfig(
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%m/%d/%Y %I:%M:%S %p",
    level=logging.INFO,
)

# pdu_


class Exporter:
    """
    Representation of Prometheus metrics and loop to fetch and transform
    application metrics into Prometheus metrics.
    """

    def __init__(self, address, port, polling_interval_seconds, pdu_request_timeout):
        self.address = address
        self.port = port
        self.polling_interval_seconds = polling_interval_seconds
        self.pdu_request_timeout = pdu_request_timeout
        self.logger = logging.getLogger(__name__)

        self.device_labels = ["id", "type"]
        self.outlet_labels = ["name", "num", "url"]

        self.device_metrics = {
            "KWatt-hrs-Total": Gauge(
                "pdu_kwh_total", "Total device KWh", self.device_labels
            ),
            "KWatt-hrs-A": Gauge("pdu_kwh", "Device KWh", self.device_labels),
            "RealPower-Total": Gauge(
                "pdu_realpower_total", "Device RealPower", self.device_labels
            ),
            "RealPower-A": Gauge(
                "pdu_realpower", "Device realpower", self.device_labels
            ),
            "Volts-A": Gauge("pdu_volts", "Device voltage", self.device_labels),
            "Volt-Pk-A": Gauge(
                "pdu_volts_peak", "Device peak voltage", self.device_labels
            ),
            "Amps-A": Gauge("pdu_amps", "Device amperage", self.device_labels),
            "Amps-Pk-A": Gauge(
                "pdu_amps_peak", "Device peak amperage", self.device_labels
            ),
            "ApPower-A": Gauge(
                "pdu_apparent_power", "Device apparent power", self.device_labels
            ),
            "Pwr-Factor%-A": Gauge(
                "pdu_power_factor_percent",
                "Power Factor Percentage",
                self.device_labels,
            ),
        }

        self.outlet_metrics = {
            "amps": Gauge(
                "pdu_outlet_amps",
                "Outlet Amperage",
                self.outlet_labels + self.device_labels,
            ),
            "kwatthrs": Gauge(
                "pdu_outlet_kwh_total",
                "Outlet Total KWh",
                self.outlet_labels + self.device_labels,
            ),
            "watts": Gauge(
                "pdu_outlet_watts",
                "Outlet Watts",
                self.outlet_labels + self.device_labels,
            ),
        }

        self.outlet_status = Enum(
            "pdu_outlet_status",
            "Outlet status",
            self.outlet_labels + self.device_labels,
            states=["On", "Off"],
        )

    def start_export_loop(self):
        """Metrics fetching loop"""

        while True:
            start = time.time()
            self.process()
            end = time.time()
            self.logger.info(f"Process completed in {end-start:.2f}s")
            time.sleep(self.polling_interval_seconds)

    def process(self):
        """
        Generate metrics and publish
        """
        self.logger.info("Starting process..")
        root = self.fetch()
        devices = root.find("devices")

        for device in devices:
            self.process_device(device)

    def process_device(self, device: Element):
        self.logger.info(f"Processsing device {device.attrib['type']}")
        outlets = device.find("outlets")
        if not outlets:
            return

        device_labels = {label: device.attrib[label] for label in self.device_labels}

        for child in device:
            if not child.tag == "field":
                continue

            metric = self.device_metrics.get(child.attrib["key"])
            if metric:
                metric.labels(**device_labels).set(float(child.attrib["value"]))

        for outlet in outlets:
            self.process_outlet(outlet, device_labels)

    def process_outlet(self, outlet: Element, device_labels: dict[str, str]):
        self.logger.info(f"Processsing outlet {outlet.attrib['num']}")
        outlet_labels = {label: outlet.attrib[label] for label in self.outlet_labels}
        outlet_labels = {**outlet_labels, **device_labels}

        for attr, metric in self.outlet_metrics.items():
            metric.labels(**outlet_labels).set(float(outlet.attrib[attr]))

        self.outlet_status.labels(**outlet_labels).state(outlet.attrib["status"])

    def fetch(self):
        """
        Get metrics from PDU and return
        """
        try:
            resp = requests.get(url=f"http://{self.address}:{self.port}/data.xml", timeout=self.pdu_request_timeout)
        except requests.ConnectionError as e:
            self.logger.warn(f"Exception while connecting to device: {e.strerror}")

        root = ET.fromstring(resp.text)
        return root


def main():
    """Main entry point"""
    from dotenv import load_dotenv

    load_dotenv(
        override=False
    )  # load .env into environment variables, without override

    PDU_ADDRESS = os.getenv("PDU_ADDRESS")
    PDU_PORT = int(os.getenv("PDU_PORT", "80"))
    POLLING_INTERVAL_SECONDS = int(os.getenv("POLLING_INTERVAL_SECONDS", "5"))
    LISTEN_PORT = int(os.getenv("LISTEN_PORT", "9100"))
    PDU_REQUEST_TIMEOUT = int(os.getenv("PDU_REQUEST_TIMEOUT", 5))

    exporter = Exporter(
        address=PDU_ADDRESS,
        port=PDU_PORT,
        polling_interval_seconds=POLLING_INTERVAL_SECONDS,
        pdu_request_timeout=PDU_REQUEST_TIMEOUT
    )
    start_http_server(LISTEN_PORT)
    exporter.start_export_loop()


if __name__ == "__main__":
    main()
