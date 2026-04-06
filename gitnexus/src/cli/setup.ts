/**
 * Setup Command
 *
 * One-time global MCP configuration writer.
 * Detects installed AI editors and writes the appropriate MCP config
 * so the GitNexus MCP server is available in all projects.
 */

import fs from 'fs/promises';
import path from 'path';
import os from 'os';
import { execFile, execFileSync } from 'child_process';
import { promisify } from 'util';
import { fileURLToPath } from 'url';
import { glob } from 'glob';
import { getGlobalDir } from '../storage/repo-manager.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const execFileAsync = promisify(execFile);

interface SetupResult {
  configured: string[];
  skipped: string[];
  errors: string[];
}

const GITNEXUS_START_MARKER = '<!-- gitnexus:start -->';
const GITNEXUS_END_MARKER = '<!-- gitnexus:end -->';

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Resolve the absolute path to the `gitnexus` binary if it's installed
 * globally (or via npm -g / yarn global). Returns null when not found.
 */
function resolveGitnexusBin(): string | null {
  try {
    const cmd = process.platform === 'win32' ? 'where' : 'which';
    const resolved = execFileSync(cmd, ['gitnexus'], {
      encoding: 'utf-8',
      timeout: 5000,
      stdio: ['ignore', 'pipe', 'ignore'],
    })
      .split('\n')[0]
      .trim();
    return resolved || null;
  } catch {
    return null;
  }
}

/**
 * The MCP server entry for all editors.
 *
 * Prefers the globally-installed `gitnexus` binary (starts in ~1 s) over
 * `npx -y gitnexus@latest` (cold-cache install of native deps can take
 * >60 s, exceeding Claude Code's 30 s MCP connection timeout).
 *
 * Falls back to npx when the binary isn't on PATH — e.g. first-time
 * users who ran `npx gitnexus analyze` but haven't done `npm i -g`.
 */
function getMcpEntry() {
  const bin = resolveGitnexusBin();

  if (bin) {
    return { command: bin, args: ['mcp'] };
  }

  // Fallback: npx (works without a global install, but slow cold-start)
  if (process.platform === 'win32') {
    return {
      command: 'cmd',
      args: ['/c', 'npx', '-y', 'gitnexus@latest', 'mcp'],
    };
  }
  return {
    command: 'npx',
    args: ['-y', 'gitnexus@latest', 'mcp'],
  };
}

/**
 * Merge gitnexus entry into an existing MCP config JSON object.
 * Returns the updated config.
 */
function mergeMcpConfig(existing: any): any {
  if (!existing || typeof existing !== 'object') {
    existing = {};
  }
  if (!existing.mcpServers || typeof existing.mcpServers !== 'object') {
    existing.mcpServers = {};
  }
  existing.mcpServers.gitnexus = getMcpEntry();
  return existing;
}

/**
 * Try to read a JSON file, returning null if it doesn't exist or is invalid.
 */
async function readJsonFile(filePath: string): Promise<any | null> {
  try {
    const raw = await fs.readFile(filePath, 'utf-8');
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

/**
 * Write JSON to a file, creating parent directories if needed.
 */
async function writeJsonFile(filePath: string, data: any): Promise<void> {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  await fs.writeFile(filePath, JSON.stringify(data, null, 2) + '\n', 'utf-8');
}

async function fileExists(filePath: string): Promise<boolean> {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

async function upsertMarkedSection(
  filePath: string,
  content: string,
): Promise<'created' | 'updated' | 'appended'> {
  if (!(await fileExists(filePath))) {
    await fs.mkdir(path.dirname(filePath), { recursive: true });
    await fs.writeFile(filePath, `${content.trimEnd()}\n`, 'utf-8');
    return 'created';
  }

  const existingContent = await fs.readFile(filePath, 'utf-8');
  const startIdx = existingContent.indexOf(GITNEXUS_START_MARKER);
  const endIdx = existingContent.indexOf(GITNEXUS_END_MARKER);

  if (startIdx !== -1 && endIdx !== -1 && endIdx > startIdx) {
    const before = existingContent.substring(0, startIdx);
    const after = existingContent.substring(endIdx + GITNEXUS_END_MARKER.length);
    const nextContent = `${before}${content}${after}`.trimEnd() + '\n';
    await fs.writeFile(filePath, nextContent, 'utf-8');
    return 'updated';
  }

  const nextContent = `${existingContent.trimEnd()}\n\n${content.trimEnd()}\n`;
  await fs.writeFile(filePath, nextContent, 'utf-8');
  return 'appended';
}

function generateCodexAgentsContent(): string {
  return `${GITNEXUS_START_MARKER}
# GitNexus — Global Workflow

When you are inside an indexed repository, use GitNexus to understand structure, blast radius, and execution flow before making changes.

## Search Flow

- Prefer fast search first:
  - Use \`fff\` MCP tools when available for raw file/content search
  - Otherwise use Bash \`rg\`, \`grep\`, or \`find\`
- After locating the symbol or file, switch to GitNexus for meaning and safety:
  - \`gitnexus_query({query: "concept"})\` for architecture and execution flows
  - \`gitnexus_context({name: "symbol"})\` for callers/callees and process participation
  - \`gitnexus_impact({target: "symbol", direction: "upstream"})\` before editing

## Hooks

- A global Codex Bash hook is installed in \`~/.codex/hooks.json\`
- Bash search commands can be enriched with GitNexus context automatically
- Bash git mutations can warn when the GitNexus index is stale
- Native MCP tools like \`fff\` are not hooked directly, so use the search flow above

## Skills

| Task | Read this skill file |
|------|----------------------|
| Understand architecture / "How does X work?" | \`~/.agents/skills/gitnexus-exploring/SKILL.md\` |
| Blast radius / "What breaks if I change X?" | \`~/.agents/skills/gitnexus-impact-analysis/SKILL.md\` |
| Trace bugs / "Why is X failing?" | \`~/.agents/skills/gitnexus-debugging/SKILL.md\` |
| Rename / extract / split / refactor | \`~/.agents/skills/gitnexus-refactoring/SKILL.md\` |
| Tools, resources, schema reference | \`~/.agents/skills/gitnexus-guide/SKILL.md\` |
| Index, status, clean, wiki CLI commands | \`~/.agents/skills/gitnexus-cli/SKILL.md\` |
| Review pull requests | \`~/.agents/skills/gitnexus-pr-review/SKILL.md\` |

## Rules

- Always run impact analysis before editing a symbol in an indexed repo
- Run \`gitnexus_detect_changes()\` before committing when GitNexus MCP is available
- Reindex after structural changes or git mutations when warned by the hook

${GITNEXUS_END_MARKER}`;
}

/**
 * Copy the bundled GitNexus hook script into a per-agent hooks directory.
 * Returns the absolute destination path.
 */
async function installBundledHookScript(
  destHooksDir: string,
  hookVariant: 'claude' | 'codex',
): Promise<string> {
  const pluginHooksPath = path.join(__dirname, '..', '..', 'hooks', hookVariant);
  const src = path.join(pluginHooksPath, 'gitnexus-hook.cjs');
  const dest = path.join(destHooksDir, 'gitnexus-hook.cjs');

  await fs.mkdir(destHooksDir, { recursive: true });

  let content = await fs.readFile(src, 'utf-8');
  // Inject resolved CLI path so the copied hook can find the CLI
  // even when it's no longer inside the npm package tree.
  const resolvedCli = path.join(__dirname, '..', 'cli', 'index.js');
  const normalizedCli = path.resolve(resolvedCli).replace(/\\/g, '/');
  const jsonCli = JSON.stringify(normalizedCli);
  content = content.replace(
    "let cliPath = path.resolve(__dirname, '..', '..', 'dist', 'cli', 'index.js');",
    `let cliPath = ${jsonCli};`,
  );

  await fs.writeFile(dest, content, 'utf-8');
  return dest;
}

interface HookEntry {
  matcher?: string;
  hooks?: Array<{ command?: string }>;
}

function ensureCommandHookEntry(
  existing: any,
  eventName: string,
  matcher: string | undefined,
  hookCmd: string,
  timeout: number,
  statusMessage: string,
) {
  if (!existing.hooks || typeof existing.hooks !== 'object') existing.hooks = {};
  if (!Array.isArray(existing.hooks[eventName])) existing.hooks[eventName] = [];

  const hasHook = existing.hooks[eventName].some((entry: HookEntry) => {
    const matcherMatches = matcher ? entry.matcher === matcher : !entry.matcher;
    return matcherMatches && entry.hooks?.some((hook) => hook.command === hookCmd);
  });

  if (hasHook) return;

  const nextEntry: { matcher?: string; hooks: Array<Record<string, any>> } = {
    hooks: [{ type: 'command', command: hookCmd, timeout, statusMessage }],
  };
  if (matcher) nextEntry.matcher = matcher;
  existing.hooks[eventName].push(nextEntry);
}

/**
 * Check if a directory exists
 */
async function dirExists(dirPath: string): Promise<boolean> {
  try {
    const stat = await fs.stat(dirPath);
    return stat.isDirectory();
  } catch {
    return false;
  }
}

// ─── Editor-specific setup ─────────────────────────────────────────

async function setupCursor(result: SetupResult): Promise<void> {
  const cursorDir = path.join(os.homedir(), '.cursor');
  if (!(await dirExists(cursorDir))) {
    result.skipped.push('Cursor (not installed)');
    return;
  }

  const mcpPath = path.join(cursorDir, 'mcp.json');
  try {
    const existing = await readJsonFile(mcpPath);
    const updated = mergeMcpConfig(existing);
    await writeJsonFile(mcpPath, updated);
    result.configured.push('Cursor');
  } catch (err: any) {
    result.errors.push(`Cursor: ${err.message}`);
  }
}

async function setupClaudeCode(result: SetupResult): Promise<void> {
  const claudeDir = path.join(os.homedir(), '.claude');
  if (!(await dirExists(claudeDir))) {
    result.skipped.push('Claude Code (not installed)');
    return;
  }

  // Claude Code stores MCP config in ~/.claude.json
  const mcpPath = path.join(os.homedir(), '.claude.json');
  try {
    const existing = await readJsonFile(mcpPath);
    const updated = mergeMcpConfig(existing);
    await writeJsonFile(mcpPath, updated);
    result.configured.push('Claude Code');
  } catch (err: any) {
    result.errors.push(`Claude Code: ${err.message}`);
  }
}

/**
 * Install GitNexus skills to ~/.claude/skills/ for Claude Code.
 */
async function installClaudeCodeSkills(result: SetupResult): Promise<void> {
  const claudeDir = path.join(os.homedir(), '.claude');
  if (!(await dirExists(claudeDir))) return;

  const skillsDir = path.join(claudeDir, 'skills');
  try {
    const installed = await installSkillsTo(skillsDir);
    if (installed.length > 0) {
      result.configured.push(`Claude Code skills (${installed.length} skills → ~/.claude/skills/)`);
    }
  } catch (err: any) {
    result.errors.push(`Claude Code skills: ${err.message}`);
  }
}

/**
 * Install GitNexus hooks to ~/.claude/settings.json for Claude Code.
 * Merges hook config without overwriting existing hooks.
 */
async function installClaudeCodeHooks(result: SetupResult): Promise<void> {
  const claudeDir = path.join(os.homedir(), '.claude');
  if (!(await dirExists(claudeDir))) return;

  const settingsPath = path.join(claudeDir, 'settings.json');

  // Copy unified hook script to ~/.claude/hooks/gitnexus/
  const destHooksDir = path.join(claudeDir, 'hooks', 'gitnexus');

  try {
    const hookPath = (await installBundledHookScript(destHooksDir, 'claude')).replace(/\\/g, '/');
    const hookCmd = `node "${hookPath.replace(/"/g, '\\"')}"`;

    // Merge hook config into ~/.claude/settings.json
    const existing = (await readJsonFile(settingsPath)) || {};
    // NOTE: SessionStart hooks are broken on Windows (Claude Code bug #23576).
    // Session context is delivered via CLAUDE.md / skills instead.

    ensureCommandHookEntry(
      existing,
      'PreToolUse',
      'Grep|Glob|Bash',
      hookCmd,
      10,
      'Enriching with GitNexus graph context...',
    );
    ensureCommandHookEntry(
      existing,
      'PostToolUse',
      'Bash',
      hookCmd,
      10,
      'Checking GitNexus index freshness...',
    );

    await writeJsonFile(settingsPath, existing);
    result.configured.push('Claude Code hooks (PreToolUse, PostToolUse)');
  } catch (err: any) {
    result.errors.push(`Claude Code hooks: ${err.message}`);
  }
}

async function setupOpenCode(result: SetupResult): Promise<void> {
  const opencodeDir = path.join(os.homedir(), '.config', 'opencode');
  if (!(await dirExists(opencodeDir))) {
    result.skipped.push('OpenCode (not installed)');
    return;
  }

  const configPath = path.join(opencodeDir, 'config.json');
  try {
    const existing = await readJsonFile(configPath);
    const config = existing || {};
    if (!config.mcp) config.mcp = {};
    config.mcp.gitnexus = getMcpEntry();
    await writeJsonFile(configPath, config);
    result.configured.push('OpenCode');
  } catch (err: any) {
    result.errors.push(`OpenCode: ${err.message}`);
  }
}

/**
 * Build a TOML section for Codex MCP config (~/.codex/config.toml).
 */
function getCodexMcpTomlSection(): string {
  const entry = getMcpEntry();
  const command = JSON.stringify(entry.command);
  const args = `[${entry.args.map((arg) => JSON.stringify(arg)).join(', ')}]`;
  return `[mcp_servers.gitnexus]\ncommand = ${command}\nargs = ${args}\n`;
}

function upsertTomlKey(content: string, sectionName: string, key: string, value: string): string {
  const sectionRegex = new RegExp(
    `(^|\\n)\\[${escapeRegExp(sectionName)}\\]\\n([\\s\\S]*?)(?=\\n\\[[^\\]]+\\]|$)`,
  );
  const keyRegex = new RegExp(`^${escapeRegExp(key)}\\s*=`, 'm');
  const keyLineRegex = new RegExp(`^\\s*${escapeRegExp(key)}\\s*=.*$`, 'm');
  const line = `${key} = ${value}`;

  if (!content.trim()) {
    return `[${sectionName}]\n${line}\n`;
  }

  if (!sectionRegex.test(content)) {
    return `${content.trimEnd()}\n\n[${sectionName}]\n${line}\n`;
  }

  return content.replace(sectionRegex, (fullMatch, prefix, body) => {
    if (keyRegex.test(body)) {
      const replacedBody = body.replace(keyLineRegex, line);
      const normalizedBody =
        replacedBody.endsWith('\n') || replacedBody.length === 0 ? replacedBody : `${replacedBody}\n`;
      return `${prefix}[${sectionName}]\n${normalizedBody}`;
    }
    const normalizedBody = body.endsWith('\n') || body.length === 0 ? body : `${body}\n`;
    return `${prefix}[${sectionName}]\n${normalizedBody}${line}\n`;
  });
}

/**
 * Append GitNexus MCP server config to Codex's config.toml if missing.
 */
async function upsertCodexConfigToml(configPath: string): Promise<void> {
  let existing = '';
  try {
    existing = await fs.readFile(configPath, 'utf-8');
  } catch {
    existing = '';
  }

  let nextContent = existing;
  if (!nextContent.includes('[mcp_servers.gitnexus]')) {
    const section = getCodexMcpTomlSection();
    nextContent =
      nextContent.trim().length > 0 ? `${nextContent.trimEnd()}\n\n${section}` : section;
  }
  nextContent = upsertTomlKey(nextContent, 'features', 'codex_hooks', 'true');

  await fs.mkdir(path.dirname(configPath), { recursive: true });
  await fs.writeFile(configPath, `${nextContent.trimEnd()}\n`, 'utf-8');
}

async function setupCodex(result: SetupResult): Promise<void> {
  const codexDir = path.join(os.homedir(), '.codex');
  if (!(await dirExists(codexDir))) {
    result.skipped.push('Codex (not installed)');
    return;
  }

  const configPath = path.join(codexDir, 'config.toml');
  let usedCli = false;

  try {
    const entry = getMcpEntry();
    await execFileAsync('codex', ['mcp', 'add', 'gitnexus', '--', entry.command, ...entry.args], {
      shell: process.platform === 'win32',
    });
    usedCli = true;
  } catch {
    // Fallback for environments where `codex` binary isn't on PATH.
  }

  try {
    await upsertCodexConfigToml(configPath);
    result.configured.push(
      usedCli ? 'Codex' : 'Codex (MCP added to ~/.codex/config.toml)',
    );
  } catch (err: any) {
    result.errors.push(`Codex: ${err.message}`);
  }
}

async function installCodexHooks(result: SetupResult): Promise<void> {
  const codexDir = path.join(os.homedir(), '.codex');
  if (!(await dirExists(codexDir))) return;

  const hooksPath = path.join(codexDir, 'hooks.json');
  const destHooksDir = path.join(codexDir, 'hooks', 'gitnexus');

  try {
    const hookPath = (await installBundledHookScript(destHooksDir, 'codex')).replace(/\\/g, '/');
    const hookCmd = `node "${hookPath.replace(/"/g, '\\"')}"`;
    const existing = (await readJsonFile(hooksPath)) || {};

    ensureCommandHookEntry(
      existing,
      'PostToolUse',
      'Bash',
      hookCmd,
      10,
      'Reviewing Bash output with GitNexus...',
    );

    await writeJsonFile(hooksPath, existing);
    result.configured.push('Codex hooks (PostToolUse Bash)');
  } catch (err: any) {
    result.errors.push(`Codex hooks: ${err.message}`);
  }
}

async function installCodexAgentsFile(result: SetupResult): Promise<void> {
  const codexDir = path.join(os.homedir(), '.codex');
  if (!(await dirExists(codexDir))) return;

  const agentsPath = path.join(codexDir, 'AGENTS.md');

  try {
    const action = await upsertMarkedSection(agentsPath, generateCodexAgentsContent());
    result.configured.push(`Codex AGENTS.md (${action} → ~/.codex/AGENTS.md)`);
  } catch (err: any) {
    result.errors.push(`Codex AGENTS.md: ${err.message}`);
  }
}

// ─── Skill Installation ───────────────────────────────────────────

/**
 * Install GitNexus skills to a target directory.
 * Each skill is installed as {targetDir}/gitnexus-{skillName}/SKILL.md
 * following the Agent Skills standard (Cursor, Claude Code, and Codex).
 *
 * Supports two source layouts:
 *   - Flat file:  skills/{name}.md           → copied as SKILL.md
 *   - Directory:  skills/{name}/SKILL.md     → copied recursively (includes references/, etc.)
 */
async function installSkillsTo(targetDir: string): Promise<string[]> {
  const installed: string[] = [];
  const skillsRoot = path.join(__dirname, '..', '..', 'skills');

  let flatFiles: string[] = [];
  let dirSkillFiles: string[] = [];
  try {
    [flatFiles, dirSkillFiles] = await Promise.all([
      glob('*.md', { cwd: skillsRoot }),
      glob('*/SKILL.md', { cwd: skillsRoot }),
    ]);
  } catch {
    return [];
  }

  const skillSources = new Map<string, { isDirectory: boolean }>();

  for (const relPath of dirSkillFiles) {
    skillSources.set(path.dirname(relPath), { isDirectory: true });
  }
  for (const relPath of flatFiles) {
    const skillName = path.basename(relPath, '.md');
    if (!skillSources.has(skillName)) {
      skillSources.set(skillName, { isDirectory: false });
    }
  }

  for (const [skillName, source] of skillSources) {
    const skillDir = path.join(targetDir, skillName);

    try {
      if (source.isDirectory) {
        const dirSource = path.join(skillsRoot, skillName);
        await copyDirRecursive(dirSource, skillDir);
        installed.push(skillName);
      } else {
        const flatSource = path.join(skillsRoot, `${skillName}.md`);
        const content = await fs.readFile(flatSource, 'utf-8');
        await fs.mkdir(skillDir, { recursive: true });
        await fs.writeFile(path.join(skillDir, 'SKILL.md'), content, 'utf-8');
        installed.push(skillName);
      }
    } catch {
      // Source skill not found — skip
    }
  }

  return installed;
}

/**
 * Recursively copy a directory tree.
 */
async function copyDirRecursive(src: string, dest: string): Promise<void> {
  await fs.mkdir(dest, { recursive: true });
  const entries = await fs.readdir(src, { withFileTypes: true });
  for (const entry of entries) {
    const srcPath = path.join(src, entry.name);
    const destPath = path.join(dest, entry.name);
    if (entry.isDirectory()) {
      await copyDirRecursive(srcPath, destPath);
    } else {
      await fs.copyFile(srcPath, destPath);
    }
  }
}

/**
 * Install global Cursor skills to ~/.cursor/skills/gitnexus/
 */
async function installCursorSkills(result: SetupResult): Promise<void> {
  const cursorDir = path.join(os.homedir(), '.cursor');
  if (!(await dirExists(cursorDir))) return;

  const skillsDir = path.join(cursorDir, 'skills');
  try {
    const installed = await installSkillsTo(skillsDir);
    if (installed.length > 0) {
      result.configured.push(`Cursor skills (${installed.length} skills → ~/.cursor/skills/)`);
    }
  } catch (err: any) {
    result.errors.push(`Cursor skills: ${err.message}`);
  }
}

/**
 * Install global OpenCode skills to ~/.config/opencode/skill/gitnexus/
 */
async function installOpenCodeSkills(result: SetupResult): Promise<void> {
  const opencodeDir = path.join(os.homedir(), '.config', 'opencode');
  if (!(await dirExists(opencodeDir))) return;

  const skillsDir = path.join(opencodeDir, 'skill');
  try {
    const installed = await installSkillsTo(skillsDir);
    if (installed.length > 0) {
      result.configured.push(
        `OpenCode skills (${installed.length} skills → ~/.config/opencode/skill/)`,
      );
    }
  } catch (err: any) {
    result.errors.push(`OpenCode skills: ${err.message}`);
  }
}

/**
 * Install global Codex skills to ~/.agents/skills/gitnexus/
 */
async function installCodexSkills(result: SetupResult): Promise<void> {
  const codexDir = path.join(os.homedir(), '.codex');
  if (!(await dirExists(codexDir))) return;

  const skillsDir = path.join(os.homedir(), '.agents', 'skills');
  try {
    const installed = await installSkillsTo(skillsDir);
    if (installed.length > 0) {
      result.configured.push(`Codex skills (${installed.length} skills → ~/.agents/skills/)`);
    }
  } catch (err: any) {
    result.errors.push(`Codex skills: ${err.message}`);
  }
}

// ─── Main command ──────────────────────────────────────────────────

export const setupCommand = async () => {
  console.log('');
  console.log('  GitNexus Setup');
  console.log('  ==============');
  console.log('');

  // Ensure global directory exists
  const globalDir = getGlobalDir();
  await fs.mkdir(globalDir, { recursive: true });

  const result: SetupResult = {
    configured: [],
    skipped: [],
    errors: [],
  };

  // Detect and configure each editor's MCP
  await setupCursor(result);
  await setupClaudeCode(result);
  await setupOpenCode(result);
  await setupCodex(result);

  // Install global skills for platforms that support them
  await installClaudeCodeSkills(result);
  await installClaudeCodeHooks(result);
  await installCursorSkills(result);
  await installOpenCodeSkills(result);
  await installCodexSkills(result);
  await installCodexHooks(result);
  await installCodexAgentsFile(result);

  // Print results
  if (result.configured.length > 0) {
    console.log('  Configured:');
    for (const name of result.configured) {
      console.log(`    + ${name}`);
    }
  }

  if (result.skipped.length > 0) {
    console.log('');
    console.log('  Skipped:');
    for (const name of result.skipped) {
      console.log(`    - ${name}`);
    }
  }

  if (result.errors.length > 0) {
    console.log('');
    console.log('  Errors:');
    for (const err of result.errors) {
      console.log(`    ! ${err}`);
    }
  }

  console.log('');
  console.log('  Summary:');
  console.log(
    `    MCP configured for: ${result.configured.filter((c) => !c.includes('skills')).join(', ') || 'none'}`,
  );
  console.log(
    `    Skills installed to: ${result.configured.filter((c) => c.includes('skills')).length > 0 ? result.configured.filter((c) => c.includes('skills')).join(', ') : 'none'}`,
  );
  console.log('');
  console.log('  Next steps:');
  console.log('    1. cd into any git repo');
  console.log('    2. Run: gitnexus analyze');
  console.log('    3. Open the repo in your editor — MCP is ready!');
  console.log('');
};
