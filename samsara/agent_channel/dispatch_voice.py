"""Signed Agora voice-dispatch client. Drafting never authorizes a launch."""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass

from samsara import agora_bridge


@dataclass(frozen=True)
class DispatchDraft:
    spoken: str
    task_id: str
    plan_hash: str
    prompt_id: str


class DispatchVoice:
    def __init__(self, base_url: str = agora_bridge.DEFAULT_BASE_URL, timeout: float = 5.0):
        self.base_url, self.timeout = base_url, timeout

    def draft(self, prompt_id: str, identity: str, effort: str | None = None) -> DispatchDraft:
        response = agora_bridge.send_dispatch_draft(
            prompt_id, identity, effort, base_url=self.base_url, timeout=self.timeout)
        if not response.get("accepted") or response.get("state") != "draft":
            raise ValueError("Agora did not create a draft")
        return DispatchDraft(response["summary"], response["task_id"],
                             response["plan_hash"], response["prompt_id"])

    def approve(self, task_id: str, plan_hash: str) -> dict:
        return agora_bridge.dispatch_request(
            "POST", agora_bridge.DISPATCH_APPROVE_PATH,
            {"task_id": task_id, "plan_hash": plan_hash, "source": "voice"},
            base_url=self.base_url, timeout=self.timeout)

    def pending(self) -> list[dict]:
        response = agora_bridge.dispatch_request(
            "GET", agora_bridge.DISPATCH_PENDING_PATH,
            base_url=self.base_url, timeout=self.timeout)
        return response["pending"]

    def events(self, since: str | None = None) -> list[dict]:
        path = agora_bridge.DISPATCH_EVENTS_PATH
        if since:
            path += "?" + urllib.parse.urlencode({"since": since})
        response = agora_bridge.dispatch_request(
            "GET", path, base_url=self.base_url, timeout=self.timeout)
        return response["events"]
