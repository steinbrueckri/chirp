# Bluetooth LE "transparent UART" serial port for pyserial.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""pyserial handler for ble:// URLs.

Many radios with built-in Bluetooth expose an HM-10 style "transparent UART":
GATT service 0xffe0 with characteristic 0xffe1, which carries the same bytes
the programming cable would. This handler makes such a device look like a
serial port, so a regular clone-mode driver can talk to it unchanged.

URL forms (enter them via "Custom..." in the port list):

  ble://                 scan and use the single device offering 0xffe0
  ble://<name>           device with this advertised name
  ble://<address>        device with this address (a MAC on Linux/Windows,
                         a CoreBluetooth UUID on macOS)

Requires the optional ``bleak`` package. Drivers that need extra GATT
traffic (e.g. an unlock write) can use write_characteristic(); drivers that
need acknowledged writes for some frames can set ``write_with_response``.
"""

import asyncio
import concurrent.futures
import logging
import threading

import serial
from serial.serialutil import SerialBase, SerialException, Timeout

LOG = logging.getLogger(__name__)

UART_SERVICE = '0000ffe0-0000-1000-8000-00805f9b34fb'
UART_CHAR = '0000ffe1-0000-1000-8000-00805f9b34fb'

SCAN_TIMEOUT = 8.0
# Once a serial device shows up, keep scanning this long so a second one is
# still noticed (and reported) instead of silently picking either.
SCAN_GRACE = 1.0
CONNECT_TIMEOUT = 20.0
# Payload size of a GATT write that every BLE stack accepts (ATT MTU 23).
WRITE_CHUNK = 20


class Serial(SerialBase):
    """A pyserial port backed by a BLE transparent-UART characteristic.

    bleak is asyncio based, so it runs on a private event loop in a
    background thread; the blocking pyserial API hands work to that loop
    and waits for the result.
    """

    write_with_response = False

    def open(self):
        if self.is_open:
            raise SerialException('Port is already open.')
        try:
            import bleak  # noqa: F401
        except ImportError as e:
            raise SerialException(
                'Bluetooth LE support needs the "bleak" Python package '
                '(pip install bleak): %s' % e)

        self._target = self._port.split('://', 1)[1].strip('/')
        self._rx = bytearray()
        self._rx_cond = threading.Condition()
        self._client = None
        self._disconnected = False
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever,
                                        name='ble-serial', daemon=True)
        self._thread.start()
        try:
            self._call(self._connect(),
                       timeout=SCAN_TIMEOUT + CONNECT_TIMEOUT + 5)
        except Exception as e:
            self._stop_loop()
            if isinstance(e, SerialException):
                raise
            raise SerialException('Bluetooth LE connect to %s failed: %s' % (
                self._port, e))
        self.is_open = True

    def close(self):
        if getattr(self, '_loop', None) is None:
            return
        try:
            self._call(self._disconnect(), timeout=10)
        except Exception as e:
            LOG.debug('BLE disconnect failed: %s', e)
        self._stop_loop()
        self.is_open = False

    def _reconfigure_port(self):
        # Baudrate, parity and flow control have no meaning over BLE.
        pass

    @property
    def in_waiting(self):
        with self._rx_cond:
            return len(self._rx)

    def read(self, size=1):
        if not self.is_open:
            raise serial.PortNotOpenError()
        timeout = Timeout(self._timeout)
        with self._rx_cond:
            while len(self._rx) < size and not self._disconnected:
                if timeout.expired():
                    break
                self._rx_cond.wait(timeout.time_left())
            data = bytes(self._rx[:size])
            del self._rx[:size]
        return data

    def write(self, data):
        if not self.is_open:
            raise serial.PortNotOpenError()
        if self._disconnected:
            raise SerialException('Bluetooth LE device disconnected')
        data = bytes(data)
        self._call(self._write(UART_CHAR, data, self.write_with_response),
                   timeout=(self._write_timeout or 0) + 30)
        return len(data)

    def write_characteristic(self, uuid, data, response=True):
        """Write raw bytes to any GATT characteristic of the device."""
        if not self.is_open:
            raise serial.PortNotOpenError()
        self._call(self._write(uuid, bytes(data), response), timeout=10)

    def flush(self):
        # Writes are complete once write() returns.
        pass

    def reset_input_buffer(self):
        with self._rx_cond:
            self._rx.clear()

    def reset_output_buffer(self):
        pass

    # The coroutines below run on the private event loop.

    async def _connect(self):
        from bleak import BleakClient

        device = await self._find_device()
        LOG.info('Connecting to BLE device %s [%s]',
                 device.name, device.address)
        self._client = BleakClient(device,
                                   disconnected_callback=self._on_disconnect,
                                   timeout=CONNECT_TIMEOUT)
        await self._client.connect()
        await self._client.start_notify(UART_CHAR, self._on_notify)

    async def _find_device(self):
        from bleak import BleakScanner

        target = self._target
        if target:
            device = await BleakScanner.find_device_by_filter(
                lambda d, adv: target.lower() in (
                    d.address.lower(), (adv.local_name or '').lower()),
                timeout=SCAN_TIMEOUT)
            if device is None:
                raise SerialException(
                    'Bluetooth LE device %r not found. Is it powered on '
                    'and not connected to another device (e.g. a phone)?' %
                    target)
            return device

        found = {}
        first_seen = asyncio.Event()

        def on_advertisement(device, adv):
            if UART_SERVICE in [u.lower() for u in adv.service_uuids]:
                found[device.address] = (device, adv)
                first_seen.set()

        async with BleakScanner(on_advertisement):
            try:
                await asyncio.wait_for(first_seen.wait(), SCAN_TIMEOUT)
                await asyncio.sleep(SCAN_GRACE)
            except asyncio.TimeoutError:
                pass
        uarts = list(found.values())
        if len(uarts) == 1:
            return uarts[0][0]
        if not uarts:
            raise SerialException(
                'No Bluetooth LE device offering a serial service found. Is '
                'the radio powered on, Bluetooth enabled and not connected '
                'to another device (e.g. a phone)?')
        raise SerialException(
            'Several Bluetooth LE serial devices found, use ble://<address> '
            'to pick one: %s' % ', '.join(
                '%s [%s]' % (adv.local_name or '?', d.address)
                for d, adv in uarts))

    async def _write(self, uuid, data, response):
        for i in range(0, len(data), WRITE_CHUNK):
            await self._client.write_gatt_char(uuid, data[i:i + WRITE_CHUNK],
                                               response=response)

    async def _disconnect(self):
        if self._client is not None and self._client.is_connected:
            await self._client.disconnect()

    # Callbacks from bleak, also on the event loop thread.

    def _on_notify(self, _char, data):
        with self._rx_cond:
            self._rx.extend(data)
            self._rx_cond.notify_all()

    def _on_disconnect(self, _client):
        LOG.debug('BLE device disconnected')
        with self._rx_cond:
            self._disconnected = True
            self._rx_cond.notify_all()

    # Helpers for the calling (pyserial) thread.

    def _call(self, coro, timeout):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise SerialException('Bluetooth LE operation timed out')

    def _stop_loop(self):
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()
        self._loop = None
        self._client = None
