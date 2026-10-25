"""CAS backend for the source-local Kemdiktisaintek production state file."""
import base64
import hashlib
import json
import re

import requests

from kemdiktisaintek_prod_state import (
    STATE_BRANCH, STATE_PATH, MAX_BYTES, StateError, StateConflict, canonical,
)
from kemdiktisaintek_prod_delivery_state import validate_delivery_state, validate_delivery_lineage

REPOSITORY = 'Galih-Hermawan-Unikom/bima-dikti-news'


def blob_sha(raw):
    return hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


class RemoteState:
    """Only reads/writes the Kemdiktisaintek file on dikti-production-state."""
    durable = True

    def __init__(self, token, *, session=None, repository=REPOSITORY,
                 branch=STATE_BRANCH, path=STATE_PATH):
        if (repository != REPOSITORY or branch != STATE_BRANCH or path != STATE_PATH
                or not isinstance(token, str) or not token):
            raise StateError('REMOTE_SCOPE_OR_TOKEN_INVALID')
        self.session = session or requests.Session()
        if session is None:
            self.session.trust_env = False
        self.headers = {'Authorization': 'Bearer ' + token,
            'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
        self.base = 'https://api.github.com/repos/' + REPOSITORY + '/'
        self.contents = self.base + 'contents/' + STATE_PATH

    def _get(self, url):
        try:
            return self.session.get(url, headers=self.headers, timeout=30, allow_redirects=False)
        except Exception:
            raise StateError('REMOTE_GET_FAILED') from None

    def _raw(self, *, missing_ok=False):
        response = self._get(self.contents + '?ref=' + STATE_BRANCH)
        if missing_ok and response.status_code == 404:
            return None, None
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
        raw = canonical(state)
        body = {'branch': STATE_BRANCH, 'message': 'chore: checkpoint Kemdiktisaintek production state',
                'content': base64.b64encode(raw).decode('ascii')}
        if expected is not None:
            body['sha'] = expected
        try:
            response = self.session.put(self.contents, json=body, headers=self.headers,
                                        timeout=45, allow_redirects=False)
        except Exception:
            raise StateError('REMOTE_CHECKPOINT_OUTCOME_UNKNOWN') from None
        if response.status_code in {409, 422}:
            raise StateConflict('REMOTE_CAS_CONFLICT')
        try:
            if (response.status_code not in {200, 201}
                    or response.json()['content']['sha'] != blob_sha(raw)):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise StateError('REMOTE_CHECKPOINT_RECEIPT_INVALID') from None
        return blob_sha(raw)

    def create(self, state, *, approved=False):
        """Create the source file once on the existing production state branch."""
        state = validate_delivery_state(state)
        if (approved is not True or state['generation'] != 1 or state['outbox']
                or state['admissions'] or state['test_authorizations']):
            raise StateError('REMOTE_INITIALIZATION_NOT_AUTHORIZED')
        ref = self._get(self.base + 'git/ref/heads/' + STATE_BRANCH)
        try:
            body = ref.json() if ref.status_code == 200 else {}
        except Exception:
            body = {}
        if ref.status_code != 200 or body.get('ref') != 'refs/heads/' + STATE_BRANCH:
            raise StateError('PRODUCTION_STATE_BRANCH_UNAVAILABLE')
        existing, _ = self._raw(missing_ok=True)
        if existing is not None:
            raise StateError('REMOTE_INITIALIZATION_REFUSES_OVERWRITE')
        return self._put(state, None)
