"""Fail-closed GitHub scheduler dispatcher for production Kemdiktisaintek POLL."""
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REPOSITORY = 'Galih-Hermawan-Unikom/bima-dikti-news'
PRODUCTION_WORKFLOW = 'kemdiktisaintek-production.yml'
API_BASE = 'https://api.github.com'


class SchedulerError(RuntimeError):
    pass


def validate_gates(env):
    if env.get('KEMDIKTISAINTEK_PRODUCTION_SCHEDULER_ENABLED') != 'true':
        return None
    if (env.get('GITHUB_ACTIONS') != 'true'
            or env.get('GITHUB_REPOSITORY') != REPOSITORY
            or env.get('GITHUB_REF') != 'refs/heads/master'
            or env.get('GITHUB_EVENT_NAME') not in {'schedule', 'workflow_dispatch'}):
        raise SchedulerError('PRODUCTION_SCHEDULER_CONTEXT_DENIED')
    if env.get('KEMDIKTISAINTEK_PRODUCTION_ENABLED') != 'true':
        raise SchedulerError('PRODUCTION_SOURCE_SWITCH_OFF')
    auto_send = env.get('KEMDIKTISAINTEK_PRODUCTION_AUTO_SEND')
    if auto_send not in {'false', 'true'}:
        raise SchedulerError('PRODUCTION_AUTO_SEND_VALUE_INVALID')
    sha = env.get('GITHUB_SHA', '')
    if (not re.fullmatch('[a-f0-9]{40}', sha)
            or env.get('KEMDIKTISAINTEK_PRODUCTION_APPROVED_CODE_SHA') != sha):
        raise SchedulerError('APPROVED_MASTER_SHA_MISMATCH')
    if not env.get('GITHUB_TOKEN'):
        raise SchedulerError('GITHUB_TOKEN_UNAVAILABLE')
    return {'sha': sha, 'auto_send': auto_send}


class GitHubAPI:
    def __init__(self, token, *, opener=urlopen):
        self.token = token
        self.opener = opener

    def _request(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode('utf-8')
        request = Request(API_BASE + path, data=data, method=method, headers={
            'Authorization': 'Bearer ' + self.token,
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'Content-Type': 'application/json',
        })
        try:
            with self.opener(request, timeout=15) as response:
                status = response.status
                body = response.read()
        except HTTPError as exc:
            raise SchedulerError(f'GITHUB_API_HTTP_{exc.code}') from None
        except (URLError, TimeoutError, OSError):
            raise SchedulerError('GITHUB_API_REQUEST_FAILED') from None
        if method == 'POST':
            if status != 204:
                raise SchedulerError('WORKFLOW_DISPATCH_NOT_ACKNOWLEDGED')
            return None
        if status != 200:
            raise SchedulerError('GITHUB_API_READ_FAILED')
        try:
            return json.loads(body.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            raise SchedulerError('GITHUB_API_RESPONSE_INVALID') from None

    def get(self, path):
        return self._request('GET', path)

    def post(self, path, payload):
        return self._request('POST', path, payload)


def run(env, api=None):
    gates = validate_gates(env)
    if gates is None:
        return {'status': 'OFF', 'dispatches': 0}
    api = api or GitHubAPI(env['GITHUB_TOKEN'])
    ref = api.get(f'/repos/{REPOSITORY}/git/ref/heads/master')
    if ref.get('object', {}).get('sha') != gates['sha']:
        raise SchedulerError('MASTER_HEAD_CHANGED_DURING_SCHEDULER_RUN')
    workflow = api.get(f'/repos/{REPOSITORY}/actions/workflows/{PRODUCTION_WORKFLOW}')
    if workflow.get('state') != 'active':
        raise SchedulerError('PRODUCTION_WORKFLOW_NOT_ACTIVE')
    api.post(f'/repos/{REPOSITORY}/actions/workflows/{PRODUCTION_WORKFLOW}/dispatches', {
        'ref': 'master',
        'inputs': {'mode': 'POLL', 'reviewed_code_sha': gates['sha']},
    })
    return {'status': 'DISPATCHED', 'dispatches': 1, 'sha': gates['sha'],
            'auto_send': gates['auto_send'], 'mode': 'POLL'}


def main():
    try:
        result = run(os.environ)
        print('PRODUCTION_SCHEDULER=' + json.dumps(result, sort_keys=True))
        return 0
    except SchedulerError as exc:
        print('PRODUCTION_SCHEDULER=BLOCKED reason=' + str(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
