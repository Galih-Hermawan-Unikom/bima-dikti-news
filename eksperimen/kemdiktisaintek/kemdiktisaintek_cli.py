"""Stage 2 local state and read-only collection. No Telegram or remote state mode."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from kemdiktisaintek_parser import AnnouncementClient
from kemdiktisaintek_monitor import capture_window, initialize, poll
from kemdiktisaintek_state import LocalStateStore, StateError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['collect', 'initialize', 'poll'])
    parser.add_argument('--state-dir')
    parser.add_argument('--bima-history', help='Explicit read-only JSON reference snapshot; never modified')
    parser.add_argument('--pages', type=int, default=2)
    parser.add_argument('--details', type=int, default=10)
    args = parser.parse_args(argv)
    client = None
    try:
        store = LocalStateStore(args.state_dir) if args.state_dir else None
        if args.mode != 'collect' and store is None:
            raise StateError('EXPLICIT_LOCAL_STATE_DIRECTORY_REQUIRED')
        if args.mode == 'initialize':
            store.assert_can_initialize()
        if args.mode == 'poll':
            store.load()  # fail before request on missing/corrupt state
        history = json.loads(Path(args.bima_history).read_text(encoding='utf-8')) if args.bima_history else None
        at = datetime.now(timezone.utc).isoformat()
        client = AnnouncementClient()
        if args.mode == 'poll':
            result = poll(store, client, at=at, bima_history=history, pages=args.pages, detail_budget=args.details)
        else:
            capture = capture_window(client, at=at, pages=args.pages, detail_budget=args.details)
            if args.mode == 'initialize':
                state = initialize(store, capture)
                result = {'mode': 'initialize', 'baseline': state['baseline'],
                          'observations': len(state['observations']), 'delivery_authorized': False}
            else:
                result = {'mode': 'collect', 'delivery_authorized': False, 'observed_at': capture.observed_at,
                          'coverage': capture.coverage, 'items': capture.items, 'duplicate_ids': capture.duplicate_ids}
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except Exception as exc:
        # Safe code/class only; no request URL, secrets, response bodies or traceback.
        code = str(exc) if isinstance(exc, StateError) else type(exc).__name__
        print(json.dumps({'status': 'FAILED', 'error': code, 'delivery_authorized': False}))
        return 1
    finally:
        if client:
            client.session.close()


if __name__ == '__main__':
    raise SystemExit(main())
