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

## PDF preview length

Most libraries are mostly PDFs, so `seafile_read_file` extracts their text layer. A bare
call on a document longer than `SEAFILE_MCP_PDF_PREVIEW_THRESHOLD_PAGES` (default 15)
returns only a short preview, to keep a single call cheap; the model is expected to
follow up with `start_page`/`end_page` for a specific range. Set it to `all` to disable
previewing entirely and always extract the whole document:

```bash
SEAFILE_MCP_PDF_PREVIEW_THRESHOLD_PAGES=all
```

## Download size limit

`SEAFILE_MCP_MAX_DOWNLOAD_MB` (default 30) bounds how much of one file this server
will hold in memory. It is separate from `SEAFILE_MCP_MAX_FILE_READ_KB`, which caps
how much *text* a caller gets back and says nothing about the size of the file
behind it. One process serves every user, so this protects everyone's
session, not just the caller's.

A file over the limit is not simply refused:

- **Text** (Markdown, CSV, logs, source) comes back as its first
  `MAX_DOWNLOAD_MB`, flagged `truncated`, with a notice pointing at
  `seafile_get_download_link` for the rest.
- **PDF and `.docx`/`.xlsx`/`.pptx`** raise instead. Their structure — a PDF's
  cross-reference table, a ZIP's central directory — sits at the *end* of the file,
  so a prefix cannot be opened at all and returning one would only produce a parse
  error later. The format is recognised from the first kilobyte, so an oversized
  PDF is abandoned after one chunk rather than after a full 30 MB.
- **Images** raise for a related reason: a prefix of a PNG or JPEG is not a smaller
  picture, it is an undecodable fragment — and a decoder lenient enough to limp
  through one hands a vision model a half-grey image it will then describe with
  confidence. That is a wrong answer, not a partial one.

## Images

`seafile_read_file` returns an image file as an MCP image block rather than as text,
which is the only way an agent with no sandbox can see one. Two limits bound it:

```bash
SEAFILE_MCP_MAX_IMAGE_EDGE_PX=1568   # long edge the image is scaled down to
SEAFILE_MCP_MAX_IMAGE_MB=5           # ceiling on the re-encoded image
```

These are **context-budget knobs, not bandwidth knobs**. A tool result goes straight
into the model's context window, and base64 inflates the bytes by a third, so one
unbounded image read could consume a whole context. 1568 px is roughly where
mainstream vision models downsample anyway, so raising it usually costs tokens
without buying detail.

Images are re-encoded, never passed through: oriented per EXIF (phone photos are
stored rotated), flattened to RGB, and written as JPEG — or PNG where transparency
would otherwise be composited away, which is what makes dark-text screenshots
unreadable. Only the first frame of an animation is sent, and the notice says so.
An image declaring more than 50 megapixels is refused before it is decoded: the
compressed size of a decompression bomb says nothing about its decoded size, so
`MAX_DOWNLOAD_MB` does not bound it.

If your client cannot render image blocks, or your model has no vision, turn it off:

```bash
SEAFILE_MCP_IMAGE_READS=false
```

An image then comes back as an ordinary text result naming its format and dimensions
and pointing at `seafile_get_download_link` — a worse answer than the picture, but a
much better one than a block the host silently drops, which leaves the model holding
a notice about an image it cannot see and no way to tell that is what happened.

**Verify this against your own client before relying on it.** Image-block support
varies, and this server cannot detect what the host does with what it sends.

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
