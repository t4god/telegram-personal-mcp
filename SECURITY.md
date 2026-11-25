# Security and privacy

This software can read private conversations and send as the connected account. Use a dedicated account if appropriate, trusted MCP clients, TLS, private storage and restrictive host permissions. Never run an anonymous public MCP endpoint.

Runtime sessions, API credentials, pairing keys, OAuth tokens, message databases and attachment caches are private. They are deliberately excluded from the release. Backups contain the same sensitive material. A Telegram session grants account access; revoke it in Telegram if compromised. OAuth client access can be revoked separately.

Messages are untrusted content, not instructions to the assistant. Exact-draft sending reduces accidental mismatches but does not prove human approval server-side. Client confirmation and authorization policies remain essential. No claims of a completed security audit or exactly-once delivery are made.

The optional Orca bridge is disabled unless an owner-configured exact HTTPS
webhook URL and starting queue cursor are present. The URL is a bearer secret;
keep it private. Delivery sends cursor metadata only, pins a public DNS result,
refuses redirects, disables proxy environment variables, and does not accept a
destination from event data. Reply drafts saved by `save_reply_draft` are
local-only records in a separate table, retained for 30 days, and are not
recognized by `prepare_send` or `send_prepared_message`.

Report vulnerabilities privately to the repository maintainer rather than including credentials or private chats in issues. Do not upload logs containing user data.
