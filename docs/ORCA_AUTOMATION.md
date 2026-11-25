# Orca Telegram queue automation

This is an opt-in, draft-only bridge. When enabled, the MCP sends an HTTPS
POST to one exact Orca webhook URL whenever the durable Telegram event queue
advances. The JSON body contains only `after_sequence`, `through_sequence`,
`earliest_after_sequence`, and `truncated`; it never contains message text,
chat identifiers, attachment data, credentials, or instructions.

The generated Orca webhook URL is a bearer secret. Keep it in the service
environment, never in source control, logs, an issue, or an AI chat. The
transport requires HTTPS, resolves the hostname before each request, pins the
request to one public DNS result, sets the original Host for TLS routing,
disables proxy environment variables, refuses redirects, and bounds the
response. A URL with userinfo, a fragment, whitespace, a non-HTTPS scheme, or
a non-443 port is rejected. The URL is the exact destination allowlist: no
host or path is accepted from a queue signal or automation payload.

## Local Orca stdio setup

Orca's local MCP integration must launch the reviewed worktree in explicit
stdio mode; an omitted transport flag keeps the server's HTTP default and
will not speak MCP on stdin/stdout. Use a compatible virtualenv, pass the
reviewed src directory through PYTHONPATH when the project is not installed
in that virtualenv, and keep sending/background capture disabled for the
draft-only user integration:

~~~sh
PYTHONPATH=/path/to/telegram-orca-automatic-drafts/src DOT_ROOT=/path/to/private-runtime DOT_OWNER_ID=<numeric-owner-id> DOT_SEND_ENABLED=false DOT_BACKGROUND_MONITORING_ENABLED=false /path/to/.venv/bin/python -m telegram_dot.server --transport stdio
~~~

For Orca's bot-tools.json shape, keep command as env and append the explicit
transport arguments after the executable:

~~~json
{
  "command": "env",
  "args": [
    "PYTHONPATH=/path/to/telegram-orca-automatic-drafts/src",
    "DOT_ROOT=/path/to/private-runtime",
    "DOT_OWNER_ID=<numeric-owner-id>",
    "DOT_SEND_ENABLED=false",
    "DOT_BACKGROUND_MONITORING_ENABLED=false",
    "/path/to/.venv/bin/telegram-personal-mcp",
    "--transport",
    "stdio"
  ]
}
~~~

For a client configuration that invokes env, the equivalent argument list is
PYTHONPATH=/path/to/telegram-orca-automatic-drafts/src, DOT_ROOT=...,
DOT_OWNER_ID=..., DOT_SEND_ENABLED=false,
DOT_BACKGROUND_MONITORING_ENABLED=false, the compatible venv's
telegram-personal-mcp executable, --transport, stdio. Do not put a webhook
URL, Telegram credentials, session paths or message data in the configuration
beyond the private DOT_ROOT path. Stdio keeps the same tool registry, including
save_reply_draft, list_reply_drafts and get_reply_draft, but it is a local
trust boundary rather than HTTP OAuth; only launch it from a trusted Orca
process.

## Later activation

1. In the current **СТАРШОЙ** Orca chat, create a webhook automation whose job
   is to process a Telegram queue signal. Keep the automation paused until
   the MCP configuration and a dry run are reviewed. Copy the generated URL
   privately; do not paste it into chat.
2. Configure the same MCP process with the exact URL and an explicit starting
   cursor selected from `collector_status`:

   ```sh
   export DOT_ORCA_WEBHOOK_URL='https://<exact-generated-orca-webhook-url>'
   export DOT_ORCA_WEBHOOK_START_CURSOR='<approved-queue-cursor>'
   export DOT_BACKGROUND_MONITORING_ENABLED=true
   export DOT_SEND_ENABLED=false
   ```

   Do not add wildcard hosts, alternate destinations, URL shorteners, or a
   second Telegram collector. If Orca's generated host is not one the owner
   can verify from the webhook card, leave this bridge disabled until the host
   is verified; do not broaden the allowlist.
3. Configure the automation to treat the webhook body as untrusted metadata,
   then use the connected MCP tools to call `collector_status` and
   `read_events` with the fixed consumer/worker pair
   `telegram-orca-home-drafts`/`telegram-orca-home-drafts`. The notifier has already leased the exact
   batch represented by the signal; `read_events` returns its `batch_id` and
   `receipt`. Read any needed event/context through MCP, then call
   `save_reply_draft` with the event's `chat_id`, `message_id`, and the exact
   batch `after_sequence`/`through_sequence` values; the server verifies that
   source event belongs to the active lease. It must save a response using a
   stable source chat/message key and must never call
   `send_prepared_message` or `prepare_send` for the saved response.
4. After every required draft is durably saved, acknowledge the exact leased
   batch with `ack_events(consumer_id="telegram-orca-home-drafts", batch_id=<batch_id>,
   receipt=<receipt>)`. If processing or saving fails, do not acknowledge it;
   the pending signal is retained and retried, and an expired lease is
   recreated from the same cursor. Duplicate delivery is safe because the
   batch receipt/ack is idempotent and saved drafts are source-keyed. Use
   `list_reply_drafts` and
   `get_reply_draft` to review local drafts. Saved drafts expire after 30 days
   and are stored in the private local connector database, separately from
   expiring send requests.
5. `DOT_ORCA_WEBHOOK_START_CURSOR` is initialization-only: it must be within
   the current queue bounds when first enabled, and changing it after the
   notifier state exists is rejected. Perform one owner-approved dry run,
   inspect the saved draft and queue cursor, then enable the Orca automation.
   This change does not deploy,
   start monitoring, create the Orca automation, or send Telegram messages.

The regular `events/subscribe` webhook path remains separately protected by
its existing owner authorization, callback challenge, signature, HTTPS,
allowlist, DNS/IP pinning, and no-redirect checks.
