from pathlib import Path

import pytest

from kemdiktisaintek_prod_scheduler import SchedulerError, run


ROOT = Path(__file__).resolve().parents[1]


class FakeGitHubAPI:
    def __init__(self, *, master_sha=None, workflow_state='active'):
        self.master_sha = master_sha
        self.workflow_state = workflow_state
        self.gets = []
        self.posts = []

    def get(self, path):
        self.gets.append(path)
        if path.endswith('/git/ref/heads/master'):
            return {'object': {'sha': self.master_sha}}
        assert path.endswith('/actions/workflows/kemdiktisaintek-production.yml')
        return {'state': self.workflow_state}

    def post(self, path, payload):
        self.posts.append((path, payload))


def scheduler_env(**overrides):
    values = {
        'GITHUB_ACTIONS': 'true',
        'GITHUB_EVENT_NAME': 'schedule',
        'GITHUB_REPOSITORY': 'Galih-Hermawan-Unikom/bima-dikti-news',
        'GITHUB_REF': 'refs/heads/master',
        'GITHUB_SHA': 'a' * 40,
        'GITHUB_TOKEN': 'MOCK_GITHUB_TOKEN',
        'KEMDIKTISAINTEK_PRODUCTION_SCHEDULER_ENABLED': 'true',
        'KEMDIKTISAINTEK_PRODUCTION_ENABLED': 'true',
        'KEMDIKTISAINTEK_PRODUCTION_AUTO_SEND': 'false',
        'KEMDIKTISAINTEK_PRODUCTION_APPROVED_CODE_SHA': 'a' * 40,
    }
    values.update(overrides)
    return values


def test_auto_send_false_dispatches_one_pinned_poll():
    api = FakeGitHubAPI(master_sha='a' * 40)

    result = run(scheduler_env(), api)

    assert result['status'] == 'DISPATCHED'
    assert result['auto_send'] == 'false'
    assert len(api.posts) == 1
    assert api.posts[0][1] == {'ref': 'master', 'inputs': {
        'mode': 'POLL', 'reviewed_code_sha': 'a' * 40}}


def test_auto_send_true_dispatches_only_after_all_gates_pass():
    api = FakeGitHubAPI(master_sha='a' * 40)

    result = run(scheduler_env(KEMDIKTISAINTEK_PRODUCTION_AUTO_SEND='true'), api)

    assert result['status'] == 'DISPATCHED'
    assert result['auto_send'] == 'true'
    assert len(api.posts) == 1


def test_source_switch_off_prevents_dispatch():
    api = FakeGitHubAPI(master_sha='a' * 40)

    with pytest.raises(SchedulerError, match='PRODUCTION_SOURCE_SWITCH_OFF'):
        run(scheduler_env(KEMDIKTISAINTEK_PRODUCTION_ENABLED='false',
                          KEMDIKTISAINTEK_PRODUCTION_AUTO_SEND='true'), api)

    assert api.gets == [] and api.posts == []


def test_approved_sha_mismatch_prevents_dispatch():
    api = FakeGitHubAPI(master_sha='a' * 40)

    with pytest.raises(SchedulerError, match='APPROVED_MASTER_SHA_MISMATCH'):
        run(scheduler_env(KEMDIKTISAINTEK_PRODUCTION_APPROVED_CODE_SHA='b' * 40), api)

    assert api.gets == [] and api.posts == []


@pytest.mark.parametrize(('master_sha', 'workflow_state', 'reason'), [
    ('b' * 40, 'active', 'MASTER_HEAD_CHANGED_DURING_SCHEDULER_RUN'),
    ('a' * 40, 'disabled_manually', 'PRODUCTION_WORKFLOW_NOT_ACTIVE'),
])
def test_remote_sha_or_workflow_state_mismatch_prevents_dispatch(master_sha, workflow_state, reason):
    api = FakeGitHubAPI(master_sha=master_sha, workflow_state=workflow_state)

    with pytest.raises(SchedulerError, match=reason):
        run(scheduler_env(KEMDIKTISAINTEK_PRODUCTION_AUTO_SEND='true'), api)

    assert api.posts == []


@pytest.mark.parametrize('invalid', ['', 'TRUE', '1', 'yes'])
def test_invalid_auto_send_value_prevents_dispatch(invalid):
    api = FakeGitHubAPI(master_sha='a' * 40)

    with pytest.raises(SchedulerError, match='PRODUCTION_AUTO_SEND_VALUE_INVALID'):
        run(scheduler_env(KEMDIKTISAINTEK_PRODUCTION_AUTO_SEND=invalid), api)

    assert api.gets == [] and api.posts == []


def test_production_scheduler_is_observation_only_and_fail_closed():
    workflow = (ROOT / '.github' / 'workflows' /
                'kemdiktisaintek-production-scheduler.yml').read_text(encoding='utf-8')
    script = (ROOT / 'kemdiktisaintek_prod_scheduler.py').read_text(encoding='utf-8')
    production = (ROOT / '.github' / 'workflows' /
                  'kemdiktisaintek-production.yml').read_text(encoding='utf-8')

    assert "schedule:" not in workflow
    assert "DIKTI_NEWS_PRODUCTION_SCHEDULER_ENABLED != 'true'" in workflow
    assert 'workflow_dispatch:' in workflow
    assert 'KEMDIKTISAINTEK_PRODUCTION_SCHEDULER_ENABLED || \'false\'' in workflow
    assert 'KEMDIKTISAINTEK_PRODUCTION_ENABLED || \'false\'' in workflow
    assert 'KEMDIKTISAINTEK_PRODUCTION_AUTO_SEND || \'false\'' in workflow
    assert "auto_send not in {'false', 'true'}" in script
    assert "KEMDIKTISAINTEK_PRODUCTION_APPROVED_CODE_SHA') != sha" in script
    assert "mode': 'POLL'" in script
    assert 'actions: write' in workflow and 'contents: read' in workflow
    assert 'TELEGRAM_BOT_TOKEN' not in workflow
    assert 'TELEGRAM_CHAT_ID' not in workflow
    assert 'TELEGRAM_' not in script
    assert 'PRODUCTION_TELEGRAM_CHAT_ID_SHA256' not in workflow
    assert 'INITIALIZE' not in workflow
    assert 'dikti-production-state' not in workflow
    for name in ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'PRODUCTION_TELEGRAM_CHAT_ID_SHA256'):
        line = next(line for line in production.splitlines() if name in line)
        assert "vars.KEMDIKTISAINTEK_PRODUCTION_AUTO_SEND == 'true'" in line
        assert "vars.KEMDIKTISAINTEK_PRODUCTION_ENABLED == 'true'" in line


def test_scheduler_switch_off_is_a_successful_noop():
    api = FakeGitHubAPI(master_sha='a' * 40)

    result = run(scheduler_env(KEMDIKTISAINTEK_PRODUCTION_SCHEDULER_ENABLED='false'), api)

    assert result == {'status': 'OFF', 'dispatches': 0}
    assert api.gets == [] and api.posts == []
