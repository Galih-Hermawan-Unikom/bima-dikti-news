import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import youtube_experiment_pipeline as pipeline
from youtube_experiment_preflight import PreflightError


def _environment(monkeypatch):
    values = {
        "GITHUB_ACTIONS": "true", "GITHUB_TOKEN": "STATE_TOKEN_TEST_ONLY",
        "GITHUB_REPOSITORY": "example/news",
        "YOUTUBE_OBSERVATION_ENABLED": "true",
        "YOUTUBE_AUTOMATIC_TABS_VERIFICATION_ENABLED": "true",
        "YOUTUBE_DELIVERY_ENABLED": "true", "YOUTUBE_DELIVERY_SEND_ENABLED": "false",
        "YOUTUBE_STATE_INITIALIZE": "false", "YOUTUBE_POLICY_APPROVED": "true",
        "YOUTUBE_ACTIVATION_AT": pipeline.ACTIVATION_FENCE,
        "YOUTUBE_POLICY_VERSION": pipeline.POLICY_VERSION,
        "YOUTUBE_STATE_BRANCH": "youtube-state",
        "YOUTUBE_CHANNEL_ID": "UCGo_6l_6kp8H8OHcKcSIeDw",
        "YOUTUBE_WINDOW_SECONDS": "86400", "YOUTUBE_TTL_SECONDS": "14400",
        "YOUTUBE_SKEW_SECONDS": "300", "YOUTUBE_CAPTURE_INTERVAL_SECONDS": "600",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("preflight_passes", [True, False])
def test_pipeline_keeps_observation_but_plans_only_after_preflight(tmp_path, monkeypatch, capsys, preflight_passes):
    _environment(monkeypatch)
    repo = Path(__file__).resolve().parents[1]
    for name in ("announcements_cache.json", "youtube_cache.json", "delivery_state.json"):
        (tmp_path / name).write_bytes((repo / name).read_bytes())
    monkeypatch.chdir(tmp_path)

    class Backend:
        def __init__(self, *args, **kwargs):
            pass

        def load(self, dst):
            cache = json.loads((tmp_path / "youtube_cache.json").read_text(encoding="utf-8"))
            observations = {item["video_id"]: {"first_seen_at": pipeline.ACTIVATION_FENCE,
                "initial_inventory": True} for item in cache[:pipeline.EXPECTED_INVENTORY_SIZE]}
            state = {"schema_version": 1, "activation_at": "2026-10-22T23:17:48+00:00",
                "policy_version": pipeline.POLICY_VERSION, "capture_seq": 0, "last_capture_at": None,
                "initialized": True, "needs_inventory": False, "observations": observations,
                "captures": [], "outbox": {}}
            Path(dst).write_text(json.dumps(state), encoding="utf-8")
            return True

        @staticmethod
        def persist(_path):
            return None

    monkeypatch.setattr(pipeline, "GitHubStateBranch", Backend)
    if preflight_passes:
        monkeypatch.setattr(pipeline, "verify_experiment_destination", lambda: {
            "destination_alias": "EXPERIMENT-123456789abc"})
    else:
        def blocked():
            raise PreflightError("ALLOWLIST_MISMATCH")
        monkeypatch.setattr(pipeline, "verify_experiment_destination", blocked)

    received = {}
    def run_once(config, **kwargs):
        received.update(config=config, kwargs=kwargs)
        return SimpleNamespace(errors=[], youtube_eligibility={"decisions": [], "outbox": []},
            sources=[SimpleNamespace(source="youtube", status="ELIGIBILITY_PREVIEW")])
    monkeypatch.setattr(pipeline, "run_once", run_once)

    assert pipeline.run() == 0
    assert received["config"]["youtube_observation_enabled"] is True
    assert received["config"]["youtube_automatic_tabs_verification_enabled"] is True
    assert received["config"]["youtube_delivery_enabled"] is preflight_passes
    assert received["config"]["youtube_delivery_send_enabled"] is False
    assert received["kwargs"]["youtube_experiment_only"] is True
    assert received["kwargs"]["youtube_send_limit"] == 1
    assert received["kwargs"]["scraper"].check_new_announcements is not None
    output = capsys.readouterr().out
    assert '"sending": "OFF"' in output
    assert ('"preflight": "PASS"' in output) is preflight_passes
    assert 'ALLOWLIST_MISMATCH' in output if not preflight_passes else 'ALLOWLIST_MISMATCH' not in output
