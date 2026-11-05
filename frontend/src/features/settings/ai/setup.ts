// SPDX-License-Identifier: AGPL-3.0-or-later
/** The setup snippet shown with a new client's token (docs/MCP_CLIENTS.md). Never contains the token. */
import type { AiTransport } from "./api";

export const TOKEN_FILE = "$HOME/.config/perfin-mcp-token";

/** Shell command that stores the token without putting it in the shell history. */
export const SAVE_TOKEN_COMMAND = `umask 077 && cat > ${TOKEN_FILE}   # paste the token, then press Ctrl-D`;

export function setupSnippet(transport: AiTransport, apiUrl: string): string {
  if (transport === "http") {
    return [
      "# deploy/.env",
      "PERFIN_MCP_ENABLED=true",
      "PERFIN_MCP_TRANSPORT=http",
      "PERFIN_MCP_HTTP_ALLOWED_ORIGINS=https://your-assistant-host.example",
      "MCP_API_TOKEN=<the token shown above>",
      "MCP_HTTP_TOKEN=<openssl rand -hex 32>",
      "",
      "# then: docker compose --profile mcp up -d",
    ].join("\n");
  }
  return JSON.stringify(
    {
      mcpServers: {
        perfin: {
          command: "uv",
          args: ["run", "--project", "/path/to/PerFin/backend", "perfin", "mcp"],
          env: {
            PERFIN_MCP_ENABLED: "true",
            PERFIN_MCP_API_URL: apiUrl,
            PERFIN_MCP_TOKEN_FILE: "/home/you/.config/perfin-mcp-token",
            PERFIN_MCP_TRANSPORT: "stdio",
          },
        },
      },
    },
    null,
    2,
  );
}
