"""Explicit manual modes. No environment/secret loading at import; no implicit sending."""
import argparse
from datetime import datetime, timezone
import json
import os
import re

from kemdiktisaintek_parser import AnnouncementClient
from kemdiktisaintek_monitor import capture_window, initialize
from kemdiktisaintek_state import StateError
from kemdiktisaintek_delivery_state import migrate_v1
from kemdiktisaintek_remote_state import RemoteState, REPOSITORY
from kemdiktisaintek_delivery import Delivery, ReconciliationRequired
from kemdiktisaintek_telegram import Credentials, TelegramGateway, switches
from kemdiktisaintek_automation import run_poll


class _BaselineSink:
    def create(self, state):
        self.state = state


def run(mode, env, *, clock=None, backend=None, source=None, gateway=None):
    clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
    if mode not in {'PREFLIGHT', 'COLLECT', 'INITIALIZE', 'POLL', 'SEND_TEST'}:
        raise StateError('INVALID_MANUAL_MODE')
    if mode == 'COLLECT':
        client = source or AnnouncementClient()
        try:
            cap = capture_window(client, at=clock(), pages=2, detail_budget=3)
            return {'mode': mode, 'delivery_authorized': False, 'coverage': cap.coverage, 'items': cap.items}
        finally:
            if source is None:
                client.session.close()
    if mode == 'SEND_TEST':
        switches(env)
        if (env.get('KEMDIKTISAINTEK_SEND_TEST_APPROVAL') != 'SEND_ONE_EXPERIMENT_TEST'
                or not re.fullmatch('[a-f0-9]{40}', env.get('GITHUB_SHA', ''))
                or env.get('KEMDIKTISAINTEK_REVIEWED_CODE_SHA') != env['GITHUB_SHA']
                or env.get('GITHUB_ACTIONS') != 'true' or env.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
                or env.get('GITHUB_REPOSITORY') != REPOSITORY):
            raise StateError('SEND_TEST_OPERATOR_OR_CODE_APPROVAL_MISSING')
    if mode == 'PREFLIGHT' and env.get('KEMDIKTISAINTEK_PREFLIGHT_APPROVAL') != 'READ_ONLY_PREFLIGHT':
        raise StateError('LIVE_PREFLIGHT_NOT_AUTHORIZED')
    if mode == 'INITIALIZE' and env.get('KEMDIKTISAINTEK_INITIALIZE_APPROVAL') != 'INITIALIZE_REVIEWED_EMPTY_BRANCH':
        raise StateError('REMOTE_INITIALIZATION_NOT_AUTHORIZED')
    remote = backend or RemoteState(env.get('KEMDIKTISAINTEK_STATE_TOKEN', ''))
    if mode in {'PREFLIGHT', 'SEND_TEST'}:
        remote.load()  # Missing/invalid schema/baseline/remote state prevents even a send preflight.
        telegram = gateway or TelegramGateway(Credentials.from_environment(env))
        try:
            proof = telegram.inspect(at=clock())
            if mode == 'PREFLIGHT':
                enabled = (env.get('KEMDIKTISAINTEK_EXPERIMENT_ENABLED') == 'true'
                           and env.get('KEMDIKTISAINTEK_EXPERIMENT_AUTO_SEND') == 'true')
                return {'mode': mode, 'destination_alias': 'EXPERIMENT-' + proof.identity['chat_sha256'][:12], 'preflight': 'PASS',
                        'send_switches_enabled': enabled, 'post_attempted': False}
            delivery = Delivery(remote, telegram, environment=env, clock=clock)
            delivery.recover()
            key = delivery.reserve_test(env.get('KEMDIKTISAINTEK_TEST_ID', ''), proof,
                                       approval=env['KEMDIKTISAINTEK_SEND_TEST_APPROVAL'])
            return delivery.send(key, proof)
        finally:
            if gateway is None:
                telegram.session.close()
    if mode == 'POLL':
        return run_poll(env, backend=remote, clock=clock, source=source, gateway=gateway)
    client = source or AnnouncementClient()
    try:
        cap = capture_window(client, at=clock(), pages=2, detail_budget=10)
        sink = _BaselineSink()
        initial = initialize(sink, cap)
        state = migrate_v1(initial, at=clock())
        remote.create(state, approved=True, empty_branch_sha=env.get('KEMDIKTISAINTEK_EMPTY_BRANCH_SHA'))
        return {'mode': mode, 'initialized': True, 'post_attempted': False}
    finally:
        if source is None:
            client.session.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['PREFLIGHT', 'COLLECT', 'INITIALIZE', 'POLL', 'SEND_TEST'])
    args = parser.parse_args(argv)
    try:
        report = run(args.mode, os.environ)
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 1 if (report.get('status') == 'AMBIGUOUS' or report.get('ambiguous_this_poll', 0)
                      or report.get('api_health') == 'FAILED') else 0
    except Exception as exc:
        # Fixed code/class only; never render a request exception or token-bearing URL.
        safe = str(exc) if isinstance(exc, StateError) else type(exc).__name__
        print(json.dumps({'status': 'BLOCKED', 'error': safe, 'post_attempted': 'unknown' if
                          isinstance(exc, ReconciliationRequired) else False,
                          'possible_receipt': exc.receipt if isinstance(exc, ReconciliationRequired) else None}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
