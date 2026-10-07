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

"""Extra pyserial URL handlers (e.g. ble://) usable as CHIRP ports."""

import importlib.util

import serial


def register():
    """Make pyserial's serial_for_url() find the handlers in this package."""
    if __name__ not in serial.protocol_handler_packages:
        serial.protocol_handler_packages.append(__name__)


def has_ble():
    """Whether the optional bleak package for ble:// ports is installed."""
    return importlib.util.find_spec('bleak') is not None
