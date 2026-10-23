"""Single-blob GitHub Contents API backend for versioned YouTube JSON state."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path, PurePosixPath
import re
from urllib.parse import quote

import requests


MAX_STATE_BYTES = 900_000


class DurableStateError(RuntimeError):
    pass


class DurableStateConflict(DurableStateError):
    pass


class GitHubStateBranch:
    """Load/checkpoint exactly one state file with branch and blob SHA CAS."""

    def __init__(self, repo, branch, path, token, *, initialize=False, session=None,
                 api="https://api.github.com", validator=None):
        normalized_path = path.replace("\\", "/") if isinstance(path, str) else ""
        if (not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
                or not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", branch)
                or branch.lower() in {"master", "main"} or not normalized_path
                or normalized_path.startswith("/") or ".." in PurePosixPath(normalized_path).parts
                or not isinstance(token, str) or not token or type(initialize) is not bool):
            raise ValueError("Invalid durable state backend configuration")
        self.repo, self.branch, self.path = repo, branch, normalized_path
        self.initialize = initialize
        self._session = session or requests
        self._api = api.rstrip("/")
        self._remote_sha = None
        self._validator = validator
        self._headers = {"Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

    @classmethod
    def from_environment(cls, *, state_path=".bot-state/youtube_delivery_state.json", session=None):
        import os
        return cls(os.environ.get("GITHUB_REPOSITORY", ""),
            os.environ.get("YOUTUBE_STATE_BRANCH", "youtube-state"), state_path,
            os.environ.get("YOUTUBE_STATE_TOKEN", ""),
            initialize=os.environ.get("YOUTUBE_STATE_INITIALIZE", "false").lower() == "true",
            session=session)

    def _url(self, suffix):
        return f"{self._api}/repos/{self.repo}/{suffix}"

    def _ensure_branch(self):
        branch = quote(self.branch, safe="")
        response = self._session.get(self._url(f"git/ref/heads/{branch}"),
            headers=self._headers, timeout=20)
        if response.status_code != 200:
            raise DurableStateError(f"Durable state branch unavailable: HTTP {response.status_code}")

    def _contents_url(self):
        return self._url("contents/" + quote(self.path, safe="/"))

    def load(self, dst):
        """Download JSON into a disposable Actions path; never use stale fallback."""
        self._ensure_branch()
        response = self._session.get(self._contents_url() + "?ref=" + quote(self.branch, safe=""),
            headers=self._headers, timeout=30)
        if response.status_code == 404 and self.initialize:
            self._remote_sha = None
            Path(dst).unlink(missing_ok=True)
            return False
        if response.status_code != 200:
            raise DurableStateError(f"Durable state download failed: HTTP {response.status_code}")
        try:
            body = response.json()
            content = base64.b64decode(body["content"], validate=False)
            sha = body["sha"]
            state = json.loads(content.decode("utf-8"))
        except (KeyError, TypeError, ValueError, UnicodeDecodeError):
            raise DurableStateError("Durable JSON state response is invalid") from None
        if (not isinstance(sha, str) or len(sha) != 40 or not content
                or len(content) > MAX_STATE_BYTES or not isinstance(state, dict)
                or state.get("schema_version") != 1):
            raise DurableStateError("Durable JSON state schema or size is invalid")
        from youtube_delivery import YouTubeStateStore
        try:
            (self._validator or YouTubeStateStore._validate_state)(state)
        except (TypeError, ValueError, KeyError):
            raise DurableStateError("Durable JSON state validation failed") from None
        target = Path(dst)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".download")
        try:
            tmp.write_bytes(content)
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        self._remote_sha = sha
        return True

    def persist(self, src):
        """One Contents API PUT per full-state checkpoint, guarded by expected SHA."""
        data = Path(src).read_bytes()
        if not data or len(data) > MAX_STATE_BYTES:
            raise DurableStateError("Durable JSON state size is invalid")
        try:
            state = json.loads(data.decode("utf-8"))
            from youtube_delivery import YouTubeStateStore
            (self._validator or YouTubeStateStore._validate_state)(state)
        except (UnicodeDecodeError, ValueError, TypeError, KeyError):
            raise DurableStateError("Refusing invalid YouTube JSON checkpoint") from None
        params = {"message": "chore: checkpoint YouTube tabs JSON state",
            "content": base64.b64encode(data).decode("ascii"), "branch": self.branch}
        if self._remote_sha is not None:
            params["sha"] = self._remote_sha
        response = self._session.put(self._contents_url(), json=params,
            headers=self._headers, timeout=45)
        if response.status_code in {409, 422}:
            raise DurableStateConflict("Concurrent durable state update; delivery halted")
        if response.status_code not in {200, 201}:
            raise DurableStateError(f"Durable state checkpoint failed: HTTP {response.status_code}")
        try:
            new_sha = response.json()["content"]["sha"]
        except (KeyError, TypeError, ValueError):
            raise DurableStateError("Durable checkpoint receipt is invalid") from None
        if not isinstance(new_sha, str) or len(new_sha) != 40:
            raise DurableStateError("Durable checkpoint receipt is invalid")
        self._remote_sha = new_sha
