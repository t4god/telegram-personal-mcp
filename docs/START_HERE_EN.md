# Telegram MCP: start from scratch

[English](START_HERE_EN.md) | [Русский](START_HERE_RU.md) · [Ready-to-copy agent prompt](AGENT_SETUP_PROMPT_EN.md) · [README](../README.md)

## What you get

Your AI assistant can read cloud chats accessible to your Telegram account, search messages, prepare and send messages and replies. This is **not a Telegram bot**: actions use your account. A BotFather token will not work.

This is an early beta. Installation and automated tests without a real account have been checked. End-to-end login and operation in every AI client are not guaranteed. Secret Chats, reactions, message editing/deletion, file sending and automatic voice transcription are unsupported. Attachments can be downloaded within the documented limits.

**Easiest approach:** copy the linked prompt into an AI agent with terminal access and answer its questions. A chat without tools can only explain the steps. You still enter secrets and complete sign-in yourself.

## Before starting: five decisions

1. Where will the program run: your Mac, Windows/Linux computer, or a server? The machine must stay on for continuous availability.
2. Which AI application and version will you use? It must support **remote MCP over HTTP with OAuth**. This project has no local `stdio` mode. Adding a startup command to an arbitrary client will not necessarily work.
3. Which Telegram account? A separate test account is a good starting point. Each deployment supports one owner.
4. Do you have a server, domain and HTTPS? A cloud AI needs an internet-reachable endpoint; it cannot reach your laptop's `localhost`. The current implementation also requires HTTPS for local clients.
5. Do you want sending and background capture? Sending is configurable. Background capture stores events and message contents locally for up to 30 days / 100,000 events. Access is not restricted to one selected chat.

The MIT project requires no license purchase. Hosting, a domain, network traffic and your chosen AI may cost money separately. A free tier is not a guarantee of no bill. Before creating a paid resource, the agent should show the provider, current terms, currency, billing frequency and limit, then obtain approval. Start with existing infrastructure if suitable.

## Never send these to an agent or publish them

Do not paste API hash, Telegram login codes, two-step verification passwords, pairing passwords, OAuth tokens or `.session` contents into chat. Never upload `runtime` to GitHub: it contains account access and private messages.

An agent can prepare installation and commands. Complete login **yourself in a private terminal** that the agent cannot observe. Setup asks for your phone/code and, if enabled, Telegram two-step verification password. Enter the pairing password in the browser only on your verified HTTPS domain. It is a separate password you create, not your Telegram password. Pause screen access during secret entry. Telling the agent “sign-in completed” is sufficient.

## Step 1. Download and install

You need Git and Python 3.11 or newer. An agent can check availability. Do not install random builds from unknown websites.

**macOS / Linux**, an ordinary terminal, without `sudo`:

```sh
git clone https://github.com/t4god/telegram-personal-mcp.git
cd telegram-personal-mcp
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
export DOT_ROOT="$PWD/runtime"
```

**Windows PowerShell**: activation is unnecessary, so there is no need to change Execution Policy.

```powershell
git clone https://github.com/t4god/telegram-personal-mcp.git
cd telegram-personal-mcp
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
$env:DOT_ROOT = Join-Path (Get-Location) 'runtime'
```

Expected: installation completes without errors. Do not continue automatically after `ERROR`. If the directory already exists, inspect its purpose first; never delete it or clone over an existing deployment.

## Step 2. Create your Telegram connection

Personally visit https://my.telegram.org, sign into your account and create an application in API development tools. Obtain **API ID** and **API hash**. This is not a paid AI API subscription or bot token. Registration availability depends on Telegram; the agent must not bypass service checks.

Run personally:

```sh
telegram-personal-mcp-setup
```

On Windows:

```powershell
.\.venv\Scripts\telegram-personal-mcp-setup.exe
```

The program requests API ID, hidden API hash and a separate pairing password of at least 20 characters, then performs Telegram login. At the end it prints `Set DOT_OWNER_ID=...`. Keep that numeric value for the next step. Remember the pairing password or save it in your password manager.

Expected: `Setup complete`. Do not forward the complete login output. Setup does not overwrite an existing session. If interrupted login leaves `Session already exists`, do not blindly delete the file: use a new empty directory with `--root`, and set **that same path** as `DOT_ROOT` when starting the service. You can terminate an unused session in Telegram's device settings.

## Step 3. Prepare HTTPS

You need your own address, such as `https://telegram.example.com`. **This is an example, not a live project endpoint.** Replace it with your domain. A reverse proxy must receive HTTPS and forward requests to the service on port 8341.

For your agent or administrator:
- Configure DNS and a valid certificate for your domain.
- Proxy the whole site, not only `/mcp`: OAuth discovery, authorization and `/owner-login` also need to work.
- Preserve the external Host. `DOT_BASE_URL` must match the scheme and domain exactly, without `/mcp` at the end.
- Do not expose port 8341 directly to the internet. The application listens on `0.0.0.0`; network/container/firewall controls provide this restriction. Agree on access-rule changes first.
- Never serve `runtime` as website files.

If the server already uses Caddy, the following block may fit after checking conflicts:

```caddyfile
telegram.example.com {
    reverse_proxy 127.0.0.1:8341
}
```

This does not replace DNS, certificate, port and service configuration. Do not overwrite an existing proxy configuration. If you do not yet have HTTPS/domain access, stop here and choose an approach with your agent. Never disable HTTPS validation for a quick setup.

## Step 4. Start the service

In the same macOS/Linux terminal with `.venv` activated, replace the domain and numeric ID with **your own** values:

```sh
export DOT_BASE_URL='https://telegram.example.com'
export DOT_OWNER_ID='123456789'
export DOT_SEND_ENABLED=true
export DOT_BACKGROUND_MONITORING_ENABLED=true
telegram-personal-mcp
```

Windows PowerShell:

```powershell
$env:DOT_BASE_URL = 'https://telegram.example.com'
$env:DOT_OWNER_ID = '123456789'
$env:DOT_SEND_ENABLED = 'true'
$env:DOT_BACKGROUND_MONITORING_ENABLED = 'true'
.\.venv\Scripts\telegram-personal-mcp.exe
```

Set `DOT_SEND_ENABLED=false` to disable sending, or `DOT_BACKGROUND_MONITORING_ENABLED=false` to disable background capture. `.env.example` is a template: the program **does not load `.env` automatically**. Exported variables apply only to the current terminal; automatic startup requires a separately configured service. Closing the terminal may stop the process.

Logging is deliberately minimal: silence does not imply a hang. Check `https://YOUR-DOMAIN/health`; expect `{"ok":true}`. This checks the connection but does not establish OAuth or sending. If you see `Unexpected or unauthorized Telegram account`, verify that the account and ID match; do not remove the check.

## Step 5. Connect your AI client

Look up remote MCP/OAuth configuration in **your application's** documentation. Menu names change and availability depends on the client and plan. The connection URL is `https://YOUR-DOMAIN/mcp`.

The client should open authorization on your domain. Personally enter the pairing password from step 2. Stop if you see an unrelated domain or are asked to upload a session file to chat.

Built-in callback restrictions cover ChatGPT and specific local callback addresses. Another client may need `DOT_OAUTH_REDIRECT_URIS`: comma-separated **exact HTTPS callback URLs** from its documentation. Do not add arbitrary domains or wildcards. This setting does not extend push support: webhook push currently accepts OpenAI/ChatGPT callback hosts only; other compatible clients can use pull tools. Even a specific ChatGPT connection mode must be verified, not promised in advance.

First ask the AI to call `connection_status` and `get_capabilities`. Then choose one authorized test chat and read only an agreed message. Do not start with a mass scan of all conversations.

## Step 6. Test a message without surprises

1. Choose a safe recipient, such as your Saved Messages. The agent must resolve the actual chat ID, not guess or supply unsupported `me`.
2. Agree on “Telegram MCP test” and the recipient.
3. `prepare_send` creates a draft but **does not send it**. Reuse the same `request_key` when retrying that request.
4. After your confirmation, call `send_prepared_message` with the exact recipient and text. Set `reply_to_message_id` during draft preparation for a reply.
5. Success means `status=sent`, `readback_verified=true`, and the message is visible in Telegram. `uncertain` means delivery is unresolved: **never retry sending automatically**; inspect the chat first.

The server checks draft matching and limits repeated sends, but cannot independently prove human approval. Your client's confirmation settings matter. Do not connect untrusted applications.

## Troubleshooting

| Symptom | Check |
|---|---|
| `command not found` | Is `.venv` activated? On Windows use the full `.exe` path. |
| `KeyError: DOT_BASE_URL` / `DOT_OWNER_ID` | Are variables set for the same process/terminal? |
| `HTTPS is required` | Use a real `https://` address, not HTTP. |
| Missing pairing key / session | Does `DOT_ROOT` match between setup and startup? |
| `Origin rejected` / callback rejected | External domain, proxy Host and the client's exact callback. |
| Sending is disabled | `DOT_SEND_ENABLED=true` in the running service's environment. |
| Session busy / SQLite locked | Is another instance using the same runtime? |
| Health works, AI does not connect | Check OAuth/HTTP compatibility separately from Telegram connectivity. |

For help, provide OS/Python versions, the step, a command **without secrets**, and a redacted error. Do not publish databases, sessions or complete logs.

## When setup is complete

Installation succeeded, the owner's login finished, HTTPS works, the client connected through OAuth, an authorized read was verified, and an agreed test send was confirmed. If only some steps passed, the agent must list what is done and what remains instead of saying “everything works.”
