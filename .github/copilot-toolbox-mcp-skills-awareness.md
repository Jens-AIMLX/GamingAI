# GitHub Copilot Toolbox — MCP & Skills awareness

_Generated: 2026-11-19T20:27:35.262Z_

## How to use this report

- **Saved copy:** This file is **`.github/copilot-toolbox-mcp-skills-awareness.md`** — refreshed whenever the toolbox runs an MCP & Skills scan (including on workspace open when auto-scan is enabled). It is meant for **Copilot workspace context** together with `.github/copilot-instructions.md` (which gets a shorter replaceable summary when auto-merge is on).
- **MCP:** Lists **configured** servers from `mcp.json`. **Live tool use** still requires **Copilot Chat → Agent** with those servers **trusted/started** in the MCP tools UI.
- **Skills:** **On-disk** folders with `SKILL.md`. Copilot does not auto-load them; attach `SKILL.md` or paths in chat when useful.
- **Task routing:** When the user’s request matches a server’s purpose (e.g. Confluence → Confluence/Atlassian MCP), prefer that **server id** from the tables below.

---

## MCP — workspace

Workspace `mcp.json` _(folder: GamingAI)_

- **c:\Users\jenss\OneDrive - Singularyt UG\Code\source\AIServer\GamingAI\GamingAI\.vscode\mcp.json** — _File missing_

_No active workspace servers in mcp.json._

## MCP — user profile

- **C:\Users\jenss\AppData\Roaming\Code\User\mcp.json** — _File exists — servers defined_

| Server id | Kind | Detail |
|-----------|------|--------|
| cognitiveVisualAnalytics | stdio | node C:/Users/jenss/ONEDRI~2/Code/source/AIServer/UserPerpectiveAI/mcp-server/mcp-server-stdio.js --config C:/Users/jenss/ONEDRI~2/Code/Test/mcpconfig/subconfigs/cognitive-visual-config_cognitive.json |
| playwright | stdio | npx -y @playwright/mcp@latest --config C:/Users/jenss/ONEDRI~2/Code/Test/mcpconfig/subconfigs/plwghtconfig_portal.json |
| magicuidesignmcp | stdio | npx -y @magicuidesign/mcp@latest |
| fetch | stdio | uvx mcp-server-fetch |
| context7 | http | https://mcp.context7.com/mcp |
| browserbase | stdio | npx @browserbasehq/mcp |
| quantumprojectmcp | http | http://192.168.178.97:3100/sse |
| imageExtractor | stdio | npx -y mcp-image-extractor |
| braingrid | http | https://mcp.braingrid.ai/mcp |
| render | http | https://mcp.render.com/mcp |

## Skills (local `SKILL.md` folders)

### Project-scoped

_None found (or no workspace open)._

### User-scoped

_None found._

---

## Suggested next steps

- **MCP:** Command Palette → `MCP: List Servers` (or this extension’s hub **MCP** tab) → start/trust servers in **Copilot Chat → Agent → tools**.
- **Edit config:** `MCP: Open Workspace Folder MCP Configuration` / `MCP: Open User Configuration`.
- **Refresh this report:** run **Intelligence — scan MCP & Skills awareness** again after changing `mcp.json` or adding skills.

_Report from GitHub Copilot Toolbox extension._
