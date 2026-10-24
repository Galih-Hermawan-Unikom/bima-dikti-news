"""Dedicated schema and atomic local CAS. No GitHub/Telegram network implementation."""
from copy import deepcopy
from datetime import datetime, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from uuid import UUID

from kemdiktisaintek_parser import normalize_url
from kemdiktisaintek_policy import POLICY_VERSION

SOURCE = 'kemdiktisaintek'
STATE_BRANCH = 'kemdiktisaintek-experiment-state'
STATE_PATH = '.bot-state/kemdiktisaintek_experiment.json'
MAX_BYTES = 900_000
DETECTIONS = {'NEW_CANDIDATE', 'KNOWN', 'HOLD', 'REJECTED', 'NEEDS_REVIEW'}


class StateError(RuntimeError):
    pass


class StateConflict(StateError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def timestamp(value):
    if not isinstance(value, str):
        raise StateError('TIMEZONE_AWARE_TIMESTAMP_REQUIRED')
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError()
        return dt.astimezone(timezone.utc)
    except ValueError:
        raise StateError('TIMEZONE_AWARE_TIMESTAMP_REQUIRED') from None


def uid(value):
    try:
        if str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise StateError('VALID_UUID_REQUIRED') from None
    return value


def validate_state(state):
    try:
        if (not isinstance(state, dict) or state.get('schema_version') != 1
                or type(state.get('schema_version')) is not int
                or state.get('source') != SOURCE or state.get('initialized') is not True
                or state.get('policy_version') != POLICY_VERSION or state.get('delivery_authorized') is not False
                or type(state.get('generation')) is not int or state['generation'] < 1
                or not isinstance(state.get('observations'), dict) or not isinstance(state.get('identity_holds'), dict)
                or state.get('outbox') != {} or not isinstance(state.get('revisions'), list)):
            raise StateError('INVALID_STATE_SCHEMA_OR_STAGE2_DELIVERY')
        activation = timestamp(state['activation_at'])
        poll = timestamp(state['last_successful_poll_at'])
        baseline = state['baseline']
        if (poll < activation or not isinstance(baseline, dict) or baseline.get('archive_complete') is not False
                or not isinstance(baseline.get('ids'), list) or len(set(baseline['ids'])) != len(baseline['ids'])
                or not isinstance(baseline.get('coverage'), dict)
                or baseline.get('sha256') != fingerprint({k: v for k, v in baseline.items() if k != 'sha256'})
                or timestamp(baseline['observed_at']) != activation):
            raise StateError('INVALID_BASELINE')
        for identifier in baseline['ids']:
            uid(identifier)
            if identifier not in state['observations']:
                raise StateError('BASELINE_OBSERVATION_MISSING')
        urls = {}
        for identifier, row in state['observations'].items():
            uid(identifier)
            if (not isinstance(row, dict) or row.get('uuid') != identifier or not isinstance(row.get('title'), str)
                    or not row['title'] or normalize_url(row['url'], article=True) != row['url']
                    or row.get('detection_status') not in DETECTIONS or row.get('delivery_status') != 'BLOCKED_STAGE2'
                    or row.get('classification', {}).get('decision') not in {'RELEVANT', 'NOT_RELEVANT', 'NEEDS_REVIEW'}
                    or row['classification'].get('policy_version') != POLICY_VERSION
                    or 'published_at' not in row or not isinstance(row.get('url_history'), list)
                    or row['url'] not in row['url_history']
                    or not isinstance(row.get('reasons'), list) or not isinstance(row.get('last_item'), dict)
                    or type(row.get('initial_inventory')) is not bool
                    or (row.get('content_sha256') is not None and not re.fullmatch('[a-f0-9]{64}', row['content_sha256']))):
                raise StateError('INVALID_OBSERVATION')
            if row['last_item'].get('id') != identifier or row['last_item'].get('url') != row['url']:
                raise StateError('OBSERVATION_ITEM_CONFLICT')
            if row['title'] != row['last_item'].get('title'):
                raise StateError('OBSERVATION_TITLE_CONFLICT')
            if row['content_sha256'] != row['last_item'].get('content_sha256'):
                raise StateError('OBSERVATION_HASH_CONFLICT')
            if row['published_at'] != row['last_item'].get('published_at'):
                raise StateError('OBSERVATION_DATE_CONFLICT')
            if row['content_sha256'] and row['content_sha256'] != fingerprint({k: row['last_item'].get(k)
                    for k in ('title', 'category', 'body_text', 'attachments', 'external_links')}):
                raise StateError('CONTENT_FINGERPRINT_MISMATCH')
            first, last = timestamp(row['first_seen_at']), timestamp(row['last_seen_at'])
            if not activation <= first <= last <= poll:
                raise StateError('INVALID_OBSERVATION_TIME')
            if row['initial_inventory'] != (identifier in baseline['ids']) or (
                    row['initial_inventory'] and first != activation):
                raise StateError('BASELINE_INVENTORY_CONFLICT')
            for url in row['url_history']:
                if normalize_url(url, article=True) != url or (url in urls and urls[url] != identifier):
                    raise StateError('STATE_URL_UUID_CONFLICT')
                urls[url] = identifier
            if row.get('detail_checked_at') is not None and not first <= timestamp(row['detail_checked_at']) <= poll:
                raise StateError('INVALID_DETAIL_CHECK_TIME')
        for row in state['identity_holds'].values():
            if row.get('detection_status') != 'HOLD' or row.get('delivery_status') != 'BLOCKED_STAGE2':
                raise StateError('INVALID_IDENTITY_HOLD')
            timestamp(row['first_seen_at']); timestamp(row['last_seen_at'])
        for revision in state['revisions']:
            if (revision.get('status') != 'NEEDS_REVIEW' or revision.get('event_type') != 'REVISION_CANDIDATE'
                    or revision.get('uuid') not in state['observations'] or not isinstance(revision.get('changes'), dict)
                    or revision.get('revision_id') != fingerprint({k: revision[k] for k in ('uuid', 'before', 'after')})):
                raise StateError('INVALID_REVISION')
            timestamp(revision['observed_at'])
        if len(canonical(state)) > MAX_BYTES:
            raise StateError('STATE_TOO_LARGE')
    except (KeyError, TypeError, ValueError, AttributeError):
        raise StateError('INVALID_STATE_CONTENT') from None
    return state


def validate_lineage(current, state):
    validate_state(state)
    if (state['generation'] != current['generation'] + 1 or state['baseline'] != current['baseline']
            or state['activation_at'] != current['activation_at']
            or timestamp(state['last_successful_poll_at']) <= timestamp(current['last_successful_poll_at'])
            or not current['observations'].keys() <= state['observations'].keys()
            or not current['identity_holds'].keys() <= state['identity_holds'].keys()):
        raise StateError('INVALID_CHECKPOINT_LINEAGE')
    for identifier, old in current['observations'].items():
        row = state['observations'][identifier]
        if (row['first_seen_at'] != old['first_seen_at'] or row['initial_inventory'] != old['initial_inventory']
                or not set(old['url_history']) <= set(row['url_history'])
                or timestamp(row['last_seen_at']) < timestamp(old['last_seen_at'])):
            raise StateError('OBSERVATION_HISTORY_IMMUTABLE')
    old_revisions = {r['revision_id']: r for r in current['revisions']}
    new_revisions = {r['revision_id']: r for r in state['revisions']}
    if any(new_revisions.get(key) != value for key, value in old_revisions.items()):
        raise StateError('REVISION_HISTORY_IMMUTABLE')


def _decode(raw):
    try:
        if not raw or len(raw) > MAX_BYTES:
            raise ValueError()
        return validate_state(json.loads(raw.decode('utf-8')))
    except (ValueError, UnicodeDecodeError):
        raise StateError('CORRUPT_STATE') from None


class LocalStateStore:
    """Explicit isolated directory; fixed filename, create-only baseline, locked CAS.

    A missing state in load/commit is always an error. No automatic reinitialization.
    Lock acquisition is nonblocking: concurrent or stale locks require reconciliation.
    """
    def __init__(self, root):
        path = Path(root).absolute()
        if path.name != STATE_BRANCH or any(p.is_symlink() for p in (path, *path.parents)):
            raise StateError('LOCAL_STATE_DIRECTORY_NOT_ALLOWLISTED')
        self.root = path.resolve()
        self.path = self.root / STATE_PATH
        self.marker = self.path.with_name('kemdiktisaintek_initialized.json')

    def _safe(self):
        if any(p.is_symlink() for p in (self.path, self.marker, self.path.parent, self.root)):
            raise StateError('STATE_SYMLINK_REJECTED')
        if not self.path.resolve().is_relative_to(self.root):
            raise StateError('STATE_PATH_ESCAPE')

    def load(self):
        self._safe()
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            raise StateError('STATE_MISSING_EXPLICIT_INITIALIZATION_REQUIRED') from None
        state = _decode(raw)
        try:
            marker = json.loads(self.marker.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise StateError('INITIALIZATION_MARKER_MISSING_OR_CORRUPT') from None
        if marker != {'activation_at': state['activation_at'], 'baseline_sha256': state['baseline']['sha256']}:
            raise StateError('INITIALIZATION_MARKER_CONFLICT')
        return state, hashlib.sha256(raw).hexdigest()

    def assert_can_initialize(self):
        self._safe()
        if self.path.exists():
            raise StateConflict('BASELINE_ALREADY_EXISTS')
        if self.marker.exists():
            raise StateError('STATE_LOST_OR_INITIALIZATION_INCOMPLETE_RESTORE_REQUIRED')

    def create(self, state):
        return self._save(state, None, creating=True)

    def commit(self, state, expected):
        if not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected):
            raise StateError('EXPECTED_LOCAL_SHA_REQUIRED')
        return self._save(state, expected, creating=False)

    def _save(self, state, expected, *, creating):
        validate_state(state)
        self._safe()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_suffix('.lock')
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise StateConflict('STATE_LOCKED') from None
        temporary = None
        try:
            os.close(fd)
            self._safe()
            if creating:
                self.assert_can_initialize()
                if state['generation'] != 1:
                    raise StateError('INVALID_INITIAL_GENERATION')
                # Durable local tombstone precedes state creation. An interrupted
                # initialization stops for recovery; it never silently tries again.
                with self.marker.open('xb') as stream:
                    stream.write(canonical({'activation_at': state['activation_at'],
                                            'baseline_sha256': state['baseline']['sha256']}))
                    stream.flush(); os.fsync(stream.fileno())
            else:
                current, sha = self.load()
                if sha != expected:
                    raise StateConflict('LOCAL_CAS_CONFLICT')
                validate_lineage(current, state)
            raw = canonical(state)
            fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix='.checkpoint-')
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            return hashlib.sha256(raw).hexdigest()
        except OSError:
            raise StateError('LOCAL_CHECKPOINT_FAILED') from None
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
            lock.unlink(missing_ok=True)


class MockContentsTransport:
    """In-memory GitHub Contents semantics, deliberately no HTTP/token capability."""
    def __init__(self):
        self.raw = None
        self.sha = None
        self.calls = []
        self.initialized = False

    def get(self, branch, path):
        self.calls.append(('GET', branch, path))
        if self.raw is None:
            return {'status': 404}
        return {'status': 200, 'sha': self.sha, 'content': base64.b64encode(self.raw).decode()}

    def put(self, branch, path, content, expected):
        self.calls.append(('PUT', branch, path))
        if self.sha != expected:
            return {'status': 409}
        raw = base64.b64decode(content, validate=True)
        self.sha = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        self.raw = raw
        self.initialized = True
        return {'status': 201, 'sha': self.sha}


class ContentsCAS:
    """Schema/branch/path wrapper for mock-only Contents API checkpoints in Stage 2."""
    def __init__(self, transport, *, branch=STATE_BRANCH, path=STATE_PATH):
        if branch != STATE_BRANCH or path != STATE_PATH or type(transport) is not MockContentsTransport:
            raise StateError('REMOTE_OR_NONALLOWLISTED_BACKEND_DISABLED_STAGE2')
        self.transport = transport

    def load(self):
        row = self.transport.get(STATE_BRANCH, STATE_PATH)
        if row['status'] != 200:
            raise StateError('MOCK_STATE_MISSING')
        if not re.fullmatch('[a-f0-9]{40}', row.get('sha', '')):
            raise StateError('INVALID_BLOB_SHA')
        return _decode(base64.b64decode(row['content'], validate=True)), row['sha']

    def create(self, state):
        if self.transport.raw is not None or self.transport.initialized:
            raise StateConflict('BASELINE_ALREADY_EXISTS')
        validate_state(state)
        if state['generation'] != 1:
            raise StateError('INVALID_INITIAL_GENERATION')
        return self._put(state, None)

    def commit(self, state, expected):
        current, sha = self.load()
        if sha != expected:
            raise StateConflict('BLOB_CAS_CONFLICT')
        validate_lineage(current, state)
        return self._put(state, expected)

    def _put(self, state, expected):
        row = self.transport.put(STATE_BRANCH, STATE_PATH, base64.b64encode(canonical(state)).decode(), expected)
        if row['status'] in {409, 422}:
            raise StateConflict('BLOB_CAS_CONFLICT')
        if row['status'] not in {200, 201} or not re.fullmatch('[a-f0-9]{40}', row.get('sha', '')):
            raise StateError('MOCK_CHECKPOINT_FAILED')
        return row['sha']
