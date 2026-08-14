# Seafile-mcp

An MCP server for [Seafile](https://www.seafile.com/), designed for **multi-user
deployments**: one server instance serves many people, and each user supplies their own
Seafile API token as the MCP API key.

## How authentication works

The Seafile token *is* the API key. There is no separate account, no registration, and no
credential storage — "authenticated" simply means "Seafile accepted this token."

Two kinds of Seafile token work, and the server auto-detects which one you gave it:

| Token | How to get it | Scope |
|---|---|---|
| **Library API token** (recommended) | In Seafile: library → Advanced → API Token, choose read-only or read-write | One library. Cannot delete, move, or copy — Seafile itself offers no such endpoints for these tokens. |
| **Account token** | `POST /api2/auth-token/` with your username and password | Your whole account, all libraries. Needed for search, move, copy, and delete. |

Prefer a **read-only library token** unless you specifically need more. It is the single
most effective way to limit what an assistant can do on your behalf.

The server is a stateless pass-through: it stores no credentials, keeps no user database,
and retains nothing between requests. Isolation between users is structural — each request
carries its own token and no per-user state exists to leak.

## Safety

Deployments choose a mode via `SEAFILE_MCP_MODE`; tools outside the active tier are never
registered, so a model cannot call what it cannot see.

- `read_only` — no mutating tools at all.
- `safe_write` — **default.** Write, upload, create directory, rename, move, copy. Every
  one is reversible via Seafile's file history or an inverse operation.
- `full` — additionally exposes delete.

Nothing this server exposes can irreversibly destroy data: deletes go to the library trash,
and overwrites create a new version in file history. The trash purge endpoint is
deliberately not wrapped as a tool.

## Reading PDFs
(This feature is still under development, parsing functionality will be extended)

Most files in a typical Seafile library are PDFs, so `seafile_read_file` extracts their
text layer instead of returning raw bytes as mojibake. There is no OCR — a scanned or
image-only PDF comes back with an explicit notice instead of an error or garbage — and
layout, tables, and images are not preserved.

A bare call returns the whole document, except that a PDF longer than a configurable
page threshold (15 pages by default) is previewed (first 2 pages only) instead, to
keep a single call cheap; pass `start_page`/`end_page` (1-indexed, inclusive) to read
a specific range instead. The response's `notice` field always states the document's
true page count and whether what you got was a preview or the full text, so the two
are never ambiguous.

System admins can tune or disable the threshold with
`SEAFILE_MCP_PDF_PREVIEW_THRESHOLD_PAGES`:

```bash
SEAFILE_MCP_PDF_PREVIEW_THRESHOLD_PAGES=30    # preview only past 30 pages
SEAFILE_MCP_PDF_PREVIEW_THRESHOLD_PAGES=all   # never preview; always extract the whole document
```

## Agent skill

[`skills/seafile-mcp-tools`](skills/seafile-mcp-tools) is a
skill documenting the tool-selection pitfalls above plus one this README doesn't
cover: saving PDF/Word/Excel/PowerPoint files, which `seafile_write_file` will
silently corrupt since it only ever sends UTF-8 text. It bundles a tested
generator script per format so an agent doesn't have to build these binary
formats by hand. The skill assumes the agent has access to a sandbox or other
environment where it can execute code (a shell and Python) — without one, those
document formats can't be produced through this server at all.

## Running it

See [docs/clients.md](docs/clients.md) for client configuration (LibreChat, Claude Desktop,
Claude Code, generic HTTP, and local stdio) and [docs/deploy.md](docs/deploy.md) for the
Docker + Caddy deployment.

```bash
# local, single user
export SEAFILE_SERVER_URL=https://seafile.example.org
export SEAFILE_API_TOKEN=your_token
uvx seafile-mcp --transport stdio
```

## License

MIT
