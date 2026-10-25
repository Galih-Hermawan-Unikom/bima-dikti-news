from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_production_scheduler_is_observation_only_and_fail_closed():
    workflow = (ROOT / '.github' / 'workflows' /
                'kemdiktisaintek-production-scheduler.yml').read_text(encoding='utf-8')

    assert "cron: '7,37 * * * *'" in workflow
    assert 'workflow_dispatch:' in workflow
    assert 'KEMDIKTISAINTEK_PRODUCTION_SCHEDULER_ENABLED || \'false\'' in workflow
    assert 'KEMDIKTISAINTEK_PRODUCTION_ENABLED || \'false\'' in workflow
    assert "AUTO_SEND: ${{ vars.KEMDIKTISAINTEK_PRODUCTION_AUTO_SEND || 'false' }}" in workflow
    assert "process.env.AUTO_SEND !== 'false'" in workflow
    assert "APPROVED_MASTER_SHA !== sha" in workflow
    assert "master.data.object.sha !== sha" in workflow
    assert "workflow_id: 'kemdiktisaintek-production.yml'" in workflow
    assert "inputs: { mode: 'POLL', reviewed_code_sha: sha }" in workflow
    assert 'actions: write' in workflow and 'contents: read' in workflow
    assert 'TELEGRAM_BOT_TOKEN' not in workflow
    assert 'TELEGRAM_CHAT_ID' not in workflow
    assert 'PRODUCTION_TELEGRAM_CHAT_ID_SHA256' not in workflow
    assert 'INITIALIZE' not in workflow
    assert 'dikti-production-state' not in workflow
