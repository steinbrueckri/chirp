import datetime
import hashlib
import os
import tempfile
import unittest
from unittest import mock

from chirp import errors
from chirp.sources import dfs

# Invented data in the structure of the DFS AIXM datasets; see the comment
# at the top of each file.
FIXTURES = os.path.join(os.path.dirname(__file__), 'dfs')
SERVICE = os.path.join(FIXTURES, 'ED_Service_fixture.xml')
AERODROMES = os.path.join(FIXTURES, 'ED_AirportHeliport_fixture.xml')
AIRSPACES = os.path.join(FIXTURES, 'ED_Airspace_fixture.xml')
TODAY = datetime.date(2026, 8, 20)


def catalogue_entry(number, airac, filename_suffix='revision'):
    def leaf(name, prefix):
        filename = '%s_%s.xml' % (prefix, filename_suffix)
        return {'name': name, 'releases': [
            {'type': 'XLSX', 'filename': prefix + '.xlsx'},
            {'type': 'AIXM 5.1', 'filename': filename,
             'checksum': {'type': 'sha512', 'value': 'ab' * 64}}]}
    metadata = {'datasets': [{'type': 'group', 'items': [
        leaf('ED Service', 'ED_Service_%i' % number),
        {'type': 'group', 'items': [
            leaf('ED AirportHeliport', 'ED_AirportHeliport_%i' % number),
            leaf('ED Airspace StrokedBorders', 'ED_Airspace_%i' % number)]},
    ]}]}
    if airac:
        metadata['airac'] = airac
    return {'Amdt': number, 'Metadata': metadata}


class TestCatalogue(unittest.TestCase):
    def test_picks_cycle_in_force(self):
        catalogue = {'Amdts': [
            catalogue_entry(1, '2026-09-03'),  # published early, not yet
            catalogue_entry(0, '2026-08-06'),
            catalogue_entry(9999, None, 'snapshot'),  # no AIRAC date
        ]}
        datasets = dfs.choose_datasets(catalogue, TODAY)
        url, filename, checksum = datasets['ED Service']
        self.assertEqual('ED_Service_0_revision.xml', filename)
        self.assertEqual(dfs.CATALOGUE_URL + '0/ED_Service_0_revision.xml',
                         url)
        self.assertEqual('ab' * 64, checksum)
        self.assertEqual('ED_AirportHeliport_0_revision.xml',
                         datasets['ED AirportHeliport'][1])

    def test_nothing_in_force(self):
        catalogue = {'Amdts': [catalogue_entry(1, '2026-09-03')]}
        self.assertRaises(errors.RadioError,
                          dfs.choose_datasets, catalogue, TODAY)

    def test_rejects_unsafe_filename(self):
        catalogue = {'Amdts': [catalogue_entry(0, '2026-08-06',
                                               '../../evil')]}
        self.assertRaises(errors.RadioError,
                          dfs.choose_datasets, catalogue, TODAY)


class TestFetch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.content = b'<aixm/>'
        entry = catalogue_entry(0, '2026-08-06')
        for leaf in dfs._leaves(entry['Metadata']['datasets']):
            leaf['releases'][1]['checksum']['value'] = hashlib.sha512(
                self.content).hexdigest()
        self.catalogue = {'Amdts': [entry]}

    def _fetch(self, content=None):
        def get(url, **kwargs):
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.json.return_value = self.catalogue
            response.iter_content.return_value = [content or self.content]
            return response

        platform = mock.MagicMock()
        platform.config_file.return_value = self.tmp.name
        with mock.patch.object(dfs.requests, 'get', side_effect=get) as g, \
                mock.patch.object(dfs.platform, 'get_platform',
                                  return_value=platform):
            paths = dfs.fetch_release(mock.MagicMock(), TODAY)
        return paths, g

    def test_downloads_then_reuses(self):
        stale = os.path.join(self.tmp.name, 'ED_Service_old_revision.xml')
        open(stale, 'w').close()

        paths, get = self._fetch()
        self.assertEqual(4, get.call_count)  # catalogue + three datasets
        with open(paths['ED Service'], 'rb') as f:
            self.assertEqual(self.content, f.read())
        self.assertFalse(os.path.exists(stale))

        paths, get = self._fetch()
        self.assertEqual(1, get.call_count)  # catalogue only

    def test_checksum_mismatch(self):
        self.assertRaises(errors.RadioError, self._fetch, b'tampered')
        self.assertEqual([], os.listdir(self.tmp.name))


class TestAIXM(unittest.TestCase):
    def test_aerodromes(self):
        aerodromes = {a.key: a for a in dfs.parse_aerodromes(AERODROMES)}
        self.assertEqual({'ZZBR', 'ZZHB', 'ZZ0155'}, set(aerodromes))
        self.assertEqual((51.05, 11.0), (aerodromes['ZZBR'].latitude,
                                         aerodromes['ZZBR'].longitude))
        # No ICAO indicator: linked by designator, named by its name
        self.assertIsNone(aerodromes['ZZ0155'].icao)
        self.assertEqual('ESGAROTH', aerodromes['ZZ0155'].name)

    def test_frequencies(self):
        frequencies = {f.mhz: f for f in dfs.parse_frequencies(SERVICE)}
        tower = frequencies[118.505]
        self.assertEqual('BREE TOWER', tower.callsign)  # English preferred
        self.assertEqual('TWR', tower.category)
        self.assertEqual({'ZZBR'}, tower.aerodromes)
        # Linked through the unit's airport, not the service itself
        self.assertEqual({'ZZBR'}, frequencies[122.505].aerodromes)
        self.assertEqual('ATIS', frequencies[122.505].category)
        # Type code ACS is enroute control
        self.assertEqual('ACC', frequencies[128.005].category)


class TestMemories(unittest.TestCase):
    def setUp(self):
        self.aerodromes = dfs.parse_aerodromes(AERODROMES)
        self.frequencies = dfs.parse_frequencies(SERVICE)

    def build(self, **kwargs):
        args = dict(position=(51.0, 10.9), radius_km=50)
        args.update(kwargs)
        return dfs.build_memories(self.aerodromes, self.frequencies, **args)

    def test_order_and_content(self):
        memories = self.build()
        self.assertEqual(['EMERGENCY', 'SAR', 'ZZBR TWR', 'ZZBR ATIS',
                          'ESGAROT INFO', 'ZZHB AFIS'],
                         [m.name for m in memories])
        self.assertEqual(list(range(1, 7)), [m.number for m in memories])
        self.assertTrue(all(m.mode == 'AM' for m in memories))
        atis = memories[3]
        self.assertEqual('S', atis.skip)
        self.assertEqual(122500000, atis.freq)
        self.assertIn('ZZBR BREE, 9 km', atis.comment)

    def test_radius(self):
        names = [m.name for m in self.build(radius_km=15)]
        self.assertEqual(['EMERGENCY', 'SAR', 'ZZBR TWR', 'ZZBR ATIS'], names)

    def test_tuning_steps(self):
        afis = self.build(step_khz=8.33)[-1]
        self.assertEqual(118908333, afis.freq)
        self.assertEqual(8.33, afis.tuning_step)
        afis = self.build(step_khz=5.0)[-1]
        self.assertEqual(118910000, afis.freq)
        self.assertIn('carrier 118.908333', afis.comment)
        # 25 kHz would land on the neighbouring channel
        self.assertNotIn('ZZHB AFIS',
                         [m.name for m in self.build(step_khz=25.0)])

    def test_carrier(self):
        self.assertEqual(118500000, dfs.carrier_hz(118.505))
        self.assertEqual(118508333, dfs.carrier_hz(118.510))
        self.assertEqual(118516666, dfs.carrier_hz(118.515))
        self.assertEqual(118525000, dfs.carrier_hz(118.525))
        self.assertIsNone(dfs.carrier_hz(125.320))

    def test_names(self):
        self.assertEqual('EDDE TWR', dfs._compose('EDDE', 'TWR', 8))
        self.assertEqual('THUERING LS', dfs._compose('THUERINGEN LOW SUED',
                                                     '', 11))
        self.assertEqual('CRAWIN-T',
                         dfs._compose('CRAWINKEL', 'TWR', 8, keep=True))
        self.assertEqual('SAR 2', dfs._unique('SAR', {'SAR'}, 12))


class TestAirspaces(unittest.TestCase):
    def setUp(self):
        self.airspaces = {a.identifier: a for a in
                          dfs.parse_airspaces(AIRSPACES).values()}

    def test_aggregation(self):
        # ZZMR24 has no geometry of its own; BASE + UNION of two parts
        moria = self.airspaces['ZZMR24']
        self.assertEqual(2, len(moria.volumes))
        self.assertEqual([24500.0, 24500.0],
                         [v.lower_ft for v in moria.volumes])

    def test_circle(self):
        ctr = self.airspaces['ZZBR'].volumes[0]
        self.assertEqual(0.0, ctr.lower_ft)
        ring = ctr.rings[0]
        self.assertEqual(ring[0], ring[-1])
        self.assertTrue(dfs._contains(ring, 51.05, 11.0))  # its centre
        self.assertFalse(dfs._contains(ring, 51.5, 11.0))

    def test_geometry(self):
        square = [(50.0, 10.0), (51.0, 10.0), (51.0, 11.0), (50.0, 11.0),
                  (50.0, 10.0)]
        self.assertTrue(dfs._contains(square, 50.5, 10.5))
        self.assertFalse(dfs._contains(square, 50.5, 11.5))
        # 1 x 1 degree around 50.5 N is roughly 111 x 71 km
        self.assertAlmostEqual(7880, dfs._area_km2(square), delta=100)
        arc = dfs._arc_points(50.0, 10.0, 10.0, 0.0, 90.0)
        self.assertAlmostEqual(50.0 + 10 / 111.2, arc[0][0], places=3)
        self.assertAlmostEqual(10.0, arc[-1][0], delta=50.0)
        self.assertGreater(arc[-1][1], 10.1)  # east of the centre

    def test_memories(self):
        memories = dfs.build_memories(
            dfs.parse_aerodromes(AERODROMES),
            dfs.parse_frequencies(SERVICE),
            (51.0, 10.9), 50, airspaces=dfs.parse_airspaces(AIRSPACES))
        self.assertEqual(['EMERGENCY', 'SAR', 'ZZBR TWR', 'ZZBR ATIS',
                          'RIVENDEL FIS', 'MORIA ACC', 'ESGAROT INFO',
                          'ZZHB AFIS'],
                         [m.name for m in memories])
        # Unnamed information sector: named after the callsign
        self.assertEqual('RIVENDELL INFORMATION, ZZRV03 overhead',
                         memories[4].comment.split(' | ')[0])
        self.assertEqual('MORIA RADAR overhead',
                         memories[5].comment.split(' | ')[0])

    def test_sector_names(self):
        # A sector gives a narrow display entirely to its place
        self.assertEqual('THUERING', dfs._compose('THUERINGEN', 'ACC', 8))
        self.assertEqual('THUER LS', dfs._compose('THUERINGEN LOW SUED',
                                                  'ACC', 8))
