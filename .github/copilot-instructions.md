# GitHub Copilot instructions

<!-- github-copilot-toolbox:mcp-skills-awareness-begin -->

### MCP & Skills awareness (GitHub Copilot Toolbox)

_Last synced: 2026-11-19T20:27:36.131Z._

- **Full report:** `.github/copilot-toolbox-mcp-skills-awareness.md` in this workspace (auto-overwritten on each scan). Use it as ground truth for configured servers and skill folders.
- **MCP:** For **live tools**, use **Copilot Chat → Agent** and **trust/start** the right servers in the MCP UI.
- **When the user’s task matches a server** (e.g. “open this Confluence page” and a **Confluence** / **Atlassian** MCP is listed), **prefer that server id** and plan on Agent + MCP for actions—not only file search.
- **Skills:** Folders below contain `SKILL.md`; attach or cite paths in chat when relevant.

#### Workspace MCP

- `c:\Users\jenss\OneDrive - Singularyt UG\Code\source\AIServer\GamingAI\GamingAI\.vscode\mcp.json` _(workspace: GamingAI)_ — _file missing_

_No active workspace servers in mcp.json._

#### User MCP

- `C:\Users\jenss\AppData\Roaming\Code\User\mcp.json` — _servers defined_

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

#### Project skills

_None found (or no workspace open)._

#### User skills

_None found._

<!-- github-copilot-toolbox:mcp-skills-awareness-end -->
