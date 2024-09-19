from __future__ import annotations

import inspect
import json
import logging
import time
from enum import Enum
from typing import Set

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from victron_ble.devices import Device, DeviceData, detect_device_type
from victron_ble.exceptions import AdvertisementKeyMissingError, UnknownDeviceError

import paho.mqtt.publish as publish

logger = logging.getLogger(__name__)


class BaseScanner:
    def __init__(self) -> None:
        """Initialize the scanner."""
        self._scanner: BleakScanner = BleakScanner(
            detection_callback=self._detection_callback
        )
        self._seen_data: Set[bytes] = set()

    def _detection_callback(self, device: BLEDevice, advertisement: AdvertisementData):
        # Filter for Victron devices and instant readout advertisements
        data = advertisement.manufacturer_data.get(0x02E1)
        if not data or not data.startswith(b"\x10") or data in self._seen_data:
            return

        # De-duplicate advertisements
        if len(self._seen_data) > 1000:
            self._seen_data = set()
        self._seen_data.add(data)

        self.callback(device, data)

    def callback(self, device: BLEDevice, data: bytes):
        raise NotImplementedError()

    async def start(self):
        await self._scanner.start()

    async def stop(self):
        await self._scanner.stop()


# An ugly hack to print a class as JSON
class DeviceDataEncoder(json.JSONEncoder):
    def default(self, obj):
        if issubclass(obj.__class__, DeviceData):
            data = {}
            for name, method in inspect.getmembers(obj, predicate=inspect.ismethod):
                if name.startswith("get_"):
                    value = method()
                    if isinstance(value, Enum):
                        value = value.name.lower()
                    if value is not None:
                        data[name[4:]] = value
            return data


class Scanner(BaseScanner):
    def __init__(self, device_keys: dict[str, str] = {}):
        super().__init__()
        self._device_keys = {k.lower(): v for k, v in device_keys.items()}
        self._known_devices: dict[str, Device] = {}

    async def start(self):
        logger.info(f"Reading data for {list(self._device_keys.keys())}")
        await super().start()

    def get_device(self, ble_device: BLEDevice, raw_data: bytes) -> Device:
        address = ble_device.address.lower()
        if address not in self._known_devices:
            advertisement_key = self.load_key(address)

            device_klass = detect_device_type(raw_data)
            if not device_klass:
                raise UnknownDeviceError(
                    f"Could not identify device type for {ble_device}"
                )

            self._known_devices[address] = device_klass(advertisement_key)
        return self._known_devices[address]

    def load_key(self, address: str) -> str:
        try:
            return self._device_keys[address]
        except KeyError:
            raise AdvertisementKeyMissingError(f"No key available for {address}")

    def callback(self, ble_device: BLEDevice, raw_data: bytes):
        logger.debug(
            f"Received data from {ble_device.address.lower()}: {raw_data.hex()}"
        )
        try:
            device = self.get_device(ble_device, raw_data)
        except AdvertisementKeyMissingError:
            return
        except UnknownDeviceError as e:
            logger.error(e)
            return
        parsed = device.parse(raw_data)

        blob = {
            "name": ble_device.name,
            "address": ble_device.address,
            "rssi": ble_device.rssi,
            "payload": parsed,
        }

        decoded_data_str = json.dumps(blob, cls=DeviceDataEncoder, indent=2)
        decoded_data = json.loads(decoded_data_str)

        msgs = [
            {'topic': f"victron/{decoded_data['address']}/name", 'payload': decoded_data['name']},
            {'topic': f"victron/{decoded_data['address']}/rssi", 'payload': str(decoded_data['rssi'])},
        ]

        for key, value in decoded_data['payload'].items():
            msgs.append({'topic': f"victron/{decoded_data['address']}/{key}", 'payload': str(value)})
        
        host = "localhost"
        print(msgs)
        publish.multiple(msgs, hostname=host)


class DiscoveryScanner(BaseScanner):
    def __init__(self) -> None:
        super().__init__()
        self._seen_devices: Set[str] = set()

    def callback(self, device: BLEDevice, advertisement: bytes):
        if device.address not in self._seen_devices:
            logger.info(f"{device}")
            self._seen_devices.add(device.address)


class DebugScanner(BaseScanner):
    def __init__(self, address: str):
        super().__init__()
        self.address = address

    async def start(self):
        logger.info(f"Dumping advertisements from {self.address}")
        await super().start()

    def callback(self, device: BLEDevice, data: bytes):
        if device.address.lower() == self.address.lower():
            logger.info(f"{time.time():<24}: {data.hex()}")
