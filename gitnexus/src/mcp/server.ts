/**
 * MCP Server (Multi-Repo)
 *
 * Model Context Protocol server that runs on stdio.
 * External AI tools (Cursor, Claude) spawn this process and
 * communicate via stdin/stdout using the MCP protocol.
 *
 * Supports multiple indexed repositories via the global registry.
 *
 * Tools: list_repos, query, cypher, context, impact, detect_changes, rename
 * Resources: repos, repo/{name}/context, repo/{name}/clusters, ...
 */

import { randomUUID } from 'node:crypto';
import * as fs from 'node:fs/promises';
import { createRequire } from 'module';
import * as path from 'node:path';
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { CompatibleStdioServerTransport } from './compatible-stdio-transport.js';
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  ListResourcesRequestSchema,
  ReadResourceRequestSchema,
  ListResourceTemplatesRequestSchema,
  ListPromptsRequestSchema,
  GetPromptRequestSchema,
} from '@modelcontextprotocol/sdk/types.js';
import { GITNEXUS_TOOLS } from './tools.js';
import { realStdoutWrite } from './core/lbug-adapter.js';
import type { LocalBackend } from './local/local-backend.js';
import { getResourceDefinitions, getResourceTemplates, readResource } from './resources.js';
import { getGlobalDir } from '../storage/repo-manager.js';

/**
 * Bound what a tool reply puts into the agent's context.
 *
 * A reply over MAX_RESPONSE_BYTES is cut at a line boundary (a newline, or the
 * JSON-escaped newline inside cypher's markdown string) — or mid-line when that
 * would show less than half the budget, as in a one-line error or one huge row — and ends
 * with a footer naming how much is shown and the file holding the complete text.
 * Every reply gets its own file under ~/.gitnexus/results, written once and never
 * renamed, so a prune in any process sharing the directory can only remove entries
 * older than the reply's; the newest RETAINED_RESULTS entries are kept (ties with
 * the last kept entry included) and the call verifies its file survived before
 * answering.
 */
const MAX_RESPONSE_BYTES = 16384;
const RETAINED_RESULTS = 50;

async function boundedResponse(toolName: string, text: string): Promise<string> {
  const bytes = Buffer.from(text);
  if (bytes.length <= MAX_RESPONSE_BYTES) return text;

  const dir = path.join(getGlobalDir(), 'results');
  const file = path.join(
    dir,
    `${toolName}-${Date.now().toString(36)}-${randomUUID().slice(0, 8)}.json`,
  );
  const lines = text.split(/\n|\\n/).length;
  const footer = (shown: number) =>
    `\n[gitnexus] bounded: ${shown} of ${lines} lines (${bytes.length} bytes). Complete result: ${file}`;
  const budget = MAX_RESPONSE_BYTES - Buffer.byteLength(footer(lines));
  const head = bytes.subarray(0, budget);
  const lineEnd = Math.max(head.lastIndexOf('\n'), head.lastIndexOf('\\n'));
  let end = lineEnd > budget / 2 ? lineEnd : budget; // a line longer than half the budget is cut mid-line
  while ((bytes[end] & 0xc0) === 0x80) end--; // never split a multi-byte character
  const visible = bytes.subarray(0, end).toString();

  await fs.mkdir(dir, { recursive: true, mode: 0o700 });
  await fs.chmod(dir, 0o700);
  await fs.writeFile(file, text, { mode: 0o600 });
  const entries = await Promise.all(
    (await fs.readdir(dir)).map(async (name) => ({
      name,
      mtime: await fs.stat(path.join(dir, name)).then(
        (s) => s.mtimeMs,
        () => 0,
      ),
    })),
  );
  entries.sort((a, b) => b.mtime - a.mtime);
  const oldest = entries[RETAINED_RESULTS - 1]?.mtime ?? 0;
  await Promise.all(
    entries
      .filter((e) => e.mtime < oldest)
      .map((e) => fs.rm(path.join(dir, e.name), { force: true })),
  );
  await fs.access(file);
  return visible + footer(visible.split(/\n|\\n/).length);
}

/**
 * Create a configured MCP Server with all handlers registered.
 * Transport-agnostic — caller connects the desired transport.
 */
export function createMCPServer(backend: LocalBackend): Server {
  const require = createRequire(import.meta.url);
  const pkgVersion: string = require('../../package.json').version;
  const server = new Server(
    {
      name: 'gitnexus',
      version: pkgVersion,
    },
    {
      capabilities: {
        tools: {},
        resources: {},
        prompts: {},
      },
    },
  );

  // Handle list resources request
  server.setRequestHandler(ListResourcesRequestSchema, async () => {
    const resources = getResourceDefinitions();
    return {
      resources: resources.map((r) => ({
        uri: r.uri,
        name: r.name,
        description: r.description,
        mimeType: r.mimeType,
      })),
    };
  });

  // Handle list resource templates request (for dynamic resources)
  server.setRequestHandler(ListResourceTemplatesRequestSchema, async () => {
    const templates = getResourceTemplates();
    return {
      resourceTemplates: templates.map((t) => ({
        uriTemplate: t.uriTemplate,
        name: t.name,
        description: t.description,
        mimeType: t.mimeType,
      })),
    };
  });

  // Handle read resource request
  server.setRequestHandler(ReadResourceRequestSchema, async (request) => {
    const { uri } = request.params;

    try {
      const content = await readResource(uri, backend);
      return {
        contents: [
          {
            uri,
            mimeType: 'text/yaml',
            text: content,
          },
        ],
      };
    } catch (err: any) {
      return {
        contents: [
          {
            uri,
            mimeType: 'text/plain',
            text: `Error: ${err.message}`,
          },
        ],
      };
    }
  });

  // Handle list tools request
  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: GITNEXUS_TOOLS.map((tool) => ({
      name: tool.name,
      description: tool.description,
      inputSchema: tool.inputSchema,
    })),
  }));

  // Handle tool calls — bound what reaches the agent, retain the complete result
  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const { name, arguments: args } = request.params;
    let resultText: string | undefined;

    try {
      const result = await backend.callTool(name, args);
      resultText = typeof result === 'string' ? result : JSON.stringify(result, null, 2);

      return {
        content: [
          {
            type: 'text',
            text: await boundedResponse(name, resultText),
          },
        ],
      };
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unknown error';
      const text =
        resultText === undefined
          ? `Error: ${message}`
          : `Error: ${name} produced ${Buffer.byteLength(resultText)} bytes, over the ${MAX_RESPONSE_BYTES}-byte reply bound, and the complete result could not be retained: ${message}`;
      return {
        // A thrown message can echo caller input; MAX_RESPONSE_BYTES/4 characters never exceed the byte bound.
        content: [{ type: 'text', text: text.slice(0, MAX_RESPONSE_BYTES / 4) }],
        isError: true,
      };
    }
  });

  // Handle list prompts request
  server.setRequestHandler(ListPromptsRequestSchema, async () => ({
    prompts: [
      {
        name: 'detect_impact',
        description:
          'Analyze the impact of your current changes before committing. Guides through scope selection, change detection, process analysis, and risk assessment.',
        arguments: [
          {
            name: 'scope',
            description: 'What to analyze: unstaged, staged, all, or compare',
            required: false,
          },
          { name: 'base_ref', description: 'Branch/commit for compare scope', required: false },
        ],
      },
      {
        name: 'generate_map',
        description:
          'Generate architecture documentation from the knowledge graph. Creates a codebase overview with execution flows and mermaid diagrams.',
        arguments: [
          {
            name: 'repo',
            description: 'Repository name (omit if only one indexed)',
            required: false,
          },
        ],
      },
    ],
  }));

  // Handle get prompt request
  server.setRequestHandler(GetPromptRequestSchema, async (request) => {
    const { name, arguments: args } = request.params;

    if (name === 'detect_impact') {
      const scope = args?.scope || 'all';
      const baseRef = args?.base_ref || '';
      return {
        messages: [
          {
            role: 'user' as const,
            content: {
              type: 'text' as const,
              text: `Analyze the impact of my current code changes before committing.

Follow these steps:
1. Run \`detect_changes(${JSON.stringify({ scope, ...(baseRef ? { base_ref: baseRef } : {}) })})\` to find what changed and affected processes
2. For each changed symbol in critical processes, run \`context({name: "<symbol>"})\` to see its full reference graph
3. For any high-risk items (many callers or cross-process), run \`impact({target: "<symbol>", direction: "upstream"})\` for blast radius
4. Summarize: changes, affected processes, risk level, and recommended actions

Present the analysis as a clear risk report.`,
            },
          },
        ],
      };
    }

    if (name === 'generate_map') {
      const repo = args?.repo || '';
      return {
        messages: [
          {
            role: 'user' as const,
            content: {
              type: 'text' as const,
              text: `Generate architecture documentation for this codebase using the knowledge graph.

Follow these steps:
1. READ \`gitnexus://repo/${repo || '{name}'}/context\` for codebase stats
2. READ \`gitnexus://repo/${repo || '{name}'}/clusters\` to see all functional areas
3. READ \`gitnexus://repo/${repo || '{name}'}/processes\` to see all execution flows
4. For the top 5 most important processes, READ \`gitnexus://repo/${repo || '{name}'}/process/{name}\` for step-by-step traces
5. Generate a mermaid architecture diagram showing the major areas and their connections
6. Write an ARCHITECTURE.md file with: overview, functional areas, key execution flows, and the mermaid diagram`,
            },
          },
        ],
      };
    }

    throw new Error(`Unknown prompt: ${name}`);
  });

  return server;
}

/**
 * Start the MCP server on stdio transport (for CLI use).
 */
export async function startMCPServer(backend: LocalBackend): Promise<void> {
  const server = createMCPServer(backend);

  // Use the shared stdout reference captured at module-load time by the
  // lbug-adapter.  Avoids divergence if anything patches stdout between
  // module load and server start.
  const _safeStdout = new Proxy(process.stdout, {
    get(target, prop, receiver) {
      if (prop === 'write') return realStdoutWrite;
      const val = Reflect.get(target, prop, receiver);
      return typeof val === 'function' ? val.bind(target) : val;
    },
  });
  const transport = new CompatibleStdioServerTransport(process.stdin, _safeStdout);
  await server.connect(transport);

  // Graceful shutdown helper
  let shuttingDown = false;
  const shutdown = async (exitCode = 0) => {
    if (shuttingDown) return;
    shuttingDown = true;
    try {
      await backend.disconnect();
    } catch {}
    try {
      await server.close();
    } catch {}
    process.exit(exitCode);
  };

  // Handle graceful shutdown
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);

  // Log crashes to stderr so they aren't silently lost.
  // uncaughtException is fatal — shut down.
  // unhandledRejection is logged but kept non-fatal (availability-first):
  // killing the server for one missed catch would be worse than logging it.
  process.on('uncaughtException', (err) => {
    process.stderr.write(`GitNexus MCP uncaughtException: ${err?.stack || err}\n`);
    shutdown(1);
  });
  process.on('unhandledRejection', (reason: any) => {
    process.stderr.write(`GitNexus MCP unhandledRejection: ${reason?.stack || reason}\n`);
  });

  // Handle stdio errors — stdin close means the parent process is gone
  process.stdin.on('end', shutdown);
  process.stdin.on('error', () => shutdown());
  process.stdout.on('error', () => shutdown());
}
