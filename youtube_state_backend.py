"""CAS-protected durable state file on a dedicated GitHub branch.

The state branch must be created explicitly. No historical cache or journal is
read, copied, or migrated by this backend.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import PurePosixPath
from pathlib import Path
import re
from urllib.parse import quote

import requests


class DurableStateError(RuntimeError):
    pass


class DurableStateConflict(DurableStateError):
    pass


class GitHubStateBranch:
    """Store one SQLite snapshot using GitHub Contents API compare-and-swap."""

    def __init__(self, repository, branch, path, token, *, initialize=False, session=None,
                 api="https://api.github.com"):
        normalized_path = path.replace("\\", "/") if isinstance(path, str) else ""
        if (not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                or not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", branch)
                or branch.lower() in {"master", "main"}
                or not normalized_path or normalized_path.startswith("/")
                or ".." in PurePosixPath(normalized_path).parts
                or not isinstance(token, str) or not token or type(initialize) is not bool):
            raise ValueError("Invalid durable state backend configuration")
        self.repository = repository
        self.branch = branch
        self.path = normalized_path
        self.manifest_path = self.path + ".manifest.json"
        self.initialize = initialize
        self._session = session or requests
        self._api = api.rstrip("/")
        self._remote_blob_sha = None
        self._manifest_blob_sha = None
        self._headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @classmethod
    def from_environment(cls, *, state_path=".bot-state/youtube_delivery.sqlite3", session=None):
        return cls(os.environ.get("GITHUB_REPOSITORY", ""),
                   os.environ.get("YOUTUBE_STATE_BRANCH", "youtube-state"), state_path,
                   os.environ.get("YOUTUBE_STATE_TOKEN", ""),
                   initialize=os.environ.get("YOUTUBE_STATE_INITIALIZE", "false").lower() == "true",
                   session=session)

    def _url(self, suffix):
        return f"{self._api}/repos/{self.repository}/{suffix}"

    def _ensure_branch(self):
        branch = quote(self.branch, safe="")
        response = self._session.get(self._url(f"git/ref/heads/{branch}"),
                                     headers=self._headers, timeout=20)
        if response.status_code != 200:
            raise DurableStateError(f"Durable state branch unavailable: HTTP {response.status_code}")

    def _contents_url(self, path):
        path = quote(path, safe="/")
        return self._url(f"contents/{path}")

    @staticmethod
    def _git_blob_sha(data):
        header = f"blob {len(data)}\0".encode("ascii")
        return hashlib.sha1(header + data).hexdigest()

    def _get_raw(self, path):
        headers = dict(self._headers)
        headers["Accept"] = "application/vnd.github.raw"
        return self._session.get(self._contents_url(path) + "?ref=" + quote(self.branch, safe=""),
                                 headers=headers, timeout=30)

    def load(self, destination):
        """Fetch the durable blob into a local scratch path; never fall back stale."""
        self._ensure_branch()
        response = self._get_raw(self.path)
        manifest_response = self._get_raw(self.manifest_path)
        if response.status_code == 404 and manifest_response.status_code == 404:
            if not self.initialize:
                raise DurableStateError("Durable state is not initialized; explicit no-send initialization required")
            self._remote_blob_sha = None
            self._manifest_blob_sha = None
            Path(destination).unlink(missing_ok=True)
            return False
        if response.status_code != 200 or manifest_response.status_code != 200:
            raise DurableStateError(f"Durable state download failed: HTTP {response.status_code}")
        data = response.content
        if not data or len(data) > 100 * 1024 * 1024:
            raise DurableStateError("Durable state blob size is invalid")
        try:
            manifest = json.loads(manifest_response.content.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise DurableStateError("Durable state manifest is invalid") from None
        if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
                or manifest.get("blob_sha") != self._git_blob_sha(data)):
            raise DurableStateError("Durable state manifest mismatch; recovery required")
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".download")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        self._remote_blob_sha = self._git_blob_sha(data)
        self._manifest_blob_sha = self._git_blob_sha(manifest_response.content)
        return True

    def _put_raw(self, path, data, expected_sha):
        params = {
            "message": "chore: checkpoint YouTube tabs state",
            "content": base64.b64encode(data).decode("ascii"),
            "branch": self.branch,
        }
        if expected_sha is not None:
            params["sha"] = expected_sha
        response = self._session.put(self._contents_url(path), json=params,
                                     headers=self._headers, timeout=45)
        if response.status_code in {409, 422}:
            raise DurableStateConflict("Concurrent durable state update; delivery halted")
        if response.status_code not in {200, 201}:
            raise DurableStateError(f"Durable state checkpoint failed: HTTP {response.status_code}")

    def persist(self, source):
        """Atomically update the branch file; stale writers fail without retry."""
        data = Path(source).read_bytes()
        if not data or len(data) > 100 * 1024 * 1024:
            raise DurableStateError("Durable state blob size is invalid")
        blob_sha = self._git_blob_sha(data)
        if blob_sha == self._remote_blob_sha:
            return False
        self._put_raw(self.path, data, self._remote_blob_sha)
        self._remote_blob_sha = blob_sha
        manifest_raw = json.dumps({"schema_version": 1, "blob_sha": blob_sha},
                                  sort_keys=True, separators=(",", ":")).encode("utf-8")
        manifest_sha = self._git_blob_sha(manifest_raw)
        self._put_raw(self.manifest_path, manifest_raw, self._manifest_blob_sha)
        self._manifest_blob_sha = manifest_sha
        return True

