from dataclasses import FrozenInstanceError, replace
from datetime import date
import json
import unittest

from fde.sbom import (
    JUSTIFICATIONS, SEVERITY_DAYS, VEX_STATUSES, VexStatement, affected, assess,
    parse_feed, parse_osv, parse_sbom, semver_key,
)

# Fixture packages and x_ advisory IDs are fictional (OSV reserves x_ for local
# databases); they are NOT real vulnerabilities.
AS_OF = date(2026, 11, 16)   # the handout's reference date


def lib(name, version, kind='npm'):
    return {'type': 'library', 'name': name, 'version': version,
            'purl': f'pkg:{kind}/{name}@{version}'}


def sbom_text(*components, **updates):
    data = {'bomFormat': 'CycloneDX', 'specVersion': '1.6', 'components': list(components)}
    data.update(updates)
    return json.dumps(data)


def osv(vid, purl, kind='SEMVER', events=None, severity='high', published='2026-10-01T00:00:00Z',
        **updates):
    record = {'id': vid, 'modified': published, 'published': published,
              'database_specific': {'severity': severity},
              'affected': [{'package': {'ecosystem': 'PyPI' if purl.startswith('pkg:pypi/') else 'npm',
                                       'name': purl.split('/')[-1], 'purl': purl},
                            'ranges': [{'type': kind,
                                        'events': events or [{'introduced': '0'}, {'fixed': '9.0.0'}]}]}]}
    record.update(updates)
    return record


class SemverTests(unittest.TestCase):
    def test_precedence_matches_semver_section_11_example(self):
        chain = ['1.0.0-alpha', '1.0.0-alpha.1', '1.0.0-alpha.beta', '1.0.0-beta', '1.0.0-beta.2',
                 '1.0.0-beta.11', '1.0.0-rc.1', '1.0.0', '2.0.0', '2.1.0', '2.1.1']
        keys = [semver_key(version) for version in chain]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(set(keys)), len(chain))
        self.assertEqual(semver_key('1.0.0+build.1'), semver_key('1.0.0+build.2'))
        self.assertLess(semver_key('1.9.0'), semver_key('1.10.0'))

    def test_non_semver_strings_are_rejected_not_guessed(self):
        for value in ('v1.2.3', '1.2', '01.2.3', '1.2.3-01', '1.2.3.4', '', ' 1.2.3', '1.2.3\n',
                      None, 1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    semver_key(value)


class OsvRangeTests(unittest.TestCase):
    def entry(self, events, kind='SEMVER', versions=None):
        record = osv('x_FIXTURE-0100', 'pkg:npm/fixture-lib', kind, events)
        if versions is not None:
            record['affected'][0]['versions'] = versions
        return parse_osv(record)[0]

    def test_multiple_range_example_is_half_open(self):
        entry = self.entry([{'introduced': '1.0.0'}, {'fixed': '1.0.2'},
                            {'introduced': '3.0.0'}, {'fixed': '3.2.5'}])
        expected = {'0.9.9': False, '1.0.0': True, '1.0.1': True, '1.0.2': False,
                    '2.9.0': False, '3.0.0': True, '3.2.4': True, '3.2.5': False,
                    '3.2.5-rc.1': True}
        self.assertEqual({v: affected(v, entry) for v in expected}, expected)

    def test_introduced_zero_and_inclusive_last_affected(self):
        unfixed = self.entry([{'introduced': '0'}])
        self.assertIs(affected('0.0.0', unfixed), True)
        self.assertIs(affected('99.0.0', unfixed), True)
        capped = self.entry([{'introduced': '0'}, {'last_affected': '2.4.1'}])
        self.assertEqual([affected(v, capped) for v in ('2.4.1', '2.4.2')], [True, False])

    def test_non_semver_ranges_are_unevaluated_unless_enumerated(self):
        pypi = self.entry([{'introduced': '0'}, {'fixed': '1.9'}], kind='ECOSYSTEM')
        self.assertIsNone(affected('1.8', pypi))
        self.assertIsNone(affected('2.0', pypi))
        listed = self.entry([{'introduced': '0'}, {'fixed': '1.9'}], 'ECOSYSTEM', ['1.8'])
        self.assertIs(affected('1.8', listed), True)
        semver = self.entry([{'introduced': '0'}, {'fixed': '2.0.0'}])
        self.assertIsNone(affected('1.8', semver))   # component version is not SemVer

    def test_feed_contract_fails_closed(self):
        bad_events = ([{'fixed': '1.0.0'}], [{'introduced': '0'}, {'limit': '2.0.0'}],
                      [{'introduced': '0'}, {'fixed': '1.0.0'}, {'last_affected': '1.0.0'}],
                      [{'introduced': 'v1.0.0'}], [{'introduced': '0', 'fixed': '1.0.0'}])
        for events in bad_events:
            with self.subTest(events=events):
                with self.assertRaises(ValueError):
                    parse_osv(osv('x_FIXTURE-0101', 'pkg:npm/fixture-lib', events=events))
        for updates in ({'published': '2026-10-01'}, {'database_specific': {}},
                        {'database_specific': {'severity': 'HIGH'}}, {'affected': []}):
            with self.subTest(updates=updates):
                with self.assertRaises(ValueError):
                    parse_osv(osv('x_FIXTURE-0102', 'pkg:npm/fixture-lib', **updates))
        for purl in ('pkg:npm/fixture-lib@1.0.0', 'npm/fixture-lib'):
            with self.assertRaises(ValueError):
                parse_osv(osv('x_FIXTURE-0103', purl))
        for missing in ('ecosystem', 'name'):   # both are required by OSV itself
            record = osv('x_FIXTURE-0107', 'pkg:npm/fixture-lib')
            del record['affected'][0]['package'][missing]
            with self.subTest(missing=missing):
                with self.assertRaises(ValueError):
                    parse_osv(record)
        with self.assertRaises(ValueError):
            parse_osv(osv('x_FIXTURE-0104', 'pkg:npm/fixture-lib', kind='RPM'))
        with self.assertRaises(ValueError):
            parse_feed([osv('x_FIXTURE-0105', 'pkg:npm/a'), osv('x_FIXTURE-0105', 'pkg:npm/b')])
        withdrawn = osv('x_FIXTURE-0106', 'pkg:npm/fixture-lib', withdrawn='2026-10-05T00:00:00Z')
        self.assertEqual(parse_osv(withdrawn), ())


class SbomTests(unittest.TestCase):
    def test_minimal_cyclonedx_components(self):
        components = parse_sbom(sbom_text(lib('fixture-pdf', '2.4.1'),
                                          {**lib('core', '1.0.0'), 'purl': 'pkg:npm/%40acme/core@1.0.0'}))
        self.assertEqual([c.package for c in components],
                         ['pkg:npm/fixture-pdf', 'pkg:npm/%40acme/core'])
        with self.assertRaises(FrozenInstanceError):
            components[0].version = '9.9.9'

    def test_sbom_contract_fails_closed(self):
        good = lib('fixture-zip', '3.1.0')
        cases = [sbom_text(good, specVersion='1.5'), sbom_text(good, bomFormat='SPDX'),
                 sbom_text(), sbom_text({**good, 'purl': 'pkg:npm/fixture-zip@3.2.0'}),
                 sbom_text({key: value for key, value in good.items() if key != 'purl'}),
                 sbom_text({**good, 'purl': 'pkg:npm/fixture-zip'}),
                 sbom_text({**good, 'components': [lib('inner', '1.0.0')]}),
                 sbom_text(good, good),
                 '{"bomFormat": "CycloneDX", "bomFormat": "CycloneDX", "specVersion": "1.6"}']
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_sbom(text)


class AssessmentTests(unittest.TestCase):
    def setUp(self):
        self.sbom = parse_sbom(sbom_text(lib('fixture-pdf', '2.4.1'), lib('fixture-zip', '3.1.0'),
                                         lib('fixture-ocr', '1.8', 'pypi')))
        self.feed = parse_feed([
            osv('x_FIXTURE-0001', 'pkg:npm/fixture-pdf', events=[{'introduced': '0'}, {'fixed': '2.4.3'}]),
            osv('x_FIXTURE-0002', 'pkg:npm/fixture-zip', severity='critical',
                events=[{'introduced': '0'}, {'fixed': '3.2.0'}]),
            osv('x_FIXTURE-0003', 'pkg:pypi/fixture-ocr', 'ECOSYSTEM', severity='medium',
                events=[{'introduced': '0'}, {'fixed': '1.9'}])])
        self.vex = [VexStatement('x_FIXTURE-0002', 'pkg:npm/fixture-zip@3.1.0', 'not_affected',
                                 justification='Vulnerable_code_not_in_execute_path')]

    def test_running_example_exact_findings(self):
        report = assess(self.sbom, self.feed, self.vex, as_of=AS_OF)
        self.assertEqual([(f.advisory, f.status, f.due) for f in report.findings], [
            ('x_FIXTURE-0001', 'affected', date(2026, 10, 31)),
            ('x_FIXTURE-0003', 'unevaluated', date(2026, 12, 30)),
            ('x_FIXTURE-0002', 'not_affected', None)])
        self.assertEqual([f.advisory for f in report.blocking], ['x_FIXTURE-0001'])
        self.assertEqual(report.findings[2].justification, 'Vulnerable_code_not_in_execute_path')
        self.assertEqual(report.unused_vex, ())

    def test_deadline_counts_from_publication_with_proposed_days(self):
        self.assertEqual(SEVERITY_DAYS, {'critical': 7, 'high': 30, 'medium': 90, 'low': 180,
                                         'unknown': 7})
        report = assess(self.sbom, self.feed, as_of=AS_OF)
        due = {f.advisory: f.due for f in report.findings}
        self.assertEqual(due['x_FIXTURE-0002'], date(2026, 10, 8))
        self.assertEqual(len(report.blocking), 2)
        self.assertEqual(assess(self.sbom, self.feed, self.vex, as_of=date(2026, 10, 31)).blocking, ())

    def test_vex_follows_cisa_minimum_requirements(self):
        self.assertEqual(len(VEX_STATUSES), 4)
        self.assertEqual(len(JUSTIFICATIONS), 5)
        purl = 'pkg:npm/fixture-zip@3.1.0'
        for kwargs in ({'status': 'not_affected'}, {'status': 'affected'},
                       {'status': 'not_affected', 'justification': 'code_not_reachable'},
                       {'status': 'fixed', 'justification': 'Component_not_present'},
                       {'status': 'not_affected', 'impact_statement': '  '},
                       {'status': 'patched'}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    VexStatement('x_FIXTURE-0002', purl, **kwargs)
        with self.assertRaises(ValueError):
            VexStatement('x_FIXTURE-0002', 'pkg:npm/fixture-zip', 'fixed')
        VexStatement('x_FIXTURE-0002', purl, 'not_affected', impact_statement='zip64 path unused')
        VexStatement('x_FIXTURE-0002', purl, 'affected', action_statement='upgrade to 3.2.0')

    def test_vex_is_bound_to_the_exact_version(self):
        upgraded = parse_sbom(sbom_text(lib('fixture-zip', '3.1.1')))
        report = assess(upgraded, self.feed, self.vex, as_of=AS_OF)
        self.assertEqual([(f.advisory, f.status) for f in report.findings],
                         [('x_FIXTURE-0002', 'affected')])
        self.assertEqual(report.unused_vex, (('x_FIXTURE-0002', 'pkg:npm/fixture-zip@3.1.0'),))

    def test_out_of_range_has_no_finding_and_vex_can_still_override(self):
        fixed = parse_sbom(sbom_text(lib('fixture-pdf', '2.4.3')))
        self.assertEqual(assess(fixed, self.feed, as_of=AS_OF).findings, ())
        statement = VexStatement('x_FIXTURE-0001', 'pkg:npm/fixture-pdf@2.4.3',
                                 'under_investigation')
        report = assess(fixed, self.feed, [statement], as_of=AS_OF)
        self.assertEqual([(f.status, f.due) for f in report.findings],
                         [('under_investigation', date(2026, 10, 31))])

    def test_entries_of_one_advisory_merge_without_hiding_open_pairs(self):
        # OSV IsVulnerable: any affected[] entry that matches decides; None stays open.
        def record(*items):
            base = osv('x_FIXTURE-0200', 'pkg:npm/fixture-pdf')
            base['affected'] = [{**base['affected'][0], **extra} for extra in items]
            return base
        out_of_range = {'ranges': [{'type': 'SEMVER', 'events': [{'introduced': '9.0.0'}]}]}
        ecosystem = {'ranges': [{'type': 'ECOSYSTEM', 'events': [{'introduced': '0'}]}]}
        in_range = {'ranges': [{'type': 'SEMVER', 'events': [{'introduced': '2.0.0'}]}]}
        sbom = parse_sbom(sbom_text(lib('fixture-pdf', '2.4.1')))
        for affected_entries, status in (((out_of_range, ecosystem), 'unevaluated'),
                                         ((ecosystem, in_range), 'affected'),
                                         ((out_of_range, out_of_range), None)):
            report = assess(sbom, parse_feed([record(*affected_entries)]), as_of=AS_OF)
            with self.subTest(status=status):
                self.assertEqual([f.status for f in report.findings], [status] if status else [])

    def test_conflicting_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            assess(self.sbom, self.feed, self.vex * 2, as_of=AS_OF)
        with self.assertRaises(ValueError):
            assess(self.sbom, self.feed, as_of='2026-11-16')
        with self.assertRaises(ValueError):
            assess(self.sbom, [*self.feed, 'not an entry'], as_of=AS_OF)
        with self.assertRaises(FrozenInstanceError):
            replace(self.feed[0]).severity = 'low'


if __name__ == '__main__':
    unittest.main()
