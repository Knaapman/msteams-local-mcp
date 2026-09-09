# Windows + OpenAI Secure MCP Tunnel

This fork can be exposed to ChatGPT through OpenAI's `tunnel-client` while keeping the Teams cache local on the Windows machine.

## Recommended layout

Use one runtime key for both local MCP servers, but keep separate tunnel IDs and profiles:

```text
Windows PC
├─ Outlook Classic MCP
│  └─ tunnel-client profile: outlook-local
│     └─ tunnel id: <outlook tunnel>
└─ Teams Local MCP
   └─ tunnel-client profile: teams-local
      └─ tunnel id: <teams tunnel>

Shared environment variable:
CONTROL_PLANE_API_KEY=<same runtime key>
```

`CONTROL_PLANE_API_KEY` is the runtime credential used by `tunnel-client`. It may be reused across multiple tunnels if the key's principal has `Tunnels: Read + Use` permission for those tunnels. Do not commit the key.

## 1. Install this fork

From the repository root:

```powershell
py -m pip install -e .
```

Or with pipx:

```powershell
pipx install -e .
```

## 2. Verify the Teams cache locally

Make sure the new Teams desktop client is running and signed in, then run:

```powershell
.\scripts\windows-smoke-test.ps1
```

The test checks the expected Teams MSIX cache path and calls:

```powershell
msteams-local-dump accounts --limit 20
```

If this succeeds, the LevelDB is discoverable and readable before the tunnel is involved.

## 3. Reuse the same runtime key as Outlook

Set the runtime key in the current shell, or preferably in the same secure Windows environment/service configuration already used for Outlook:

```powershell
$env:CONTROL_PLANE_API_KEY = "<existing Outlook runtime key>"
```

Do not create a second key unless you intentionally want separate credentials or permissions.

## 4. Create a separate Teams tunnel

Create or obtain a distinct tunnel ID for Teams. Then initialize the Teams profile:

```powershell
.\scripts\setup-openai-tunnel.ps1 -TunnelId "tunnel_..."
```

The script creates the `teams-local` profile using the official local stdio sample and configures `msteams-local-mcp` as the local MCP command.

Equivalent manual commands:

```powershell
tunnel-client init `
  --sample sample_mcp_stdio_local `
  --profile teams-local `
  --tunnel-id tunnel_... `
  --mcp-command "msteams-local-mcp" `
  --force

tunnel-client doctor --profile teams-local --explain
tunnel-client run --profile teams-local
```

## Why a separate tunnel ID?

The Outlook MCP and Teams MCP are separate local stdio processes. Keeping a separate tunnel/profile for each avoids mixing MCP session state or routing related requests to different processes.

The credential can still be shared:

```text
CONTROL_PLANE_API_KEY
  ├─ outlook-local -> outlook tunnel
  └─ teams-local   -> teams tunnel
```

## Current Teams limitations

- Read-only. This MCP reads the local Teams 2.x cache and does not send Teams messages.
- Cache-only. It sees recent/synchronized content present on the machine, not guaranteed full server history.
- General unread state is not available in the local cache. Unread `@mentions` are available.
- Teams may change its internal IndexedDB schema, so cache parsing can require updates after major client changes.
