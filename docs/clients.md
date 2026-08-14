# Connecting a client

Every user supplies **their own Seafile API token** as this server's API key. Nothing is
stored on the server, and no two users can see each other's files — each request carries its
own credential.

## Step 1: get a Seafile API token

### Library API token (recommended)

Scoped to one library, and you choose its permission when you create it.

1. Open Seafile in your browser.
2. Find the library you want the assistant to work with.
3. Open its dropdown menu → **Advanced** → **API Token**.
4. Add a token. Give it a name (e.g. `mcp`) and pick **read-only** unless you actually need
   the assistant to make changes.
5. Copy the token.

This is the safest option, and not only by convention: Seafile publishes no delete, move,
copy, or search endpoint for library tokens, so those operations are impossible with one
regardless of what any assistant tries to do.

### Account token (broader access)

Needed only for searching, or for moving/copying/deleting across libraries. It grants access
to your **whole account**, so prefer a library token when it will do.

```bash
curl -d "username=you@example.org" -d "password=YOUR_PASSWORD" \
  https://seafile.example.org/api2/auth-token/
```

Run this yourself, on your own machine — never paste your Seafile password into a chat.

> Tokens are revocable. If one is ever exposed (a shared screenshot, a pasted transcript),
> delete it in Seafile and generate a new one.

## Step 2: configure your client

The server auto-detects which kind of token you supplied, so the same field works for both.

### LibreChat

1. Open the **MCP Settings** panel (right sidebar), press **+**.
2. Fill in:
   - **Name**: `seafile`
   - **URL**: `https://seafile-mcp.example.org/mcp`
   - **Transport / type**: `streamable-http`
   - **API key**: your Seafile token
3. Save. The Seafile tools appear in the tool list.

> If your LibreChat version does not offer an API-key or header field when adding a server
> yourself, an administrator can declare it once in `librechat.yaml` instead — see
> [deploy.md](deploy.md#librechat-administrator-fallback). Users still enter their own token
> through the UI; the YAML only holds a placeholder.

### Claude Desktop, Claude Code, and other HTTP clients

```json
{
  "mcpServers": {
    "seafile": {
      "type": "http",
      "url": "https://seafile-mcp.example.org/mcp",
      "headers": { "Authorization": "Token YOUR_SEAFILE_TOKEN" }
    }
  }
}
```

`Bearer YOUR_TOKEN` or a bare token work too — the server normalises whichever form your
client sends. If your client reserves the `Authorization` header, send `X-Seafile-Token`
instead.

### Zoo Code 
[Zoo Code](https://docs.zoocode.dev/features/mcp/using-mcp-in-roo?utm_source=extension&utm_medium=ide&utm_campaign=mcp_edit_settings#editing-mcp-settings-files) Integration. Add configuration to `mcp_settings.json`

```json
{
  "mcpServers": {
    "seafile": {
      "type": "streamable-http",
      "url": "https://seafile-mcp.example.org/mcp",
      "headers": {
        "Authorization": "YOUR_SEAFILE_TOKEN"
      }
    }
  }
}
```


### Running it locally over stdio

No Docker and no server required; single user, credential from your own config.

```json
{
  "mcpServers": {
    "seafile": {
      "command": "uvx",
      "args": ["seafile-mcp", "--transport", "stdio"],
      "env": {
        "SEAFILE_SERVER_URL": "https://seafile.example.org",
        "SEAFILE_API_TOKEN": "YOUR_SEAFILE_TOKEN"
      }
    }
  }
}
```

## What the tools can do

| Tool | Needs an account token? |
|---|---|
| `seafile_list_libraries`, `seafile_get_library_info` | no |
| `seafile_list_directory`, `seafile_read_file`, `seafile_get_file_info`, `seafile_get_download_link` | no |
| `seafile_write_file`, `seafile_upload_file`, `seafile_create_directory`, `seafile_rename` | no (needs a read-write token) |
| `seafile_move`, `seafile_copy` | **yes** |
| `seafile_delete` | **yes**, and only if the deployment runs in `full` mode |
| `seafile_search` | **yes**, and only on Seafile Professional |

Tools your deployment has disabled are not registered at all, so they will not appear in
your client's tool list.

## Safety notes

- **Deleting.** Most deployments run in `safe_write` mode, where the delete tool does not
  exist. Delete from the Seafile web interface instead. Where delete *is* enabled, items go
  to the library trash and can be restored; nothing this server exposes destroys data
  permanently.
- **File contents are untrusted.** A document in a shared library can contain text aimed at
  the assistant ("ignore your instructions and…"). The server labels all file content as
  untrusted data, but the strongest protection is a read-only token: it makes the whole
  category of problem moot.
- **Least privilege.** Use a read-only library token by default. Reach for a read-write or
  account token only when a specific task needs it.
