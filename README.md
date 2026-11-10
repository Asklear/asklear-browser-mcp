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

First, follow the [illustrated Chrome extension installation guide](https://docs.asklear.cn/browser)
using the same computer and Chrome profile as the Agent. The paired local connection
requires extension 0.3.3 or newer. Agents helping with installation should proactively
share the guide with the user. Matching packages are available here while the public
download is being updated:

- [China production](downloads/asklear-browser-extraction-production.zip)
- [China test](downloads/asklear-browser-extraction-test.zip)
- [Global production](downloads/asklear-browser-extraction-global-production.zip)
- [Global test](downloads/asklear-browser-extraction-global-test.zip)

Choose the package for your environment, unzip it to a permanent folder, and load
the folder containing `manifest.json` via Chrome's `chrome://extensions` page with
Developer mode enabled.

```bash
uv tool install git+https://github.com/Asklear/asklear-browser-mcp.git
```

Or, if you prefer pip:

```bash
pip install git+https://github.com/Asklear/asklear-browser-mcp.git
```

If the package is already installed with uv, upgrade it and restart the existing
background Connector so it runs the new version:

```bash
uv tool upgrade asklear-browser-mcp
asklear-browser-connector restart
```

For an installation made with pip, use `pip install --upgrade git+https://github.com/Asklear/asklear-browser-mcp.git`
instead of `uv tool upgrade`, then restart the Connector as above.

Pair the extension once on the same computer. The user runs
`asklear-browser-connector pair` in a local terminal, opens the extension popup,
chooses **Connect local service**, and enters the displayed code. Keep the code
private; an Agent must not read, repeat, log, or transmit it.

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
- The Asklear Chrome extension installed and enabled in the Chrome profile used by the Agent
- Open the extension popup to check the browser and local service connection
- Verify: `curl http://127.0.0.1:8765/health` → `{"extension_connected": true}`; then use `navigate` and `extract` to verify actual page extraction

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
- `extension-pairing-key` — the private local pairing key

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
