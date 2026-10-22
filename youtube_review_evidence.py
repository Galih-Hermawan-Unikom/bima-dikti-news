"""Verify explicit operator reviews; never infer category/readiness from a tile.

HMAC authenticates a review by a configured authority, not a YouTube claim by
itself. The reviewer must inspect the referenced source. No keys are loaded from
disk/environment and no authority is configured by default.
"""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import hmac
import json
from urllib.parse import urlsplit

import youtube_eligibility as e


@dataclass(frozen=True)
class ReviewedEvidence:
    category: e.CategoryEvidence = e.CategoryEvidence()
    release: e.ReleaseEvidence = e.ReleaseEvidence()
    admission: e.AdmissionEvidence = e.AdmissionEvidence()


class ReviewVerificationError(ValueError):
    pass


class SignedReviewVerifier:
    """Explicitly injected trusted keys and publisher host allowlist, dry-run only.

    Envelopes use {key_id, payload, signature}; signature is HMAC-SHA256 of UTF-8
    JSON(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True).
    Tests use synthetic keys/reviews. A valid signature does not auto-approve:
    approved must be exactly True with reviewer/history reference and expiry.
    """
    FIELDS = frozenset({"schema", "video_id", "channel_id", "scope", "policy_version",
        "state_fingerprint", "category", "readiness", "source_url", "reviewer",
        "history_review_reference", "observed_at", "expires_at", "ended_at",
        "first_seen_at", "approved"})

    def __init__(self, keys, allowed_source_hosts):
        if (not isinstance(keys, dict) or any(not isinstance(k, str) or not k
                or type(v) is not bytes or len(v) < 32 for k, v in keys.items())):
            raise ValueError("Invalid review authority configuration")
        if (not isinstance(allowed_source_hosts, frozenset)
                or any(not isinstance(h, str) or not h for h in allowed_source_hosts)):
            raise ValueError("Invalid source allowlist")
        self._keys = dict(keys)
        self._hosts = allowed_source_hosts

    def verify(self, envelope, *, video_id, channel_id, policy, fingerprint, first_seen_at):
        try:
            if not isinstance(envelope, dict) or set(envelope) != {"key_id", "payload", "signature"}:
                raise ValueError()
            key = self._keys[envelope["key_id"]]
            p = envelope["payload"]
            if not isinstance(p, dict) or set(p) != self.FIELDS:
                raise ValueError()
            raw = json.dumps(p, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
            expected = hmac.new(key, raw, hashlib.sha256).hexdigest()
            if not isinstance(envelope["signature"], str) or not hmac.compare_digest(expected, envelope["signature"]):
                raise ValueError()
            if (p["schema"] != "operator-youtube-tabs-review-v1" or p["video_id"] != video_id
                    or p["channel_id"] != channel_id or p["scope"] != policy.scope
                    or p["policy_version"] != policy.version or p["state_fingerprint"] != fingerprint
                    or p["approved"] is not True):
                raise ValueError()
            for field in ("reviewer", "history_review_reference", "source_url"):
                if not isinstance(p[field], str) or not p[field].strip():
                    raise ValueError()
            url = urlsplit(p["source_url"])
            if (url.scheme != "https" or url.hostname not in self._hosts or url.username
                    or url.password or url.port not in (None, 443) or "/shorts" in url.path.lower()):
                raise ValueError()
            if url.hostname in {"www.youtube.com", "youtube.com"}:
                if p["source_url"] != "https://www.youtube.com/watch?v=" + video_id:
                    raise ValueError()
            times = {}
            for field in ("observed_at", "expires_at", "first_seen_at", "ended_at"):
                if field == "ended_at" and p[field] is None:
                    times[field] = None
                    continue
                value = datetime.fromisoformat(p[field].replace("Z", "+00:00"))
                if value.tzinfo is None or value.utcoffset() is None:
                    raise ValueError()
                times[field] = value
            category, readiness = e.Category(p["category"]), e.Readiness(p["readiness"])
            if (category is e.Category.UNKNOWN or readiness is e.Readiness.UNKNOWN
                    or times["first_seen_at"] != first_seen_at
                    or times["first_seen_at"] > times["observed_at"]
                    or times["expires_at"] <= times["observed_at"]):
                raise ValueError()
            reference = "review-sha256:" + hashlib.sha256(raw).hexdigest()
            proof = e.Proof(video_id, channel_id, reference, times["observed_at"], policy.scope, True)
            return ReviewedEvidence(
                e.CategoryEvidence(category, e.CategoryMethod.MANUAL, proof),
                e.ReleaseEvidence(readiness, proof, times["ended_at"]),
                e.AdmissionEvidence(proof, policy.version, p["history_review_reference"], fingerprint,
                    times["expires_at"], reviewer=p["reviewer"], approved=True))
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            # Never expose envelope contents, key material or arbitrary source URLs.
            raise ReviewVerificationError("REVIEW_EVIDENCE_UNVERIFIED") from None
