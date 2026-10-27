"""Manual-only runtime for controlled, UUID-scoped editorial recovery."""
import json
import os
from datetime import datetime, timezone

from kemdiktisaintek_editorial_delivery import EditorialDelivery, EditorialReconciliationRequired
from kemdiktisaintek_editorial_state import EditorialRemoteState
from kemdiktisaintek_prod_parser import AnnouncementClient
from kemdiktisaintek_prod_remote_state import RemoteState
from kemdiktisaintek_prod_state import StateError
from kemdiktisaintek_prod_telegram import Credentials, TelegramGateway


def run(mode, identifier, confirmation, env=None, *, production_backend=None, ledger_backend=None,
        client=None, gateway=None, clock=None):
    env = os.environ if env is None else env
    clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
    mode = str(mode or '').upper()
    if mode not in {'PREVIEW', 'AUTHORIZE', 'SEND'}:
        raise StateError('EDITORIAL_MODE_INVALID')
    production_backend = production_backend or RemoteState(env.get('GITHUB_TOKEN', ''))
    ledger_backend = ledger_backend or EditorialRemoteState(env.get('GITHUB_TOKEN', ''))
    own_client = client is None
    client = client or AnnouncementClient()
    own_gateway = False
    if gateway is None and mode in {'AUTHORIZE', 'SEND'}:
        credentials = Credentials.from_environment(env)
        gateway = TelegramGateway(credentials)
        own_gateway = True
    try:
        service = EditorialDelivery(production_backend, ledger_backend, client, gateway,
                                    env=env, clock=clock)
        if mode == 'PREVIEW':
            return service.preview(identifier)
        if mode == 'AUTHORIZE':
            return service.authorize(identifier, confirmation)
        return service.send(identifier, confirmation)
    finally:
        if own_client:
            client.session.close()
        if own_gateway:
            gateway.session.close()


def main():
    try:
        result = run(os.environ.get('EDITORIAL_MODE'), os.environ.get('EDITORIAL_UUID'),
                     os.environ.get('EDITORIAL_CONFIRMATION'))
        print('KEMDIKTISAINTEK_EDITORIAL=' + json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except EditorialReconciliationRequired:
        print('KEMDIKTISAINTEK_EDITORIAL=' + json.dumps({'status': 'RECONCILIATION_REQUIRED',
            'error': 'EDITORIAL_RECEIPT_RECONCILIATION_REQUIRED', 'post_attempted': True,
            'automatic_retry': False}, ensure_ascii=True, sort_keys=True))
        return 1
    except Exception as exc:
        error = str(exc) if isinstance(exc, StateError) else type(exc).__name__
        print('KEMDIKTISAINTEK_EDITORIAL=' + json.dumps({'status': 'BLOCKED', 'error': error,
            'post_attempted': False}, ensure_ascii=True, sort_keys=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
