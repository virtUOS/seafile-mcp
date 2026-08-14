## Handling large payloads and tool argument size limits

Many tools in this MCP server accept parameters that go through a JSON
serialization step before being sent over the wire. That JSON step has a
**per-parameter character limit** — usually somewhere between 30.000 and
40.000 characters, with this deployment's hard cap sitting at roughly 38.000.
This limit is independent of any "decoded byte count" or "file size" cap the
server might also enforce; a 28 KB binary is perfectly fine on the server side,
but its base64 encoding (roughly 37.300 characters) may still blow the JSON
parameter limit and fail with a "file not in tool arg scope" error.

**The general workaround pattern is always the same:**

1. **Generate the large value in bash**, not as a literal string inside a tool
   call. Use a Python script, `base64`, `jq`, or whatever fits — but produce
   the result by running code, not by typing it out.

2. **Print the value to stdout** from bash. Do not save it to a file first
   and try to read it back in a later step — that intermediate file is the
   part that fails. The bash tool is stateless: between calls, `/mnt/data`
   files are unreliable scratch, and `read_file` cannot access them.

3. **Take the bash stdout directly** and paste it as the parameter of the next
   tool call. The tool result from step 1 is visible in the conversation —
   you can reference it by position and drop it straight into the parameter
   of step 2's tool invocation.

In pseudo-form:

```
bash: python3 generate_and_print_base64.py <args>
→ stdout: "SGVsbG8gV29ybGQ="

→ Use that stdout string as the `content_base64` parameter:
  seafile_upload_file(parent_dir="/...", filename="file.b64", content_base64="SGVsbG8gV29ybGQ=")
```

This works for **any** tool argument that exceeds the per-parameter limit — not
just base64 uploads, but also large JSON payloads, long text bodies, or
pre-built CSVs and reports. The trick is the same every time: the tool that
*produces* the value and the tool that *consumes* it are two separate calls,
connected only by the stdout of the producer being pasted into the consumer's
argument. No intermediate file, no `read_file`, no detour.

Concrete examples of when to use this pattern:

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| "file not in tool arg scope" | `content_base64` too large for the JSON parameter | Generate base64 in bash, pipe stdout directly into the upload call |
| Tool call refused with no other error on a large argument | Parameter exceeds char limit regardless of content type | Generate value in bash, pipe stdout into next tool |
| `read_file` on a file you just `cat`-ed to `/mnt/data` returns nothing | `/mnt/data` is scratch-only between tool calls | Keep everything in one bash call, or use stdout piping |
| Tool call silently dropped the value for a very long string | Same char limit, different tool | Same pattern: generate in bash, pipe through |

**Why `read_file` does not work as a bridge here:** The bash tool runs in a
stateless environment. Any file you write to `/tmp` or `/mnt/data` may not
survive to the next call. Even when it does, `read_file` operates on the
agent's filesystem view, not on the bash tool's runtime — and the skill's own
`scripts/` bundle is the exception, not the rule. If you need a value
computed by code to feed into another tool, the stdout-piping pattern is the
reliable path.