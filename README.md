# Asklear Browser MCP

Local browser collection gateway for [Asklear](https://asklear.cn) — a stdio MCP server that drives your Chrome browser via the Asklear extension.

**Page content and login state never leave your computer.** All browser operations run locally; the Asklear service never sees your cookies, credentials, or page data.

## What you get

Six browser tools, usable from any MCP-compatible agent (Claude Code, Codex, WorkBuddy, Trae, …):

| Tool | Purpose |
| --- | --- |
| `navigate` | Open a URL in a background tab |
| `observe` | Snapshot the current page's accessibility tree |
| `click` | Click an element |
| `fill` | Type into an input |
| `scroll` | Scroll the page |
| `extract` | Extract structured text/markdown from the page |

## Install

```bash
uv tool install git+https://github.com/Asklear/asklear-browser-mcp.git
```

Or, if you prefer pip:

```bash
pip install git+https://github.com/Asklear/asklear-browser-mcp.git
```

## Configure

Add this to your MCP client configuration:

```json
{
  "mcpServers": {
    "asklear-local": {
      "command": "asklear-browser-agent",
      "args": []
    }
  }
}
```

## Prerequisites

- Google Chrome installed
- macOS, Windows, and Linux are supported
- [Asklear Chrome extension](https://asklear.cn) installed and bound to your account
- Verify: `curl http://127.0.0.1:8765/health` → `{"extension_connected": true}`

## How it works

```
Agent (Claude Code / Codex / …)
  │ stdio JSON-RPC
  ▼
asklear-browser-agent (this package)
  │ probe / call over loopback HTTP (127.0.0.1:8765)
  ▼
asklear-browser-connector (independent background daemon)
  │ WebSocket
  ▼
Asklear Chrome extension → your Chrome tab
```

The first browser tool call starts the Connector in the background when it is
not already running. A second Agent reuses the same loopback daemon; its
session is isolated by `session_id`. When an Agent exits, the Connector stays
running for the next Agent.

The daemon keeps only local runtime state under
`~/.asklear/browser-connector/`:

- `connector.pid` — current process ID
- `connector.log` — daemon output
- `process-token-*` — a private local process token

You normally do not need to manage it. For troubleshooting, the same package
provides:

```bash
asklear-browser-connector start
asklear-browser-connector status
asklear-browser-connector stop
asklear-browser-connector restart
```

`start` and the automatic start path do not need an API key or OAuth. The
token is read from the private local state file and is never put in the
command line.

## Two connections, one Asklear

This package provides **browser collection** only. For data queries (JD/Tmall/PDD/Douyin market data), use the hosted Asklear MCP:

| Connection | Purpose | Transport |
| --- | --- | --- |
| `asklear` | Data queries, market research | Remote HTTP (OAuth) |
| `asklear-local` (this package) | Browser collection, page extraction | Local stdio |

Both can coexist. Your agent picks the right tool automatically.

## License

Apache-2.0. See [LICENSE](LICENSE).
