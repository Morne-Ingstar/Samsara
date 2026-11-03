# Private-message and email adapters

In command mode, say `slack message Pat saying I will be there at six`.
The configured Slack account and individual DM are checked against Slack's
provider IDs before a preview appears. Until that exact conversation has been
approved for the short grace, sending asks one bound question. Say `yes` or
`cancel`. Approving a send does not also approve future sends.

Say `trust slack conversation Pat` to approve the exact Slack sending account,
recipient and conversation for future user-authored messages. After that setup,
the recipient and first eight body words are shown and spoken. A single
one-second grace starts when spoken feedback completes (or immediately if speech
is unavailable). `send now` ends it early; `no`, `cancel` and `stop` cancel it.
Speech onset holds the send during decoding; continued dictation cancels it.
Trust does not downgrade model, macro or scheduled messages, or email.

Slack setup is config-only and empty by default. Add a row under `messaging.slack`:

```json
{
  "messaging": {
    "slack": [
      {
        "name": "Pat",
        "team": "workspace-provider-id",
        "sender": "sending-user-provider-id",
        "recipient": "recipient-user-provider-id",
        "conversation": "individual-dm-provider-id",
        "token_env": "SAMSARA_SLACK_TOKEN"
      }
    ],
    "trusted_slack": []
  }
}
```

Use actual provider IDs from the intended Slack account and existing individual
DM. Put the credential in the named environment variable before starting Samsara;
never dictate it. The adapter uses `auth.test`, `conversations.info` and
`chat.postMessage`. Duplicate contact names, group channels, different accounts
and mismatched peers are refused. The trust command saves an exact scope in
`messaging.trusted_slack`; remove that scope to revoke the short grace. No account
or trust is automatically created, and no environment variable is configured by
this change.

For email, open a classic Outlook draft and say `send outlook email`. The preview
includes the explicit sending account, To, CC, BCC, subject, attachment names and
the first eight body words. Say `read full message` while the question is pending
to hear the complete body, then `yes` to send or `cancel` to keep the draft.
Long visual previews may be shortened by the existing display; the spoken
preview and full readback remain available on Compact and Full displays.
Changing any recipient, account, subject, body, HTML or attachment bytes invalidates
approval. Unresolved recipients, implicit sending accounts and unsupported
attachments are refused. The adapter does not save or alter the draft to inspect it.

Supported classic Outlook keyboard, wake-profile and Send-click routes use the
same complete draft preview. Raw input/click attempts have an unknown receipt
unless the provider is observed; pressing a key alone is never called "sent".
New Outlook, Thunderbird, webmail and other messaging providers do not acquire a
dedicated adapter from this change. Native email applications without a complete
readable preview or an existing local adapter's bound account, conversation and
recipient IDs refuse Send. Existing local adapters retain their bound confirmation
and final destination check; provider snapshots add the complete preview and
readback. Web/browser controls retain their existing effect policy. The Smart
Actions email tool remains unimplemented.

The direct adapters report `Sent: provider observed submission` only for a
matching provider receipt; that does not prove delivery or reading. A timeout,
exception or uncertain result says to check the conversation. A second attempt
for the same message snapshot in the same command generation is refused, including
after success. There is no automatic retry, persistent message store or advertised
unsend/recall. These in-memory attempt receipts do not survive application restart.

Provider/account and real microphone verification is still required before
claiming live readiness. Automated coverage uses synthetic messages and inert
transports; it sends no email or DM.
