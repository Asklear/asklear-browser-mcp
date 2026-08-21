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
uv tool install asklear-browser-mcp
```

Or, if you prefer pip:

```bash
pip install asklear-browser-mcp
```

## Configure

Add this to your MCP client configuration:

```json
{
  "mcpServers": {
    "asklear-local": {
      "command": "asklear-browser-mcp",
      "args": []
    }
  }
}
```

## Prerequisites

- Google Chrome installed
- [Asklear Chrome extension](https://asklear.cn) installed and bound to your account
- Verify: `curl http://127.0.0.1:8765/health` → `{"extension_connected": true}`

## How it works

```
Agent (Claude Code / Codex / …)
  │ stdio JSON-RPC
  ▼
asklear-browser-mcp (this package)
  │ loopback HTTP (127.0.0.1:8765)
  ▼
asklear-browser-connector (auto-started)
  │ WebSocket
  ▼
Asklear Chrome extension → your Chrome tab
```

The local Connector is auto-started on first use and shuts down when the gateway exits. No manual process management needed.

## Two connections, one Asklear

This package provides **browser collection** only. For data queries (JD/Tmall/PDD/Douyin market data), use the hosted Asklear MCP:

| Connection | Purpose | Transport |
| --- | --- | --- |
| `asklear` | Data queries, market research | Remote HTTP (OAuth) |
| `asklear-local` (this package) | Browser collection, page extraction | Local stdio |

Both can coexist. Your agent picks the right tool automatically.

## License

Apache-2.0. See [LICENSE](LICENSE).
