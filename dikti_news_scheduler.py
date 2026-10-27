"""Pinned unified scheduling and source adapters. No Telegram calls in the planner."""
import argparse
import json
import os
from pathlib import Path
import re
from urllib.request import Request, urlopen

REPO = 'Galih-Hermawan-Unikom/bima-dikti-news'
WORKFLOW = '.github/workflows/dikti-news-production.yml'
FULL_CRON = '17 */3 * * *'
LIGHT_CRON = '17 1-23/3,2-23/3 * * *'
PREFIX = {'bima': 'BIMA_PRODUCTION', 'youtube': 'YOUTUBE_PRODUCTION',
          'kemdiktisaintek': 'KEMDIKTISAINTEK_PRODUCTION'}


class ScheduleError(RuntimeError):
    pass


def master_head(env):
    token = env.get('GITHUB_TOKEN', '')
    if not token:
        raise ScheduleError('GITHUB_TOKEN_REQUIRED')
    request = Request(f'https://api.github.com/repos/{REPO}/git/ref/heads/master',
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                 'X-GitHub-Api-Version': '2022-11-28'})
    try:
        with urlopen(request, timeout=20) as response:
            if response.status != 200:
                raise ValueError()
            return json.load(response)['object']['sha']
    except Exception:
        raise ScheduleError('MASTER_HEAD_VERIFICATION_FAILED') from None


def plan(env, *, read_head=master_head):
    if (env.get('GITHUB_ACTIONS') != 'true' or env.get('GITHUB_REPOSITORY') != REPO
            or env.get('GITHUB_REF') != 'refs/heads/master'
            or env.get('GITHUB_WORKFLOW_REF') != f'{REPO}/{WORKFLOW}@refs/heads/master'
            or env.get('GITHUB_EVENT_NAME') not in {'schedule', 'workflow_dispatch'}):
        raise ScheduleError('UNIFIED_CONTEXT_DENIED')
    if env.get('DIKTI_NEWS_PRODUCTION_SCHEDULER_ENABLED', 'false') != 'true':
        return {'status': 'OFF', **{source: False for source in PREFIX}}
    sha = env.get('GITHUB_SHA', '')
    if (not re.fullmatch('[a-f0-9]{40}', sha)
            or env.get('KEMDIKTISAINTEK_PRODUCTION_APPROVED_CODE_SHA') != sha):
        raise ScheduleError('APPROVED_MASTER_SHA_MISMATCH')
    if read_head(env) != sha:
        raise ScheduleError('MASTER_HEAD_CHANGED')
    if env['GITHUB_EVENT_NAME'] == 'schedule':
        cron = env.get('SCHEDULE_CRON')
        if cron not in {FULL_CRON, LIGHT_CRON}:
            raise ScheduleError('UNKNOWN_SCHEDULE')
        selected = set(PREFIX) if cron == FULL_CRON else {'youtube', 'kemdiktisaintek'}
        observation = False
    else:
        selection = env.get('MANUAL_SOURCES', 'all')
        if selection not in {'all', *PREFIX} or env.get('OBSERVATION_ONLY', 'true') not in {'true', 'false'}:
            raise ScheduleError('INVALID_MANUAL_INPUT')
        selected = set(PREFIX) if selection == 'all' else {selection}
        observation = env.get('OBSERVATION_ONLY', 'true') == 'true'
    result = {'status': 'READY', 'sha': sha, 'observation_only': observation}
    for source, prefix in PREFIX.items():
        enabled, auto = env.get(prefix + '_ENABLED', 'false'), env.get(prefix + '_AUTO_SEND', 'false')
        if enabled not in {'true', 'false'} or auto not in {'true', 'false'}:
            raise ScheduleError('INVALID_SOURCE_SWITCH:' + source)
        result[source] = source in selected and enabled == 'true'
        result[source + '_auto_send'] = result[source] and auto == 'true' and not observation
    return result


def run_source(source, env, *, read_head=master_head, bima_youtube=None, kemdikti=None):
    """Recheck pinned HEAD after waiting for the shared job lock, before any runtime."""
    result = plan(env, read_head=read_head)
    if source not in PREFIX or not result.get(source):
        raise ScheduleError('SOURCE_NOT_ADMITTED')
    child = dict(env)
    auto = 'true' if result[source + '_auto_send'] else 'false'
    if source == 'kemdiktisaintek':
        if kemdikti is None:
            from kemdiktisaintek_prod_runtime import run as kemdikti
        child.update(KEMDIKTISAINTEK_PRODUCTION_REVIEWED_SHA=result['sha'],
                     KEMDIKTISAINTEK_PRODUCTION_APPROVED_SHA=result['sha'],
                     KEMDIKTISAINTEK_PRODUCTION_AUTO_SEND=auto)
        report = kemdikti('POLL', child)
        if report.get('api_health') == 'FAILED' or report.get('ambiguous_this_poll') or report.get('delivery_error'):
            raise ScheduleError('KEMDIKTISAINTEK_POLL_REQUIRES_ATTENTION')
        return report
    if bima_youtube is None:
        import production_runtime
        def bima_youtube(name, mode):
            before = production_runtime.hashes()
            try:
                return production_runtime.run(name, mode)
            finally:
                if production_runtime.hashes() != before:
                    raise ScheduleError('HISTORICAL_MASTER_STATE_CHANGED')
    child.update(PRODUCTION_SOURCE_ENABLED='true', PRODUCTION_AUTO_SEND=auto,
                 YOUTUBE_MONITOR_ENABLED='true' if source == 'youtube' else 'false',
                 TELEGRAM_SEND_FILES='true' if source == 'bima' else 'false')
    previous = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(child)
        code = bima_youtube(source, 'run')
        if code != 0:
            raise ScheduleError('SOURCE_RUNTIME_FAILED:' + source)
        return {'source': source, 'runtime_exit_code': code,
                'planned_interval_seconds': 10800 if source == 'bima' else 3600}
    finally:
        os.environ.clear()
        os.environ.update(previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['plan', *PREFIX])
    args = parser.parse_args()
    try:
        result = plan(os.environ) if args.mode == 'plan' else run_source(args.mode, os.environ)
        if args.mode == 'plan' and os.environ.get('GITHUB_OUTPUT'):
            with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as stream:
                for key, value in result.items():
                    stream.write(f'{key}={str(value).lower() if isinstance(value, bool) else value}\n')
        print('DIKTI_NEWS_PRODUCTION=' + json.dumps(result, sort_keys=True))
        return 0
    except ScheduleError as exc:
        print('DIKTI_NEWS_PRODUCTION_BLOCKED=' + str(exc))
        return 1
    except Exception as exc:
        # Match production_runtime's safe failure reporting. Request exceptions
        # can contain Telegram URLs; never print the raw exception or traceback.
        print('DIKTI_NEWS_PRODUCTION_BLOCKED=' + type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
