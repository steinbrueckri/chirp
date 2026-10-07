# Copyright 2026 Richard Steinbrueck
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

"""Published airband frequencies from the German AIP (DFS).

DFS publishes its aeronautical data as AIXM 5.1 datasets, listed in a
catalogue at CATALOGUE_URL. This source downloads the three it needs for
the AIRAC cycle in force, keeps them in CHIRP's config directory until the
next cycle, and turns the frequencies around a position into memories: the
emergency and SAR guard frequencies, the aerodromes inside the radius and
the airspace sectors (FIS, ACC) over and near the position.

AIXM links a frequency to what it serves like this, and only the local
element names are relied on because producers differ in namespaces:

    Unit  <--serviceProvider--  Service  --radioCommunication-->  Channel
                                   |
                                   +-- clientAirport (aerodrome designator)
                                   +-- clientAirspace (airspace UUID)

Since 8.33 kHz spacing a published frequency is a channel *name*, not the
carrier a radio is tuned to (118.510 is carried on 118.508333 MHz), so
carriers are computed and then snapped onto the tuning step of the radio.
"""

import collections
import datetime
import hashlib
import logging
import math
import os
import re
import xml.etree.ElementTree as ET

import requests
import wx

from chirp import chirp_common
from chirp import errors
from chirp import platform
from chirp.sources import base

_ = wx.GetTranslation
LOG = logging.getLogger(__name__)

CATALOGUE_URL = 'https://aip.dfs.de/datasets/rest/'
AIXM_RELEASE = 'AIXM 5.1'
SERVICE_DATASET = 'ED Service'
AERODROME_DATASET = 'ED AirportHeliport'
AIRSPACE_DATASET = 'ED Airspace StrokedBorders'
DATASETS = (SERVICE_DATASET, AERODROME_DATASET, AIRSPACE_DATASET)
# A filename from the catalogue names a file on disk, so it is checked
# rather than trusted: no path separators, no "..", no leading dot.
SAFE_FILENAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*\.xml\Z')
TIMEOUT = (15, 120)

AIRBAND = (108.0, 137.0)
# Fixed by ICAO; DFS does not publish them as frequencies of their own.
GUARD_FREQUENCIES = (
    (121.5, 'EMERGENCY', 'international emergency frequency'),
    (123.1, 'SAR', 'search and rescue'),
)

# AIXM service and unit type codes, by the category written in a name.
SERVICE_TYPES = {
    'ACS': 'ACC', 'ACC': 'ACC',
    'APP': 'APP', 'APP_ARR': 'APP', 'APP-ARR': 'APP', 'APP_DEP': 'APP',
    'APP-DEP': 'APP', 'ARR': 'APP', 'DEP': 'APP',
    'TWR': 'TWR',
    'GND': 'GND', 'SMC': 'GND', 'SMGCS': 'GND', 'APRON': 'GND',
    'DEL': 'DEL', 'CLD': 'DEL',
    'AFIS': 'AFIS',
    'FIS': 'FIS', 'FISA': 'FIS', 'FIS_FISA': 'FIS',
    'INFO': 'INFO', 'INF': 'INFO',
    'ATIS': 'ATIS',
    'RADAR': 'RADAR', 'RAD': 'RADAR',
}
UNIT_TYPES = {
    'ACC': 'ACC', 'APP': 'APP', 'TWR': 'TWR', 'AFIS': 'AFIS', 'FIS': 'FIS',
    'FSS': 'FIS', 'ATIS': 'ATIS',
}
# Order the frequencies of one aerodrome are listed in.
AERODROME_ORDER = ('TWR', 'AFIS', 'INFO', 'APP', 'GND', 'DEL', 'ATIS')
# How far the ground station of a service is realistically audible; None
# means anywhere inside the search radius. An apron frequency 60 km away is
# not a weak result but an unusable memory.
GROUND_STATION_RANGE_KM = {
    'DEL': 25.0, 'GND': 25.0, 'TWR': 45.0, 'AFIS': 45.0, 'ATIS': 60.0,
    'INFO': 60.0, 'APP': 90.0, 'FIS': None, 'ACC': None, 'RADAR': None,
    '': 60.0,
}
ENROUTE_CATEGORIES = ('ACC', 'RADAR')
# An ATIS is a loop that never goes quiet and would stop every scan.
SKIP_WHILE_SCANNING = ('ATIS',)

# 8.33 kHz channel names inside a 25 kHz block, and their carrier offsets
# in Hz. Truncated, not rounded: CHIRP recognises an 8.33 kHz frequency by
# exactly these remainders (see chirp_common.is_8_33).
CHANNEL_BLOCK_KHZ = 25
CHANNEL_OFFSETS_HZ = {0: 0, 5: 0, 10: 8333, 15: 16666}
NARROW_STEP_KHZ = 8.33
# Airband AM is received in a passband several kHz wide, so a carrier
# missed by up to half a 5 kHz step still sounds right; beyond that the
# radio sits on the neighbouring channel.
MAX_DETUNING_HZ = 2500
TUNING_STEPS_KHZ = (8.33, 5.0, 25.0)

EARTH_RADIUS_KM = 6371.0088
KM_PER_NM = 1.852
FEET_PER_METRE = 1 / 0.3048
# Resolution for turning published arcs and circles into polygons; at a
# 30 NM radius the chord error stays well below 10 m.
ARC_STEP_DEG = 2.0
# Country-sized class airspaces contain every position and say nothing
# about which sector is talking, so they are left out.
BROAD_AIRSPACE_KM2 = 300000.0
# Kinds whose published name is an internal code rather than a place
# ("SECTOR LANGEN GGCIDV"); their channels are named after the callsign.
UNNAMED_AIRSPACE_KINDS = ('FLIGHT INFORMATION SECTOR',)
MIN_NAME_LENGTH = 6
# Below this a place name is not worth spelling out next to a category.
MIN_PLACE_LENGTH = 6

Aerodrome = collections.namedtuple(
    'Aerodrome', 'key icao name latitude longitude')
Frequency = collections.namedtuple(
    'Frequency', 'mhz callsign unit category aerodromes airspaces')
Airspace = collections.namedtuple(
    'Airspace', 'uuid identifier name kind volumes')
# rings: polygons as closed lists of (lat, lon); bbox: (min_lat, min_lon,
# max_lat, max_lon) over all rings.
Volume = collections.namedtuple('Volume', 'rings lower_ft bbox')


class DFSRadio(base.NetworkResultRadio):
    VENDOR = 'DFS'

    def get_label(self):
        return 'DFS'

    def do_fetch(self, status, params):
        try:
            paths = fetch_release(status)
            status.send_status(_('Reading aerodromes'), 70)
            aerodromes = parse_aerodromes(paths[AERODROME_DATASET])
            status.send_status(_('Reading frequencies'), 75)
            frequencies = parse_frequencies(paths[SERVICE_DATASET])
            status.send_status(_('Reading airspaces'), 80)
            airspaces = parse_airspaces(paths[AIRSPACE_DATASET])
        except (errors.RadioError, OSError, ET.ParseError,
                requests.exceptions.RequestException) as e:
            LOG.exception('DFS query failed')
            status.send_fail(str(e))
            return

        self._memories = build_memories(
            aerodromes, frequencies,
            (params['lat'], params['lon']), params['radius'],
            params['step'], params['name_length'], airspaces)
        if len(self._memories) <= len(GUARD_FREQUENCIES):
            status.send_fail(_('No frequencies found in this area'))
            return
        status.send_end()


# -- the DFS catalogue and the local copy of a release ---------------------

def fetch_release(status, today=None):
    """Make sure the datasets in force are in the cache, return their paths.

    Files are named after their AIRAC cycle, so a cached file is reused
    until DFS publishes a new cycle; files of older cycles are removed.
    """
    status.send_status(_('Checking DFS catalogue'), 5)
    r = requests.get(CATALOGUE_URL, headers=base.HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    datasets = choose_datasets(r.json(), today or datetime.date.today())

    cache_dir = platform.get_platform().config_file('dfs')
    os.makedirs(cache_dir, exist_ok=True)
    paths = {}
    for index, (name, (url, filename, checksum)) in enumerate(
            sorted(datasets.items())):
        path = os.path.join(cache_dir, filename)
        if not os.path.exists(path):
            status.send_status(_('Downloading %s') % name, 10 + 20 * index)
            _download(url, path, checksum)
        paths[name] = path

    for filename in os.listdir(cache_dir):
        if filename not in [os.path.basename(p) for p in paths.values()]:
            os.remove(os.path.join(cache_dir, filename))
    return paths


def choose_datasets(catalogue, today):
    """The datasets this source reads, of the AIRAC cycle in force.

    That is the amendment with the latest AIRAC date not in the future:
    DFS lists the next cycles weeks early, and a daily snapshot without an
    AIRAC date. Returns {dataset name: (url, filename, sha512 or None)}.
    """
    in_force = []
    for amendment in catalogue.get('Amdts') or []:
        metadata = amendment.get('Metadata') or {}
        airac = _parse_iso_date(metadata.get('airac'))
        if airac and airac <= today and isinstance(amendment.get('Amdt'),
                                                   int):
            in_force.append((airac, amendment))
    if not in_force:
        raise errors.RadioError('No AIRAC cycle in force in the DFS catalogue')
    airac, amendment = max(in_force, key=lambda item: item[0])

    leaves = {leaf.get('name'): leaf
              for leaf in _leaves(amendment['Metadata'].get('datasets'))}
    datasets = {}
    for name in DATASETS:
        for release in (leaves.get(name) or {}).get('releases') or []:
            filename = release.get('filename')
            if (release.get('type') == AIXM_RELEASE and
                    isinstance(filename, str) and
                    SAFE_FILENAME.match(filename)):
                checksum = release.get('checksum') or {}
                datasets[name] = (
                    '%s%i/%s' % (CATALOGUE_URL, amendment['Amdt'], filename),
                    filename,
                    checksum.get('value')
                    if checksum.get('type') == 'sha512' else None)
                break
        else:
            raise errors.RadioError(
                'DFS cycle %s offers no %s dataset' % (airac, name))
    LOG.info('Using DFS amendment %s (AIRAC %s)', amendment['Amdt'], airac)
    return datasets


def _leaves(items):
    """Walk the catalogue's group/leaf tree, which nests arbitrarily."""
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get('type') == 'group':
            yield from _leaves(item.get('items'))
        else:
            yield item


def _download(url, path, checksum):
    """Download to a temporary file and only keep it once it verified."""
    partial = path + '.part'
    digest = hashlib.sha512()
    with requests.get(url, headers=base.HEADERS, timeout=TIMEOUT,
                      stream=True) as r:
        r.raise_for_status()
        with open(partial, 'wb') as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
                digest.update(chunk)
    if checksum and digest.hexdigest().lower() != checksum.lower():
        os.remove(partial)
        raise errors.RadioError('Checksum mismatch downloading %s' % url)
    os.replace(partial, path)


def _parse_iso_date(text):
    try:
        return datetime.date.fromisoformat(text)
    except (TypeError, ValueError):
        return None


# -- reading AIXM -----------------------------------------------------------

XSI_NIL = '{http://www.w3.org/2001/XMLSchema-instance}nil'
XLINK_HREF = '{http://www.w3.org/1999/xlink}href'
XLINK_TITLE = '{http://www.w3.org/1999/xlink}title'


def _local(element):
    return element.tag.rsplit('}', 1)[-1]


def _children(element, *names):
    if element is None:
        return []
    return [child for child in element if _local(child) in names]


def _child(element, *names):
    found = _children(element, *names)
    return found[0] if found else None


def _value(element):
    """Text of an element, honouring xsi:nil and collapsing whitespace."""
    if element is None or element.get(XSI_NIL, '').lower() == 'true':
        return None
    return ' '.join((element.text or '').split()) or None


def _child_value(element, *names):
    return _value(_child(element, *names))


def _href_uuid(element):
    href = element.get(XLINK_HREF) if element is not None else None
    return href.rsplit(':', 1)[-1].strip() if href else None


def _href_title(element):
    title = element.get(XLINK_TITLE) if element is not None else None
    return title.strip() if title else None


def _features(path, names):
    """Stream (uuid, time slice) of the features with these local names.

    The datasets are tens of megabytes, so parsed elements are dropped as
    soon as they have been handed out. A BASELINE slice is preferred over
    the PERMDELTA slices a revision may carry.
    """
    context = ET.iterparse(path, events=('start', 'end'))
    _event, root = next(context)
    for event, element in context:
        if event != 'end' or _local(element) not in names:
            continue
        slices = [candidate
                  for wrapper in _children(element, 'timeSlice')
                  for candidate in wrapper]
        baseline = [s for s in slices
                    if (_child_value(s, 'interpretation') or '').upper() ==
                    'BASELINE']
        uuid = _child_value(element, 'identifier')
        if uuid and slices:
            uuid = uuid.rsplit(':', 1)[-1]
            yield _local(element), uuid, (baseline or slices)[0]
        root.clear()


def parse_aerodromes(path):
    aerodromes = []
    for _name, _uuid, slice_ in _features(path, {'AirportHeliport'}):
        position = None
        for element in slice_.iter():
            if _local(element) == 'pos' and _value(element):
                try:
                    lat, lon = (float(x) for x in _value(element).split())
                    position = (lat, lon)
                except ValueError:
                    pass
                break
        icao = _child_value(slice_, 'locationIndicatorICAO')
        designator = _child_value(slice_, 'designator')
        key = icao or designator
        if position is None or not key:
            continue
        aerodromes.append(Aerodrome(
            key=key, icao=icao,
            name=_child_value(slice_, 'name') or key,
            latitude=position[0], longitude=position[1]))
    return aerodromes


def parse_frequencies(path):
    """Published frequencies, with the aerodromes and airspaces served."""
    units = {}
    channels = {}
    services = []
    for name, uuid, slice_ in _features(
            path, {'Unit', 'RadioCommunicationChannel',
                   'AirTrafficControlService', 'GroundTrafficControlService',
                   'InformationService', 'SearchRescueService'}):
        if name == 'Unit':
            units[uuid] = (_child_value(slice_, 'name'),
                           _child_value(slice_, 'type'),
                           _href_title(_child(slice_, 'airportLocation')))
        elif name == 'RadioCommunicationChannel':
            channels[uuid] = _channel_mhz(slice_)
        else:
            services.append(slice_)

    frequencies = []
    for slice_ in services:
        unit_name, unit_type, unit_airport = units.get(
            _href_uuid(_child(slice_, 'serviceProvider')) or '',
            (None, None, None))
        aerodromes = {_href_title(a) for a in
                      _children(slice_, 'clientAirport', 'clientAerodrome')}
        aerodromes.discard(None)
        if not aerodromes and unit_airport:
            aerodromes = {unit_airport}
        airspaces = {_href_uuid(a)
                     for a in _children(slice_, 'clientAirspace')}
        airspaces.discard(None)
        category = _category(_child_value(slice_, 'type'), unit_type)
        callsign = _callsign(slice_)
        unit = unit_name or _child_value(slice_, 'name')
        for radio in _children(slice_, 'radioCommunication'):
            mhz = channels.get(_href_uuid(radio))
            if mhz is not None:
                frequencies.append(Frequency(
                    mhz, callsign, unit, category, frozenset(aerodromes),
                    frozenset(airspaces)))
    return frequencies


def _channel_mhz(slice_):
    element = _child(slice_, 'frequencyTransmission')
    if not _value(element):
        element = _child(slice_, 'frequencyReception')
    try:
        mhz = float((_value(element) or '').replace(',', '.'))
    except ValueError:
        return None
    unit = (element.get('uom') or 'MHZ').upper()
    mhz *= {'KHZ': 1e-3, 'HZ': 1e-6, 'GHZ': 1e3}.get(unit, 1)
    return round(mhz, 4) if mhz > 0 else None


def _category(service_type, unit_type):
    code = (service_type or '').upper().split(':')[-1]
    return (SERVICE_TYPES.get(code) or
            UNIT_TYPES.get((unit_type or '').upper()) or '')


def _callsign(slice_):
    """The English callsign, which is what is spoken for ATC services."""
    callsigns = {}
    for holder in _children(slice_, 'call-sign', 'callSign'):
        detail = holder
        for element in holder.iter():
            if _local(element) == 'CallsignDetail':
                detail = element
                break
        text = _child_value(detail, 'callSign')
        if text:
            language = (_child_value(detail, 'language') or '').lower()
            callsigns.setdefault(language, text)
    for language in ('eng', 'ger'):
        if language in callsigns:
            return callsigns[language]
    return next(iter(callsigns.values()), None)


def parse_airspaces(path):
    """Airspaces with concrete volumes, by UUID.

    Sectors are often aggregations of other airspaces (contributors joined
    with BASE/UNION), so everything is read first and resolved after.
    """
    raw = {}
    for _name, uuid, slice_ in _features(path, {'Airspace'}):
        components = []
        for index, component in enumerate(
                _children(slice_, 'geometryComponent')):
            holder = _descendant(component, 'AirspaceGeometryComponent')
            if holder is None:
                holder = component
            volume = _descendant(holder, 'AirspaceVolume')
            if volume is None:
                continue
            sequence = _child_value(holder, 'operationSequence') or ''
            lower = _child(volume, 'lowerLimit')
            components.append((
                int(sequence) if sequence.isdigit() else index,
                (_child_value(holder, 'operation') or 'BASE').upper(),
                _surface_rings(_child(volume, 'horizontalProjection')),
                _limit_ft(lower),
                _value(lower) is not None or
                _child_value(volume, 'upperLimit') is not None,
                [_href_uuid(_child(dependency, 'theAirspace'))
                 for dependency in volume.iter()
                 if _local(dependency) == 'AirspaceVolumeDependency']))
        raw[uuid] = (
            _child_value(slice_, 'designator') or
            _child_value(slice_, 'designatorICAO') or
            _child_value(slice_, 'name') or uuid,
            _child_value(slice_, 'name'),
            _child_value(slice_, 'localType') or _child_value(slice_, 'type'),
            sorted(components, key=lambda c: c[0]))

    airspaces = {}
    for uuid, (identifier, name, kind, _components) in raw.items():
        volumes = [Volume(rings, lower_ft, _bbox(rings))
                   for rings, lower_ft in _resolve(uuid, raw, set())]
        if volumes:
            airspaces[uuid] = Airspace(uuid, identifier, name, kind, volumes)
    return airspaces


def _resolve(uuid, raw, stack):
    """(rings, lower_ft) volumes of an airspace, following contributors.

    SUBTR and INTERS would need polygon clipping; DFS does not use them,
    so they are reported and skipped, which can only make an airspace
    larger. The stack guards against cyclic references.
    """
    if uuid in stack or uuid not in raw:
        return []
    stack = stack | {uuid}
    volumes = []
    for component in raw[uuid][3]:
        _seq, operation, rings, lower_ft, has_limits, contributors = component
        if operation not in ('BASE', 'UNION'):
            LOG.warning('Ignoring %s operation in airspace %s',
                        operation, raw[uuid][0])
            continue
        if not contributors:
            if rings:
                volumes.append((rings, lower_ft))
            continue
        for contributor in contributors:
            for part_rings, part_lower in _resolve(contributor, raw, stack):
                # This component's own limits win over the contributor's.
                volumes.append((part_rings,
                                lower_ft if has_limits else part_lower))
    return volumes


def _descendant(element, *names):
    for candidate in element.iter():
        if candidate is not element and _local(candidate) in names:
            return candidate
    return None


def _limit_ft(element):
    """A lower limit in feet; GND/SFC is 0, unknown is None."""
    text = (_value(element) or '').upper()
    if text in ('GND', 'SFC'):
        return 0.0
    try:
        number = float(text.replace(',', '.'))
    except ValueError:
        return None
    unit = (element.get('uom') or 'FT').upper()
    return number * {'FL': 100.0, 'M': FEET_PER_METRE}.get(unit, 1.0)


def _surface_rings(element):
    """Exterior rings of a horizontal projection, as closed (lat, lon) lists.

    Interior rings are not read: DFS publishes none.
    """
    rings = []
    if element is None:
        return rings
    for patch in element.iter():
        if _local(patch) not in ('PolygonPatch', 'Polygon'):
            continue
        exterior = _child(patch, 'exterior')
        points = _ring_points(exterior) if exterior is not None else []
        if len(points) >= 3:
            if points[0] != points[-1]:
                points.append(points[0])
            rings.append(points)
    return rings


def _ring_points(ring):
    """Walk the curve segments of a GML ring in document order."""
    points = []
    for holder in ring.iter():
        if _local(holder) != 'segments':
            continue
        for segment in holder:
            addition = _segment_points(segment)
            if points and addition and _same_point(points[-1], addition[0]):
                addition = addition[1:]
            points.extend(addition)
    if not points:
        # Some producers put coordinates straight into a LinearRing.
        for element in ring.iter():
            points.extend(_positions(element))
    return points


def _segment_points(segment):
    positions = []
    for element in segment:
        positions.extend(_positions(element))
    name = _local(segment)
    if name in ('GeodesicString', 'LineStringSegment', 'Geodesic'):
        return positions
    radius = _radius_km(_child(segment, 'radius'))
    if not positions or radius is None:
        return []
    lat, lon = positions[0]
    if name == 'CircleByCenterPoint':
        return _arc_points(lat, lon, radius, 0.0, 360.0)
    if name == 'ArcByCenterPoint':
        try:
            start = float(_child_value(segment, 'startAngle'))
            end = float(_child_value(segment, 'endAngle'))
        except (TypeError, ValueError):
            return []
        return _arc_points(lat, lon, radius, start, end)
    return []


def _positions(element):
    """(lat, lon) pairs of a gml:pos or gml:posList element."""
    if _local(element) not in ('pos', 'posList'):
        return []
    numbers = (_value(element) or '').split()
    try:
        return [(float(numbers[i]), float(numbers[i + 1]))
                for i in range(0, len(numbers) - 1, 2)]
    except ValueError:
        return []


def _radius_km(element):
    try:
        number = float(_value(element))
    except (TypeError, ValueError):
        return None
    unit = (element.get('uom') or '').strip('[] ').upper()
    return number * {'KM': 1.0, 'M': 0.001,
                     'FT': 0.0003048}.get(unit, KM_PER_NM)


def _same_point(a, b):
    return abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) < 1e-6


def _arc_points(lat, lon, radius_km, start, end):
    """Points along an arc, clockwise from start to end bearing.

    Bearings are true, from north, which is how DFS publishes
    gml:ArcByCenterPoint; a full sweep gives a circle.
    """
    sweep = (end - start) % 360.0 or 360.0
    steps = max(2, math.ceil(sweep / ARC_STEP_DEG))
    return [_destination(lat, lon, start + sweep * i / steps, radius_km)
            for i in range(steps + 1)]


def _destination(lat, lon, bearing, distance):
    angular = distance / EARTH_RADIUS_KM
    brg = math.radians(bearing)
    phi1, lambda1 = math.radians(lat), math.radians(lon)
    sin_phi2 = (math.sin(phi1) * math.cos(angular) +
                math.cos(phi1) * math.sin(angular) * math.cos(brg))
    phi2 = math.asin(sin_phi2)
    lambda2 = lambda1 + math.atan2(
        math.sin(brg) * math.sin(angular) * math.cos(phi1),
        math.cos(angular) - math.sin(phi1) * sin_phi2)
    return math.degrees(phi2), (math.degrees(lambda2) + 540.0) % 360.0 - 180.0


def _bbox(rings):
    lats = [lat for ring in rings for lat, _lon in ring]
    lons = [lon for ring in rings for _lat, lon in ring]
    return min(lats), min(lons), max(lats), max(lons)


def _overlaps(a, b):
    """Whether two (min_lat, min_lon, max_lat, max_lon) boxes overlap."""
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _contains(ring, lat, lon):
    """Whether a closed (lat, lon) ring contains the point (ray casting)."""
    inside = False
    for (lat1, lon1), (lat2, lon2) in zip(ring, ring[1:]):
        if (lat1 > lat) != (lat2 > lat):
            crossing = lon1 + (lat - lat1) * (lon2 - lon1) / (lat2 - lat1)
            if lon < crossing:
                inside = not inside
    return inside


def _area_km2(ring):
    """Rough area of a ring; enough to tell a sector from a country."""
    twice = sum(lon1 * lat2 - lon2 * lat1
                for (lat1, lon1), (lat2, lon2) in zip(ring, ring[1:]))
    latitude = sum(lat for lat, _lon in ring) / len(ring)
    return abs(twice) / 2 * 111.32 * 111.32 * math.cos(math.radians(latitude))


# -- frequencies to memories --------------------------------------------------

def carrier_hz(channel_mhz):
    """The carrier a published channel name is tuned on, or None."""
    khz = round(channel_mhz * 1000.0, 3)
    block = math.floor(khz / CHANNEL_BLOCK_KHZ) * CHANNEL_BLOCK_KHZ
    offset = CHANNEL_OFFSETS_HZ.get(round(khz - block))
    if offset is None:
        return None
    return round(block * 1000) + offset


def tuned_hz(carrier, step_khz):
    """What a radio stepping in step_khz is set to for this carrier."""
    if math.isclose(step_khz, NARROW_STEP_KHZ):
        return carrier
    step = round(step_khz * 1000)
    return round(carrier / step) * step


def distance_km(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((phi2 - phi1) / 2) ** 2 + math.cos(phi1) * math.cos(phi2) *
         math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def build_memories(aerodromes, frequencies, position, radius_km,
                   step_khz=NARROW_STEP_KHZ, name_length=12, airspaces=None):
    """Memories in the order a listener works them.

    Guard frequencies, the nearest aerodrome, the sectors overhead, the
    other aerodromes in the radius nearest first, then sectors nearby. A
    frequency is listed once, where it first comes up.
    """
    name_length = max(MIN_NAME_LENGTH, name_length)
    nearby = [(d, a) for d, a in sorted(
        (distance_km(position[0], position[1], a.latitude, a.longitude), a)
        for a in aerodromes) if d <= radius_km]
    overhead, around = _airspace_candidates(
        airspaces or {}, frequencies, position, radius_km, name_length)
    # Enroute control is better described by its sector than by an
    # aerodrome it also serves, so those stay with the sectors.
    enroute = {mhz for mhz, _name, category, _note in overhead + around
               if category in ENROUTE_CATEGORIES}
    served = [f for f in frequencies if f.mhz not in enroute]

    candidates = [(mhz, name, '', comment)
                  for mhz, name, comment in GUARD_FREQUENCIES]
    candidates += _aerodrome_candidates(nearby[:1], served, name_length)
    candidates += overhead
    candidates += _aerodrome_candidates(nearby[1:], served, name_length)
    candidates += around

    memories = []
    seen = set()
    names = set()
    for mhz, name, category, note in candidates:
        if not AIRBAND[0] <= mhz <= AIRBAND[1] or mhz in seen:
            continue
        seen.add(mhz)
        carrier = carrier_hz(mhz)
        if carrier is None:
            LOG.debug('Skipping %.3f: not a valid channel name', mhz)
            continue
        tuned = tuned_hz(carrier, step_khz)
        if abs(tuned - carrier) > MAX_DETUNING_HZ:
            LOG.debug('Skipping %.3f: out of reach in %s kHz steps',
                      mhz, step_khz)
            continue
        narrow = carrier % (CHANNEL_BLOCK_KHZ * 1000) != 0

        mem = chirp_common.Memory()
        mem.number = len(memories) + 1
        mem.freq = tuned
        mem.name = _unique(name[:name_length], names, name_length)
        names.add(mem.name)
        mem.mode = 'AM'
        mem.tuning_step = step_khz if narrow else float(CHANNEL_BLOCK_KHZ)
        mem.skip = 'S' if category in SKIP_WHILE_SCANNING else ''
        comment = [note, 'ch %.3f' % mhz]
        if narrow:
            comment.append('8.33 kHz')
        if tuned != carrier:
            comment.append('carrier %s' % chirp_common.format_freq(carrier))
        mem.comment = ' | '.join(comment)
        memories.append(mem)
    return memories


def _aerodrome_candidates(nearby, frequencies, name_length):
    """(mhz, name, category, note) of these aerodromes, in that order."""
    candidates = []
    for distance, aerodrome in nearby:
        place = aerodrome.icao or aerodrome.name
        served = [f for f in frequencies if aerodrome.key in f.aerodromes]
        for frequency in sorted(served, key=_aerodrome_order):
            reach = GROUND_STATION_RANGE_KM.get(frequency.category, 60.0)
            if reach is not None and distance > reach:
                continue
            spoken = (frequency.callsign or
                      '%s %s' % (aerodrome.name, frequency.category))
            candidates.append((
                frequency.mhz,
                _compose(place, frequency.category, name_length, keep=True),
                frequency.category,
                '%s, %s %s, %.0f km' % (spoken, aerodrome.key,
                                        aerodrome.name, distance)))
    return candidates


def _airspace_candidates(airspaces, frequencies, position, radius_km,
                         name_length):
    """Sector frequencies, as two lists: overhead and merely nearby.

    Within each, information services come first, then the sectors from
    the ground up: a sector starting at FL245 only carries traffic that is
    already high and far away. A frequency serving several airspaces is
    described by the narrowest one.
    """
    lat, lon = position
    pad_lat = radius_km / 111.32
    pad_lon = pad_lat / max(0.01, math.cos(math.radians(lat)))
    box = (lat - pad_lat, lon - pad_lon, lat + pad_lat, lon + pad_lon)

    # Per airspace in reach: (overhead, area, floor)
    reach = {}
    for uuid, airspace in airspaces.items():
        for volume in airspace.volumes:
            if not _overlaps(volume.bbox, box):
                continue
            area = sum(_area_km2(ring) for ring in volume.rings)
            if area > BROAD_AIRSPACE_KM2:
                continue
            is_over = any(_contains(ring, lat, lon) for ring in volume.rings)
            was_over, was_area, was_floor = reach.get(
                uuid, (False, area, float('inf')))
            reach[uuid] = (was_over or is_over, min(was_area, area),
                           min(was_floor, volume.lower_ft or 0.0))

    ordered = []
    for frequency in frequencies:
        linked = [uuid for uuid in frequency.airspaces if uuid in reach]
        if not linked:
            continue
        uuid = min(linked, key=lambda u: reach[u][1])
        airspace = airspaces[uuid]
        is_over = any(reach[u][0] for u in linked)
        place = _place_name(airspace)
        spoken = frequency.callsign or frequency.unit or airspace.identifier
        where = 'overhead' if is_over else 'nearby'
        if (place or airspace.identifier).upper() in spoken.upper():
            note = '%s %s' % (spoken, where)
        else:
            note = '%s, %s %s' % (spoken, place or airspace.identifier,
                                  where)
        name = _compose(place or _first_word(spoken), frequency.category,
                        name_length)
        information = frequency.category in ('FIS', 'INFO')
        ordered.append(((0 if information else 1, reach[uuid][2],
                         frequency.mhz),
                        is_over,
                        (frequency.mhz, name, frequency.category, note)))
    ordered.sort(key=lambda item: item[0])
    return ([c for _key, over, c in ordered if over],
            [c for _key, over, c in ordered if not over])


def _place_name(airspace):
    """The place a sector is named after, or None if it has none.

    133.230 is MUENCHEN RADAR working the GERA sector, and GERA is what is
    said on the frequency and printed on the chart; EDMMGER is neither.
    """
    name = airspace.name
    if not name or (airspace.kind or '').upper() in UNNAMED_AIRSPACE_KINDS:
        return None
    if name.startswith('SECTOR '):
        name = name[len('SECTOR '):]
    name = name.strip()
    # Volumes without a name of their own repeat their identifier.
    if not name or name.upper() == airspace.identifier.upper():
        return None
    return name


def _first_word(text):
    text = ''.join(c for c in text or '' if c.isalnum() or c == ' ')
    return (text.split() or [''])[0].upper()


def _aerodrome_order(frequency):
    category = frequency.category
    rank = (AERODROME_ORDER.index(category) if category in AERODROME_ORDER
            else len(AERODROME_ORDER))
    return rank, frequency.mhz


def _compose(place, category, length, keep=False):
    """'PLACE CAT' in length characters, as the display shows it.

    The category is never cut in half. On a narrow display a sector gives
    the whole width to its name (there is only one GERA), while an
    aerodrome (keep) holds on to an initial of it, since its tower and its
    ATIS differ only there.
    """
    if not category:
        return _place_token(place, length) or 'FREQ'
    room = length - len(category) - 1
    if room >= MIN_PLACE_LENGTH or (place and len(place) <= room):
        token = _place_token(place, room)
        return ('%s %s' % (token, category) if token else category)[:length]
    if not keep:
        return _place_token(place, length) or category[:length]
    token = _place_token(place, length - 2)
    return ('%s-%s' % (token, category[0]) if token else category)[:length]


def _place_token(place, room):
    """A place name in room characters, keeping what tells places apart.

    Words after the first shrink to their initial, numbers stay whole:
    'THUERINGEN LOW SUED' and '... NORD' must not both become 'THUERINGEN'.
    """
    words = [''.join(c for c in word if c.isalnum())
             for word in (place or '').split()]
    words = [word for word in words if word]
    if not words or room <= 0:
        return None
    whole = ' '.join(words).upper()
    if len(words) == 1 or len(whole) <= room:
        return whole[:room]
    tail = ''.join(w if w.isdigit() else w[0] for w in words[1:])
    head = words[0][:max(1, room - 1 - len(tail))]
    return ('%s %s' % (head, tail))[:room].upper()


def _unique(name, taken, length):
    """The name, or the name with a counter, so no two memories read alike."""
    if name not in taken:
        return name
    for counter in range(2, 100):
        suffix = ' %i' % counter
        candidate = name[:length - len(suffix)].rstrip() + suffix
        if candidate not in taken:
            return candidate
    return name
