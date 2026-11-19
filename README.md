# Personal Telegram MCP

**Early beta:** offline-tested; real-client OAuth, live delivery and deployment still need environment-specific validation.

Self-hosted access to your own Telegram cloud chats for an AI assistant. Search conversations, inspect context, prepare and send messages or replies, and process a durable event queue. No hosted service or shared Telegram account is required.

## Features
- Read chats, messages and bounded search snapshots without marking messages read.
- **Send messages and replies**, with exact drafts, request deduplication and delivery readback.
- Recoverable event journal for creates, edits, deletions and explicit gaps.
- Leased batches with explicit processing acknowledgement, plus webhook notification support.
- Authenticated attachment downloads, expiring local cache and single-owner OAuth/PKCE.

## Quick start
Python 3.11+ and your own Telegram API application are required.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
export DOT_ROOT="$PWD/runtime"
telegram-personal-mcp-setup
```
Run setup **yourself** in a private terminal. Obtain your API ID/hash from https://my.telegram.org. Setup asks for Telegram sign-in and a separate MCP pairing password. Never paste credentials, OTPs or session files into AI chat or GitHub.

Set the following environment variables (use the numeric ID printed by setup):
```sh
export DOT_BASE_URL=https://telegram.example.com
export DOT_OWNER_ID=123456789
export DOT_SEND_ENABLED=true
export DOT_BACKGROUND_MONITORING_ENABLED=true
telegram-personal-mcp
```
Put a trusted HTTPS reverse proxy in front of port 8341 and expose `/mcp`, the OAuth routes and `/owner-login`. Forward the original Host header. Keep port 8341 private. `DOT_BASE_URL` must match the external origin exactly. The pairing password is entered only on your own `/owner-login` page.

Connect an OAuth-capable MCP client to `https://telegram.example.com/mcp`. ChatGPT callbacks and constrained local loopback callbacks are included. Webhook push currently accepts OpenAI/ChatGPT callback hosts only; other clients should use pull tools. Configuring OAuth callbacks does not extend webhook compatibility. Other clients may need their **exact** HTTPS callback in `DOT_OAUTH_REDIRECT_URIS` (comma-separated). Do not wildcard callback domains. Client compatibility is not comprehensively tested; event webhooks in particular may require custom client support.

## Sending and replying
Sending is enabled by `DOT_SEND_ENABLED=true`. The assistant calls `prepare_send` with a stable request key and optional reply target, shows the draft, then calls `send_prepared_message` with the approved recipient and text. Retrying the same draft does not send twice. `uncertain` means delivery cannot be established: inspect the chat before retrying with another request key. Approval is enforced by the client/user workflow, **not independently proven by the server**. Only connect trusted clients. Setting the flag to false disables sends without removing read/event features.

## Background events
Choose an initial cursor explicitly using `collector_status`, lease with `read_events`, process every event, then acknowledge that exact batch with `ack_events`. Reading or receiving a webhook is not acknowledgement. If media cannot be processed, do not pretend success: record a durable deferred-work item in the consumer and acknowledge only when your consumer's documented processing contract allows that deferral. The server has no automatic transcription. Handle gap events and expired cursors explicitly.

## Limits
Secret Chats are unavailable. Deletion events and offline recovery are not guaranteed complete. Global search is bounded. Text send/reply are supported; edits, deletion, reactions, file uploads and chat administration are not yet exposed. Attachments: 25 MiB each, 250 MiB cache, one-hour TTL. Events: 30 days / 100,000 events. One owner per deployment; not a multi-tenant SaaS.

## Development
```sh
pip install -e '.[dev]'
pytest -q
python scripts/scan_release.py
```
Tests use temporary local stores and fake Telegram clients, never your account. See SECURITY.md and RELEASE_CHECKLIST.md before deploying. Dockerfile is provided as a starting point; mount runtime with ownership suitable for UID 10001. No credentials or production data are included.

## License and provenance
MIT, copyright Daniil Tagirov. See LICENSE and PROVENANCE.md. Third-party dependencies retain their own licenses.

### Troubleshooting
- Existing session: setup refuses to overwrite it. For a new account use a new `--root` directory. Never delete a working session just to rerun setup.
- Interrupted setup after Telegram login: if the session exists but api.json or pairing_key is missing, use a new empty runtime directory and repeat setup; revoke the abandoned session in Telegram if needed.
- Invalid pairing password: setup now rejects it before Telegram authentication.
- The example `.env` file is documentation, not automatically loaded. Export variables in your shell or explicitly load them through your deployment manager.
- Never start multiple collectors with the same Telegram session or shared runtime directory.
