"""Manual, source-isolated Kemdiktisaintek production runtime."""
import argparse
from datetime import datetime, timezone
import json
import os
import re

from kemdiktisaintek_prod_parser import AnnouncementClient
from kemdiktisaintek_prod_monitor import capture_window, initialize
from kemdiktisaintek_prod_state import StateError
from kemdiktisaintek_prod_delivery_state import migrate_v1
from kemdiktisaintek_prod_remote_state import RemoteState, REPOSITORY
from kemdiktisaintek_prod_automation import run_poll


class _BaselineSink:
    def create(self, state):
        self.state = state


def _review_gate(env, *, initialize_mode=False):
    reviewed = env.get('KEMDIKTISAINTEK_PRODUCTION_REVIEWED_SHA', '')
    approved = env.get('KEMDIKTISAINTEK_PRODUCTION_APPROVED_SHA', '')
    if (env.get('GITHUB_ACTIONS') != 'true' or env.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
            or env.get('GITHUB_REPOSITORY') != REPOSITORY
            or env.get('GITHUB_REF') != 'refs/heads/master'
            or not re.fullmatch('[a-f0-9]{40}', reviewed)
            or reviewed != approved or reviewed != env.get('GITHUB_SHA')):
        raise StateError('PRODUCTION_REVIEWED_SHA_GATE_BLOCKED')
    if initialize_mode and env.get('KEMDIKTISAINTEK_PRODUCTION_INITIALIZE_APPROVAL') != 'INITIALIZE_REVIEWED_PRODUCTION_STATE':
        raise StateError('PRODUCTION_INITIALIZATION_NOT_AUTHORIZED')


def run(mode, env, *, clock=None, backend=None, source=None, gateway=None):
    if mode not in {'COLLECT', 'INITIALIZE', 'POLL'}:
        raise StateError('INVALID_PRODUCTION_MODE')
    clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
    if mode == 'COLLECT':
        client = source or AnnouncementClient()
        try:
            capture = capture_window(client, at=clock(), pages=2, detail_budget=10)
            return {'mode': mode, 'source': 'kemdiktisaintek', 'delivery_authorized': False,
                    'api_health': 'HEALTHY', 'coverage': capture.coverage,
                    'items': len(capture.items), 'post_attempted': False}
        finally:
            if source is None:
                client.session.close()
    _review_gate(env, initialize_mode=(mode == 'INITIALIZE'))
    remote = backend or RemoteState(env.get('GITHUB_TOKEN', ''))
    if mode == 'INITIALIZE':
        client = source or AnnouncementClient()
        try:
            capture = capture_window(client, at=clock(), pages=2, detail_budget=10)
        finally:
            if source is None:
                client.session.close()
        sink = _BaselineSink()
        initial = initialize(sink, capture)
        state = migrate_v1(initial, at=clock())
        sha = remote.create(state, approved=True)
        return {'mode': mode, 'source': 'kemdiktisaintek', 'initialized': True,
                'baseline_count': len(initial['baseline']['ids']), 'state_blob_sha': sha,
                'archive_complete': False, 'delivery_authorized': False, 'post_attempted': False}
    if env.get('KEMDIKTISAINTEK_PRODUCTION_ENABLED') != 'true':
        raise StateError('PRODUCTION_SOURCE_SWITCH_OFF')
    return run_poll(env, backend=remote, clock=clock, source=source, gateway=gateway)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['COLLECT', 'INITIALIZE', 'POLL'])
    args = parser.parse_args(argv)
    try:
        report = run(args.mode, os.environ)
        print('KEMDIKTISAINTEK_PRODUCTION_RUN=' + json.dumps(report, ensure_ascii=True, sort_keys=True))
        return 1 if report.get('api_health') == 'FAILED' or report.get('ambiguous_this_poll', 0) else 0
    except Exception as exc:
        safe = str(exc) if isinstance(exc, StateError) else type(exc).__name__
        print('KEMDIKTISAINTEK_PRODUCTION_RUN=' + json.dumps(
            {'status': 'BLOCKED', 'error': safe, 'post_attempted': False}, sort_keys=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
