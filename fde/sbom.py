"""Offline SBOM x advisory matching with VEX, NOT a vulnerability scanner.

Inputs are files listed in the signed release bundle (3-1): a CycloneDX 1.6
JSON SBOM whose components carry name, version and purl, OSV records from an
offline feed, and VEX statements. Only OSV ranges of type SEMVER are evaluated,
with SemVer 2.0.0 precedence (build metadata ignored). ECOSYSTEM ranges (e.g.
PyPI versions under PEP 440) and GIT ranges are not guessed: unless the exact
version string is enumerated in `versions`, the pair is 'unevaluated' and stays
open. VEX statuses and justifications follow CISA's minimum requirements.

Feed contract beyond OSV: besides OSV's required id, modified and package
ecosystem/name, `published` and affected[].package.purl are required and
database_specific.severity is a key of SEVERITY_DAYS. Packages are matched by
the purl string as given (no per-type normalization), so the SBOM generator and
the feed converter must agree on purl spelling. Only those fields and affected[]
ranges/versions are read; validate the rest against the OSV JSON schema when the
feed is imported. The day counts are proposals to agree with the customer's
security officer, not a standard. The deadline is a UTC calendar date counted
from publication, so a slow import cadence consumes it.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import re
from urllib.parse import unquote

SEVERITY_DAYS = {'critical': 7, 'high': 30, 'medium': 90, 'low': 180, 'unknown': 7}
VEX_STATUSES = ('not_affected', 'affected', 'fixed', 'under_investigation')
JUSTIFICATIONS = ('Component_not_present', 'Vulnerable_code_not_present',
                  'Vulnerable_code_not_in_execute_path',
                  'Vulnerable_code_cannot_be_controlled_by_adversary',
                  'Inline_mitigations_already_exist')
OPEN = ('affected', 'under_investigation', 'unevaluated')

_IDENT = r'(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)'
_SEMVER = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
                     rf'(?:-({_IDENT}(?:\.{_IDENT})*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?')
_PACKAGE = re.compile(r'pkg:[a-z][a-z0-9.+-]*/[^@?#\s]+')
_ID = re.compile(r'[A-Za-z0-9_.:-]{1,100}')
_ZERO = (-1,)   # OSV introduced "0" sorts before every version


def semver_key(version):
    """Sort key for SemVer 2.0.0 section 11 precedence; anything else raises."""
    match = _SEMVER.fullmatch(version) if type(version) is str else None
    if match is None:
        raise ValueError(f'not a SemVer 2.0.0 version: {version!r}')
    core = tuple(int(part) for part in match.group(1, 2, 3))
    if match[4] is None:
        return core + ((1,),)
    identifiers = tuple((0, int(i), '') if i.isdigit() else (1, 0, i) for i in match[4].split('.'))
    return core + ((0, identifiers),)


def _split_purl(purl):
    if type(purl) is not str:
        raise ValueError('purl must be a string')
    package, at, version = purl.split('#')[0].split('?')[0].rpartition('@')
    if not at or not version or not _PACKAGE.fullmatch(package):
        raise ValueError(f'purl must pin a version: {purl!r}')
    return package, unquote(version)


@dataclass(frozen=True)
class Component:
    name: str
    version: str
    purl: str

    @property
    def package(self):
        return _split_purl(self.purl)[0]


def _strict(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def parse_sbom(text):
    data = json.loads(text, object_pairs_hook=_strict)
    if type(data) is not dict or data.get('bomFormat') != 'CycloneDX' or data.get('specVersion') != '1.6':
        raise ValueError('expected a CycloneDX 1.6 JSON SBOM')
    items = data.get('components')
    if type(items) is not list or not 1 <= len(items) <= 10_000:
        raise ValueError('invalid component list')
    components = []
    for item in items:
        # A nested component list would otherwise be skipped silently.
        if type(item) is not dict or 'components' in item:
            raise ValueError('invalid or nested component')
        fields = [item.get(name) for name in ('name', 'version', 'purl')]
        if not all(type(value) is str and value for value in fields):
            raise ValueError('component needs name, version and purl')
        if _split_purl(fields[2])[1] != fields[1]:
            raise ValueError('purl version differs from component version')
        components.append(Component(*fields))
    if len({c.purl for c in components}) != len(components):
        raise ValueError('duplicate component purl')
    return tuple(components)


@dataclass(frozen=True)
class AdvisoryEntry:
    id: str
    published: date
    severity: str
    package: str
    ranges: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
    versions: tuple[str, ...]


def _utc_date(value):
    if type(value) is not str or not value.endswith('Z'):
        raise ValueError('timestamps must be RFC3339 UTC ending in Z')
    return datetime.fromisoformat(value).astimezone(timezone.utc).date()


def _range(item):
    kind = item.get('type') if type(item) is dict else None
    events = item.get('events') if type(item) is dict else None
    if kind not in ('SEMVER', 'ECOSYSTEM', 'GIT') or type(events) is not list:
        raise ValueError('unknown range type or events')
    parsed = []
    for event in events:
        if type(event) is not dict or len(event) != 1:
            raise ValueError('each event has exactly one key')
        (name, value), = event.items()
        if name not in ('introduced', 'fixed', 'last_affected', 'limit') or type(value) is not str:
            raise ValueError('invalid range event')
        if kind == 'SEMVER':
            if name == 'limit':
                raise ValueError('limit is not used for SEMVER ranges')
            if not (name == 'introduced' and value == '0'):
                semver_key(value)
        parsed.append((name, value))
    names = {name for name, _ in parsed}
    if 'introduced' not in names or {'fixed', 'last_affected'} <= names:
        raise ValueError('range needs introduced and not both fixed and last_affected')
    return kind, tuple(parsed)


def parse_osv(record):
    """One OSV record -> entries; a withdrawn record yields none."""
    if type(record) is not dict or type(record.get('id')) is not str or not _ID.fullmatch(record['id']):
        raise ValueError('invalid advisory id')
    _utc_date(record.get('modified'))
    if 'withdrawn' in record:
        _utc_date(record['withdrawn'])
        return ()
    published = _utc_date(record.get('published'))
    extra = record.get('database_specific')
    severity = extra.get('severity') if type(extra) is dict else None
    if severity not in SEVERITY_DAYS:
        raise ValueError('feed contract: database_specific.severity is required')
    affected = record.get('affected')
    if type(affected) is not list or not affected:
        raise ValueError('advisory without affected packages')
    entries = []
    for item in affected:
        package = item.get('package') if type(item) is dict else None
        if type(package) is not dict or not all(type(package.get(field)) is str and package[field]
                                                for field in ('ecosystem', 'name')):
            raise ValueError('OSV requires package ecosystem and name')
        purl = package.get('purl')
        if type(purl) is not str or not _PACKAGE.fullmatch(purl):
            raise ValueError('feed contract: package purl without version is required')
        versions = item.get('versions', [])
        if type(versions) is not list or not all(type(v) is str and v for v in versions):
            raise ValueError('invalid versions list')
        ranges = item.get('ranges', [])
        if type(ranges) is not list or not (ranges or versions):
            raise ValueError('affected entry needs ranges or versions')
        entries.append(AdvisoryEntry(record['id'], published, severity, purl,
                                     tuple(_range(r) for r in ranges), tuple(versions)))
    return tuple(entries)


def parse_feed(records):
    if type(records) not in (list, tuple):
        raise ValueError('feed must be a list of OSV records')
    ids = [record.get('id') if type(record) is dict else None for record in records]
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate advisory id in feed')
    return tuple(entry for record in records for entry in parse_osv(record))


def _semver_hit(version_key, events):
    # OSV evaluation pseudocode: walk events in version order.
    vulnerable = False
    for name, key in sorted(((n, _ZERO if v == '0' and n == 'introduced' else semver_key(v))
                             for n, v in events), key=lambda event: event[1]):
        if name == 'introduced' and version_key >= key:
            vulnerable = True
        elif name == 'fixed' and version_key >= key:
            vulnerable = False
        elif name == 'last_affected' and version_key > key:
            vulnerable = False
    return vulnerable


def affected(version, entry):
    """True, False, or None when no evaluable rule decides (never a guess)."""
    if version in entry.versions:
        return True
    undecided = False
    for kind, events in entry.ranges:
        if kind != 'SEMVER':
            undecided = True          # ECOSYSTEM/GIT ordering is not implemented here
            continue
        try:
            key = semver_key(version)
        except ValueError:
            undecided = True          # the component version is not SemVer
            continue
        if _semver_hit(key, events):
            return True
    return None if undecided else False


@dataclass(frozen=True)
class VexStatement:
    vulnerability: str
    purl: str
    status: str
    justification: str | None = None
    impact_statement: str | None = None
    action_statement: str | None = None

    def __post_init__(self):
        if type(self.vulnerability) is not str or not _ID.fullmatch(self.vulnerability):
            raise ValueError('invalid vulnerability id')
        _split_purl(self.purl)
        if self.status not in VEX_STATUSES:
            raise ValueError('unknown VEX status')
        if self.justification is not None and (self.status != 'not_affected'
                                               or self.justification not in JUSTIFICATIONS):
            raise ValueError('justification only from the CISA list, for not_affected')
        for text in (self.impact_statement, self.action_statement):
            if text is not None and (type(text) is not str or not text.strip()):
                raise ValueError('empty statement')
        if self.status == 'not_affected' and not (self.justification or self.impact_statement):
            raise ValueError('not_affected needs a justification or an impact statement')
        if self.status == 'affected' and not self.action_statement:
            raise ValueError('affected needs an action statement')


@dataclass(frozen=True)
class Finding:
    advisory: str
    purl: str
    severity: str
    status: str
    due: date | None
    justification: str | None


@dataclass(frozen=True)
class Report:
    as_of: date
    findings: tuple[Finding, ...]
    unused_vex: tuple[tuple[str, str], ...]

    @property
    def blocking(self):
        """Open findings past their due date (the release gate input)."""
        return tuple(f for f in self.findings if f.status in OPEN and f.due < self.as_of)


def assess(components, entries, vex=(), *, as_of):
    if type(as_of) is not date:
        raise ValueError('as_of must be a date')
    statements = {}
    for statement in vex:
        if type(statement) is not VexStatement:
            raise ValueError('invalid VEX statement')
        key = (statement.vulnerability, statement.purl)
        if key in statements:
            raise ValueError('conflicting VEX statements')
        statements[key] = statement
    by_package = {}
    for component in components:
        if type(component) is not Component:
            raise ValueError('invalid component')
        by_package.setdefault(component.package, []).append(component)
    verdicts, meta = {}, {}
    for entry in entries:
        if type(entry) is not AdvisoryEntry:
            raise ValueError('invalid advisory entry')
        for component in by_package.get(entry.package, ()):
            key = (entry.id, component.purl)
            hit = affected(component.version, entry)
            previous = verdicts.get(key, False)
            verdicts[key] = True if True in (previous, hit) else (None if None in (previous, hit) else False)
            meta[key] = entry
    findings = []
    for key, hit in verdicts.items():
        statement = statements.get(key)
        if statement is not None:
            status = statement.status
        elif hit is None:
            status = 'unevaluated'
        elif hit:
            status = 'affected'
        else:
            continue
        entry = meta[key]
        due = entry.published + timedelta(days=SEVERITY_DAYS[entry.severity]) if status in OPEN else None
        findings.append(Finding(key[0], key[1], entry.severity, status, due,
                                statement.justification if statement else None))
    findings.sort(key=lambda f: (f.due or date.max, f.advisory, f.purl))
    return Report(as_of, tuple(findings), tuple(sorted(set(statements) - set(verdicts))))
