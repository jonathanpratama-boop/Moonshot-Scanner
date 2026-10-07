"""Pinned, read-only capture033/v34 evidence adapter, outside the radar queue.

This module does not import radar_ops, entry_bridge, detectors or persistence.
It reads one authenticated-by-local-pin packet, retains exact bytes, and returns
separate review projections. Pins prove identity to the supplied packet, not
independent market truth, backend authorship or original-executable equivalence.
No packet text is executed. No writes, wall clocks, network, admission or fills.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
from types import MappingProxyType

MANIFEST_SHA256 = '79e5ce95d905ac5883753add28b67a9e01ff141a00334a209c91f691f9a46b41'
SOURCE_GENERATION = 'MOONSHOT-CAPTURE-20261006-033'
CASES = ('near_rs_shock', 'apxc', 'techd', 'near_compression', 'hlb')
MODE = 'ARCHIVE_READ_ONLY_PROJECTION'


class ArchiveRefused(ValueError):
    """A byte pin, identity, or archive-only contract does not match."""


def require(condition, message):
    # Explicit validation remains active under python -O.
    if not condition:
        raise ArchiveRefused(message)


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class EvidenceFile:
    path: str
    raw: bytes
    expected_sha256: str
    evidence_class: str

    def __post_init__(self):
        require(isinstance(self.raw, bytes), 'immutable raw bytes required')
        require(sha256(self.raw) == self.expected_sha256, 'raw-file pin mismatch: ' + self.path)

    def object(self):
        # Each call returns a detached object. The exact original is always raw.
        return json.loads(self.raw)


class ArchiveIndex:
    """In-memory archive identities only; never a study trial or research queue.

    Same record ID and bytes produce a repeat receipt. A conflicting ID is
    refused. Different record IDs are retained even within one dependency group.
    Synthetic variants can be used in tests with an explicit evidence class;
    load_packet never admits them into the fixed accepted packet.
    """
    def __init__(self):
        self._records = {}
        self._receipts = []

    def add(self, evidence):
        require(isinstance(evidence, EvidenceFile), 'verified evidence required')
        require(evidence.evidence_class in ('EXACT_ACCEPTED_STUDY_RECORD', 'SYNTHETIC_TEST_ONLY'),
                'unsupported record evidence class')
        record = evidence.object()
        require(isinstance(record, dict), 'record object required')
        key = record.get('record_id')
        require(isinstance(key, str) and bool(key), 'record identity required')
        for field in ('authored_at_utc', 'forecast_target', 'forecast_deadline_at_utc'):
            require(field in record and record[field] is None, 'archive unknown changed: ' + field)
        for field in ('primary_forecast_admitted', 'diagnostic_admitted', 'entry_eligible'):
            require(record.get(field) is False, 'archive admission changed: ' + field)
        for field in ('registration_status', 'cohort'):
            require(record.get(field) == 'RESEARCH_ONLY', 'archive classification changed: ' + field)
        prior = self._records.get(key)
        if prior is not None:
            require(prior.raw == evidence.raw and prior.evidence_class == evidence.evidence_class,
                    'conflicting bytes or evidence class for record identity')
        else:
            self._records[key] = evidence
        receipt = {'sequence': len(self._receipts), 'record_id': key,
                   'raw_sha256': evidence.expected_sha256,
                   'evidence_class': evidence.evidence_class,
                   'status': 'REPEAT_ARCHIVE_RECORD' if prior else 'ARCHIVE_RECORD_RETAINED',
                   'mode': MODE, 'cohort_admitted': False}
        self._receipts.append(receipt)
        return dict(receipt)

    def records(self):
        return tuple(self._records.values())

    def receipts(self):
        return [dict(receipt) for receipt in self._receipts]


class ArchivePacket:
    def __init__(self, files):
        self._files = MappingProxyType(dict(files))
        self._index = ArchiveIndex()
        for case in CASES:
            self._index.add(self.file('records/' + case + '.json'))
        rows = self.file('captures/case_clock_and_provenance_index.json').object()
        require(isinstance(rows, list) and len(rows) == len(CASES), 'clock index count mismatch')
        clock_by_case = {row['case']: row for row in rows}
        require(set(clock_by_case) == set(CASES), 'clock index selection mismatch')
        for case in CASES:
            record = self.record(case)
            capture = self.file('captures/' + case + '.json').object()
            clock = clock_by_case[case]
            body_key = 'sanitized_raw_text' if 'sanitized_raw_text' in capture else 'sanitized_body'
            require(sha256(capture[body_key].encode('utf-8')) == clock['sanitized_body_sha256_verified'],
                    'sanitized body pin mismatch: ' + case)
            require(capture['authored_at_utc'] is None and clock['authored_at_utc'] is None,
                    'unknown authorship changed: ' + case)
            require(record['source_displayed_datetime_utc'] == capture['source_datetime_utc'] ==
                    clock['platform_datetime_utc'], 'display clock mismatch: ' + case)
            require(record['first_observed_at_utc'] == clock['collector_first_observed_at_utc'],
                    'collector clock mismatch: ' + case)
            require(record['source_signal_id'] == clock['signal_id'] and
                    record['dependency_group_id'] == clock['dependency_group_id'], 'record index mismatch: ' + case)
            require(record['source_message_hash_sha256'] == capture['message_content_hash_sha256'] ==
                    clock['unredacted_body_sha256_recorded_only'], 'recorded hash claim mismatch: ' + case)

    def file(self, path):
        return self._files[path]

    def paths(self):
        return tuple(self._files)

    def record(self, case):
        require(case in CASES, 'case outside fixed accepted selection')
        return self.file('records/' + case + '.json').object()

    def review_view(self):
        clock_by_case = {row['case']: row for row in
                         self.file('captures/case_clock_and_provenance_index.json').object()}
        selected = {self.record(case)['source_signal_id'] for case in CASES}
        records = []
        warnings = []
        for case in CASES:
            record = self.record(case)
            prior = record.get('related_prior_signal')
            if prior is not None and prior not in selected:
                warnings.append({'code': 'RELATED_SIGNAL_NOT_IN_SELECTED_ARCHIVE',
                                 'record_id': record['record_id'], 'related_prior_signal': prior,
                                 'resolution': 'UNRESOLVED_PRESERVE_ORIGINAL_LINK'})
            records.append({'case': case, 'archive_record': record,
                            'record_raw_sha256': self.file('records/' + case + '.json').expected_sha256,
                            'archived_capture': self.file('captures/' + case + '.json').object(),
                            'clock_provenance': clock_by_case[case]})
        return {'schema': 'moonshot.archive.review.v1', 'mode': MODE,
                'source_generation': SOURCE_GENERATION,
                'packet_manifest_sha256': MANIFEST_SHA256,
                'operational_enabled': False, 'study_state': 'PAUSED',
                'study_state_scope': 'adapter execution boundary; not a live scheduler read',
                'source_truth_verified_by_adapter': False,
                'original_executable_equivalence': 'UNVERIFIED',
                'trial_creation_performed': False, 'forecast_admission_performed': False,
                'entry_assessment_performed': False, 'records': records, 'warnings': warnings,
                'reference_files': {path: self.file(path).expected_sha256 for path in self.paths()},
                'repeat_receipts': self._index.receipts()}


def load_packet(root):
    """Verify every supplied packet byte before exposing any derived view.

    The manifest pin is anchored in this version of the adapter. Replacing the
    manifest together with files cannot turn a later capture into accepted input.
    The accepted protocol's canonical serialization is preserved without claiming
    that those JSON files preexisted as historical standalone source files.
    """
    root = Path(root)
    require(root.is_dir() and not root.is_symlink(), 'regular packet directory required')
    manifest_path = root / 'MANIFEST.json'
    require(manifest_path.is_file() and not manifest_path.is_symlink(), 'regular manifest required')
    manifest = EvidenceFile('MANIFEST.json', manifest_path.read_bytes(), MANIFEST_SHA256,
                            'PINNED_PACKET_MANIFEST')
    document = manifest.object()
    require(document['schema'] == 'moonshot.compatibility.evidence.packet.v1' and
            document['source_generation'] == SOURCE_GENERATION, 'unsupported packet generation')
    files = {'MANIFEST.json': manifest}
    for entry in document['files']:
        relative = PurePosixPath(entry['path'])
        require(not relative.is_absolute() and '..' not in relative.parts and
                relative.as_posix() == entry['path'] and entry['path'] not in files,
                'invalid or duplicate packet path')
        path = root / relative
        require(all(not (root.joinpath(*relative.parts[:i])).is_symlink()
                    for i in range(1, len(relative.parts) + 1)), 'symlink packet member refused')
        require(path.is_file(), 'missing packet member: ' + entry['path'])
        raw = path.read_bytes()
        require(len(raw) == entry['bytes'], 'raw-file size mismatch: ' + entry['path'])
        files[entry['path']] = EvidenceFile(entry['path'], raw, entry['sha256'], entry['evidence_class'])
    observed = set()
    for path in root.rglob('*'):
        require(not path.is_symlink(), 'symlink packet member refused')
        if path.is_file():
            observed.add(path.relative_to(root).as_posix())
    require(observed == set(files), 'unexpected packet member; fixed capture033 selection only')
    return ArchivePacket(files)
