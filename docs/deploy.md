# Deploying

One container behind Caddy. Users authenticate with their own Seafile tokens, so there is no
user database to provision and no credential store to protect.

## Quick start

```bash
cp .env.example .env
# edit SEAFILE_SERVER_URL and MCP_DOMAIN
docker compose up --build -d
```

Point `MCP_DOMAIN`'s DNS record at the host first — Caddy obtains a certificate on startup
and needs the name to resolve. Users then connect to `https://$MCP_DOMAIN/mcp`; see
[clients.md](clients.md).

## What the deployment guarantees

- The MCP container publishes **no port**. The only route in is through Caddy over the
  internal compose network.
- Caddy terminates TLS. This matters more than usual here: every request carries a live
  Seafile token in a header.
- Access logs omit headers, and the application redacts token-shaped strings from all log
  output including tracebacks.
- Nothing is persisted. Restarting the container discards all in-memory state; there is no
  volume holding user data or credentials.

## Choosing a mode

`SEAFILE_MCP_MODE` decides which tools exist. Tools outside the tier are never registered,
so a model cannot call what it cannot see.

| Mode | Tools | When |
|---|---|---|
| `read_only` | browsing and reading only | First rollout, or read-only assistants |
| `safe_write` | **default** — write, upload, mkdir, rename, move, copy | Recommended for shared deployments |
| `full` | adds `seafile_delete` | Only when you need it |

The default is `safe_write` for a specific reason: most chat clients (LibreChat included)
cannot show an interactive confirmation prompt, so there is no way for a human to approve a
deletion at the moment it happens. A `confirm` argument supplied by the model is not a
substitute — anything a model can set, text injected into a document can also set. Deleting
from the Seafile web interface keeps a real person in the loop.

If your users connect only with clients that support MCP elicitation (Claude Desktop, for
example), `full` is more defensible, because the server will ask the user directly.

## Restricting who may use the server

Any valid token for your Seafile instance works by default, which mirrors the fact that
those users could already reach Seafile directly. To narrow it:

```bash
SEAFILE_MCP_ALLOWED_EMAIL_DOMAINS=uni-osnabrueck.de,example.org
```

This applies to account tokens, where the server can read the account's email. Library
tokens are anonymous by design and are unaffected.

## Search

Seafile's file search is a **Professional-edition** feature. On startup the server probes
`/api2/search/` without credentials — URL routing happens before authentication, so a
missing feature answers `404` while a present one answers `401`. If it is absent,
`seafile_search` is not registered and the boot log says so. Override with
`SEAFILE_MCP_ENABLE_SEARCH=true|false`.

## Operating notes

- **Logs**: `docker compose logs -f seafile-mcp`. Mutating calls are audit-logged with the
  tool, a token fingerprint, the library, the path, and the outcome — never the token.
- **Rate limiting**: writes are capped per token per minute (`mutation_rate_limit`), which
  bounds a runaway or injected loop.
- **Upgrades**: `docker compose up --build -d`. Certificates persist in the `caddy_data`
  volume; do not delete it casually or you will re-request certificates and may hit issuance
  rate limits.

## LibreChat administrator fallback

Prefer having users add the server themselves ([clients.md](clients.md)). If your LibreChat
version does not expose an API-key field when a user adds a server, declare it once in
`librechat.yaml`. The file holds only a placeholder — each user still enters their own token
through the LibreChat UI, and LibreChat stores it encrypted per user.

```yaml
mcpServers:
  seafile:
    type: streamable-http
    url: 'https://seafile-mcp.example.org/mcp'
    headers:
      Authorization: 'Token {{SEAFILE_TOKEN}}'
    customUserVars:
      SEAFILE_TOKEN:
        title: 'Seafile API Token'
        description: 'Library API Token (library -> Advanced -> API Token), or an account token.'
```

Restart LibreChat afterwards. Users will find the token field via the gear icon next to the
server in the tool picker, or in the MCP Settings panel.

## Verifying a deployment

```bash
# 1. Unauthenticated requests must be refused.
curl -i https://$MCP_DOMAIN/mcp            # expect 401 + WWW-Authenticate

# 2. A real token must be accepted.
curl -i -H "Authorization: Token $YOUR_TOKEN" https://$MCP_DOMAIN/mcp

# 3. Confirm the advertised tool list matches the mode you configured.
docker compose logs seafile-mcp | grep starting
```

Then connect from two different accounts and confirm each sees only its own libraries.
