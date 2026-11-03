"""1031c: provider-bound Slack DMs and classic Outlook drafts.

No connection, credentials, COM access or service starts at import time. Bodies
exist only in memory. Provider failures are deliberately not logged with payloads.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import urllib.request


def digest(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Message:
    provider: str
    account: str
    conversation: str
    recipients: tuple
    label: str
    body: str = field(repr=False)
    cc: tuple = ()
    bcc: tuple = ()
    subject: str = field(default="", repr=False)
    attachments: tuple = ()  # (filename, SHA256 of actual attachment bytes)
    revision: str = ""
    person: bool = False

    @property
    def scope(self):
        return (self.provider, self.account, self.conversation, self.recipients)

    @property
    def fingerprint(self):
        return digest((self.scope, self.body, self.cc, self.bcc, self.subject,
                       self.attachments, self.revision))

    def preview(self, full=False):
        body = self.body if full else " ".join(self.body.split()[:8])
        if self.provider == "outlook":
            files = ", ".join(a[0] for a in self.attachments) or "none"
            return (f"Email from {self.account}. To: {', '.join(self.recipients)}. "
                    f"CC: {', '.join(self.cc) or 'none'}. BCC: {', '.join(self.bcc) or 'none'}. "
                    f"Subject: {self.subject or 'none'}. Attachments: {files}. Body: {body}")
        return (f"Slack from {self.account}, conversation {self.conversation}, "
                f"to {self.label} ({', '.join(self.recipients)}): {body}")


@dataclass(frozen=True)
class Receipt:
    state: str  # sent = provider observed submission, never proof of reading/delivery
    provider_id: str = ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never forward an Authorization header elsewhere.


class SlackAdapter:
    """One explicit local setup row; display names never resolve recipients.

    The token is fetched from an environment variable, never from voice/model
    arguments. Read-only preflight verifies the authenticated sender and IM peer.
    chat.postMessage is called once; even HTTP errors/timeouts are not retried.
    """
    def __init__(self, settings, request=None):
        self.settings = dict(settings)
        self.request = request or self._request

    def _request(self, method, payload):
        if method not in {"auth.test", "conversations.info", "chat.postMessage"}:
            raise ValueError("Unsupported Slack method")
        token = os.environ[self.settings["token_env"]]
        request = urllib.request.Request("https://slack.com/api/" + method,
            data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json; charset=utf-8"})
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=10) as response:
            result = json.load(response)
        if result.get("ok") is not True:
            raise ValueError("Slack request refused")
        return result

    def snapshot(self, body):
        cfg = self.settings
        if not all(isinstance(cfg.get(k), str) and cfg[k].strip()
                   for k in ("team", "sender", "conversation", "recipient", "token_env", "name")):
            raise ValueError("Incomplete Slack setup")
        auth = self.request("auth.test", {})
        if (auth.get("team_id"), auth.get("user_id")) != (cfg["team"], cfg["sender"]):
            raise ValueError("Slack sending account changed")
        channel = self.request("conversations.info", {"channel": cfg["conversation"]})["channel"]
        if (channel.get("id") != cfg["conversation"] or channel.get("is_im") is not True
                or channel.get("user") != cfg["recipient"] or channel.get("is_archived")):
            raise ValueError("Slack destination is not the configured individual DM")
        return Message("slack", cfg["team"] + "/" + cfg["sender"], cfg["conversation"],
                       (cfg["recipient"],), cfg["name"], body,
                       revision=digest(tuple(sorted(cfg.items()))), person=True)

    def send(self, message):
        if self.snapshot(message.body) != message:
            return Receipt("not_sent")
        result = self.request("chat.postMessage", {"channel": message.conversation,
            "text": message.body, "mrkdwn": False, "parse": "none", "link_names": False,
            "unfurl_links": False, "unfurl_media": False})
        observed = result.get("message", {})
        if (result.get("ok") is True and result.get("channel") == message.conversation
                and result.get("ts") and observed.get("text") == message.body
                and observed.get("user") == self.settings["sender"]):
            return Receipt("sent", str(result["ts"]))
        return Receipt("unknown")


@contextmanager
def _outlook_item(hwnd):
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    try:
        # GetActiveObject never launches Outlook. Only the active inspector qualifies.
        inspector = win32com.client.GetActiveObject("Outlook.Application").ActiveInspector()
        if inspector is None or int(inspector._oleobj_.QueryInterface(pythoncom.IID_IOleWindow).GetWindow()) != hwnd:
            raise ValueError("Outlook draft is not in front")
        item = inspector.CurrentItem
        if item.Class != 43 or item.Sent:
            raise ValueError("No unsent Outlook mail draft")
        yield item
    finally:
        pythoncom.CoUninitialize()


class OutlookAdapter:
    """Reads a complete draft without saving, resolving, or altering it."""
    def __init__(self, hwnd, item_context=None):
        self.hwnd = hwnd
        self.item_context = item_context or _outlook_item

    @staticmethod
    def _address(recipient):
        if not recipient.Resolved:
            raise ValueError("Unresolved email recipient")
        entry = recipient.AddressEntry
        address = (entry.GetExchangeUser().PrimarySmtpAddress if entry.Type == "EX" else entry.Address)
        if not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+", address or ""):
            raise ValueError("Recipient SMTP address unavailable")
        return address

    def _snapshot(self, item):
        if item.Class != 43 or item.Sent:
            raise ValueError("No unsent Outlook mail draft")
        account = item.SendUsingAccount
        if account is None or not account.SmtpAddress:
            raise ValueError("Explicit Outlook sending account required")
        recipients = {1: [], 2: [], 3: []}
        for index in range(1, item.Recipients.Count + 1):
            recipient = item.Recipients.Item(index)
            recipients[recipient.Type].append(self._address(recipient))
        if not any(recipients.values()):
            raise ValueError("No email recipients")
        attachments = []
        for index in range(1, item.Attachments.Count + 1):
            attachment = item.Attachments.Item(index)
            if attachment.Type != 1:
                raise ValueError("Unsupported email attachment; review in Outlook")
            data = attachment.PropertyAccessor.GetProperty(
                "http://schemas.microsoft.com/mapi/proptag/0x37010102")
            attachments.append((attachment.FileName, hashlib.sha256(bytes(data)).hexdigest()))
        return Message("outlook", account.SmtpAddress, str(self.hwnd) + "/" + str(item.EntryID),
            tuple(recipients[1]), "Outlook email", item.Body, tuple(recipients[2]), tuple(recipients[3]),
            item.Subject, tuple(attachments), digest((item.HTMLBody, item.BodyFormat)))

    def snapshot(self, body=None):
        with self.item_context(self.hwnd) as item:
            return self._snapshot(item)

    def send(self, message):
        with self.item_context(self.hwnd) as item:
            if self._snapshot(item) != message:
                return Receipt("not_sent")
            item.Send()  # Exactly one invocation; COM returning alone is not a sent receipt.
            try:
                return Receipt("sent", str(item.EntryID)) if item.Sent else Receipt("unknown")
            except Exception:
                return Receipt("unknown")
