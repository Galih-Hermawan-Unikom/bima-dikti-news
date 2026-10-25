"""Default-branch scheduler gate and dispatch to the pinned experiment branch."""
import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPOSITORY = "Galih-Hermawan-Unikom/bima-dikti-news"
FEATURE_BRANCH = "feat/kemdiktisaintek-telegram-stage3"
WORKFLOW = "kemdiktisaintek-experiment.yml"
API = "https://api.github.com"


class SchedulerError(RuntimeError):
    pass


def plan(env, feature_head_sha):
    """Only master + exact operator-approved feature HEAD may dispatch POLL."""
    if env.get("GITHUB_REF") != "refs/heads/master" or env.get("GITHUB_REPOSITORY") != REPOSITORY:
        return {"dispatch": False, "reason": "UNEXPECTED_SCHEDULER_CONTEXT"}
    if env.get("KEMDIKTISAINTEK_SCHEDULER_ENABLED") != "true":
        return {"dispatch": False, "reason": "SCHEDULER_SWITCH_OFF"}
    approved = env.get("KEMDIKTISAINTEK_EXPERIMENT_APPROVED_CODE_SHA", "")
    if not re.fullmatch(r"[a-f0-9]{40}", approved) or approved != feature_head_sha:
        return {"dispatch": False, "reason": "APPROVED_FEATURE_SHA_MISMATCH"}
    return {"dispatch": True, "repository": REPOSITORY, "workflow": WORKFLOW,
            "ref": FEATURE_BRANCH, "mode": "POLL", "reviewed_code_sha": approved}


def _request(method, path, token, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(API + path, data=data, method=method, headers={
        "Accept": "application/vnd.github+json", "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=15) as response:
            body = response.read()
            return response.status, json.loads(body) if body else None
    except (HTTPError, URLError, TimeoutError):
        raise SchedulerError("GITHUB_DISPATCH_API_FAILED") from None


def dispatch(env, request=_request):
    """Read target branch, compare approved SHA, then dispatch exactly one POLL."""
    token = env.get("GITHUB_TOKEN", "")
    if not token:
        raise SchedulerError("SCHEDULER_TOKEN_MISSING")
    status, result = request("GET", f"/repos/{REPOSITORY}/git/ref/heads/{FEATURE_BRANCH}", token)
    if status != 200 or not isinstance(result, dict):
        raise SchedulerError("FEATURE_REF_LOOKUP_FAILED")
    feature_sha = result.get("object", {}).get("sha")
    plan_row = plan(env, feature_sha)
    if not plan_row["dispatch"]:
        return plan_row
    payload = {"ref": FEATURE_BRANCH, "inputs": {"mode": "POLL",
        "reviewed_code_sha": plan_row["reviewed_code_sha"], "confirmation": "SCHEDULED_POLL",
        "empty_branch_sha": "", "test_id": "", "bima_reference_path": "", "bima_reference_sha256": ""}}
    status, _ = request("POST", f"/repos/{REPOSITORY}/actions/workflows/{WORKFLOW}/dispatches", token, payload)
    if status not in (200, 201, 202, 204):
        raise SchedulerError("WORKFLOW_DISPATCH_REJECTED")
    return {"dispatch": True, "mode": "POLL", "ref": FEATURE_BRANCH,
            "reviewed_code_sha": plan_row["reviewed_code_sha"]}


def main():
    import os
    try:
        result = dispatch(os.environ)
        print(json.dumps(result, sort_keys=True))
        return 0
    except SchedulerError as exc:
        print(json.dumps({"dispatch": False, "error": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
