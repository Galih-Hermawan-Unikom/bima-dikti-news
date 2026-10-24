"""Fixed-repository/branch/path GitHub Contents API CAS. Never creates branches."""
import base64
from copy import deepcopy
import hashlib
import json
import re

import requests

from kemdiktisaintek_state import STATE_BRANCH, STATE_PATH, MAX_BYTES, StateError, StateConflict, canonical
from kemdiktisaintek_delivery_state import validate_delivery_state, validate_delivery_lineage, migrate_v1

REPOSITORY = 'Galih-Hermawan-Unikom/bima-dikti-news'


def blob_sha(raw):
    return hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


class RemoteState:
    durable = True

    def __init__(self, token, *, session=None, repo=REPOSITORY, branch=STATE_BRANCH, path=STATE_PATH):
        if repo != REPOSITORY or branch != STATE_BRANCH or path != STATE_PATH or not isinstance(token, str) or not token:
            raise StateError('REMOTE_SCOPE_OR_TOKEN_INVALID')
        self._session = session or requests.Session()
        if session is None:
            self._session.trust_env = False
        self._headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                         'X-GitHub-Api-Version': '2022-11-28'}
        self._base = 'https://api.github.com/repos/' + REPOSITORY + '/'
        self._contents = self._base + 'contents/' + STATE_PATH

    def _get(self, url):
        try:
            return self._session.get(url, headers=self._headers, timeout=30, allow_redirects=False)
        except Exception:
            raise StateError('REMOTE_GET_FAILED') from None

    def _raw(self):
        response = self._get(self._contents + '?ref=' + STATE_BRANCH)
        if response.status_code != 200:
            raise StateError('REMOTE_STATE_UNAVAILABLE')
        try:
            body = response.json()
            raw = base64.b64decode(''.join(body['content'].split()), validate=True)
            sha = body['sha']
            if (body.get('encoding') != 'base64' or not raw or len(raw) > MAX_BYTES
                    or not re.fullmatch('[a-f0-9]{40}', sha) or sha != blob_sha(raw)
                    or body.get('path') != STATE_PATH):
                raise ValueError()
            return json.loads(raw.decode('utf-8')), sha
        except (KeyError, TypeError, ValueError, UnicodeDecodeError):
            raise StateError('REMOTE_STATE_CONTENT_INVALID') from None

    def load(self):
        state, sha = self._raw()
        return validate_delivery_state(state), sha

    def checkpoint(self, state, expected):
        previous, sha = self.load()
        if sha != expected:
            raise StateConflict('REMOTE_CAS_CONFLICT')
        validate_delivery_lineage(previous, state)
        return self._put(state, expected)

    def _put(self, state, expected):
        validate_delivery_state(state)
        raw = canonical(state)
        body = {'branch': STATE_BRANCH, 'message': 'chore: checkpoint Kemdiktisaintek experiment',
                'content': base64.b64encode(raw).decode()}
        if expected is not None:
            body['sha'] = expected
        try:
            response = self._session.put(self._contents, json=body, headers=self._headers,
                                         timeout=45, allow_redirects=False)
        except Exception:
            raise StateError('REMOTE_CHECKPOINT_OUTCOME_UNKNOWN') from None
        if response.status_code in {409, 422}:
            raise StateConflict('REMOTE_CAS_CONFLICT')
        try:
            if response.status_code not in {200, 201} or response.json()['content']['sha'] != blob_sha(raw):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise StateError('REMOTE_CHECKPOINT_RECEIPT_INVALID') from None
        return blob_sha(raw)

    def create(self, state, *, approved=False, empty_branch_sha=None):
        """Future operator-authorized initialization: existing empty tree at pinned SHA only."""
        if approved is not True or not re.fullmatch('[a-f0-9]{40}', empty_branch_sha or ''):
            raise StateError('REMOTE_INITIALIZATION_NOT_AUTHORIZED')
        ref = self._get(self._base + 'git/ref/heads/' + STATE_BRANCH)
        try:
            if ref.status_code != 200 or ref.json()['object']['sha'] != empty_branch_sha:
                raise ValueError()
            commit = self._get(self._base + 'git/commits/' + empty_branch_sha)
            tree_sha = commit.json()['tree']['sha']
            if (commit.status_code != 200 or not re.fullmatch('[a-f0-9]{40}', tree_sha)
                    or commit.json().get('parents') != []):
                raise ValueError()
            tree = self._get(self._base + 'git/trees/' + tree_sha)
            if tree.status_code != 200 or tree.json().get('tree') != [] or tree.json().get('truncated') is not False:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise StateError('REMOTE_BRANCH_NOT_REVIEWED_EMPTY_TREE') from None
        if self._get(self._contents + '?ref=' + STATE_BRANCH).status_code != 404:
            raise StateError('REMOTE_INITIALIZATION_REFUSES_OVERWRITE')
        if state['generation'] != 1 or state['outbox'] or state['admissions']:
            raise StateError('INVALID_INITIAL_REMOTE_STATE')
        return self._put(state, None)

    def migrate(self, *, expected, at, approved=False):
        if approved is not True:
            raise StateError('EXPLICIT_MIGRATION_APPROVAL_REQUIRED')
        previous, sha = self._raw()
        if expected != sha:
            raise StateConflict('REMOTE_CAS_CONFLICT')
        return self._put(migrate_v1(previous, at=at), expected)


class MemoryRemote:
    """Offline CAS backend with durable ordering semantics for tests/simulation."""
    durable = True

    def __init__(self, state):
        self.state = deepcopy(validate_delivery_state(state))
        self.sha = blob_sha(canonical(state))
        self.checkpoints = []

    def load(self):
        return deepcopy(validate_delivery_state(self.state)), self.sha

    def checkpoint(self, state, expected):
        if expected != self.sha:
            raise StateConflict('MOCK_REMOTE_CAS_CONFLICT')
        validate_delivery_lineage(self.state, state)
        self.state, self.sha = deepcopy(state), blob_sha(canonical(state))
        self.checkpoints.append(deepcopy(state))
        return self.sha
