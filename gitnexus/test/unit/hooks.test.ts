import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { spawnSync } from 'child_process';
import fs from 'fs';
import path from 'path';
import os from 'os';
import { runHook, parseHookOutput } from '../utils/hook-test-helpers.js';

const CLAUDE_HOOK = path.resolve(__dirname, '..', '..', 'hooks', 'claude', 'gitnexus-hook.cjs');
const CODEX_HOOK = path.resolve(__dirname, '..', '..', 'hooks', 'codex', 'gitnexus-hook.cjs');
const PLUGIN_HOOK = path.resolve(
  __dirname,
  '..',
  '..',
  '..',
  'gitnexus-claude-plugin',
  'hooks',
  'gitnexus-hook.js',
);
const SHARED_CORE = path.resolve(__dirname, '..', '..', 'hooks', 'shared', 'gitnexus-hook-core.cjs');
const PLUGIN_SHARED_CORE = path.resolve(
  __dirname,
  '..',
  '..',
  '..',
  'gitnexus-claude-plugin',
  'hooks',
  'shared',
  'gitnexus-hook-core.cjs',
);

let tmpDir: string;
let gitNexusDir: string;

beforeAll(() => {
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'gitnexus-hook-test-'));
  gitNexusDir = path.join(tmpDir, '.gitnexus');
  fs.mkdirSync(gitNexusDir, { recursive: true });

  spawnSync('git', ['init'], { cwd: tmpDir, stdio: 'pipe' });
  spawnSync('git', ['config', 'user.email', 'test@test.com'], { cwd: tmpDir, stdio: 'pipe' });
  spawnSync('git', ['config', 'user.name', 'Test'], { cwd: tmpDir, stdio: 'pipe' });
  fs.writeFileSync(path.join(tmpDir, 'dummy.txt'), 'hello');
  spawnSync('git', ['add', '.'], { cwd: tmpDir, stdio: 'pipe' });
  spawnSync('git', ['commit', '-m', 'init'], { cwd: tmpDir, stdio: 'pipe' });
});

afterAll(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
});

function getHeadCommit(): string {
  const result = spawnSync('git', ['rev-parse', 'HEAD'], {
    cwd: tmpDir,
    encoding: 'utf-8',
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  return (result.stdout || '').trim();
}

describe('Hook runtime files', () => {
  it('keeps the packaged and plugin hook files present', () => {
    expect(fs.existsSync(CLAUDE_HOOK)).toBe(true);
    expect(fs.existsSync(CODEX_HOOK)).toBe(true);
    expect(fs.existsSync(PLUGIN_HOOK)).toBe(true);
    expect(fs.existsSync(SHARED_CORE)).toBe(true);
    expect(fs.existsSync(PLUGIN_SHARED_CORE)).toBe(true);
  });

  it('vendors the same shared core into the standalone plugin', () => {
    expect(fs.readFileSync(PLUGIN_SHARED_CORE, 'utf-8')).toBe(fs.readFileSync(SHARED_CORE, 'utf-8'));
  });

  it('keeps adapter semantics isolated by runtime', () => {
    const claudeSource = fs.readFileSync(CLAUDE_HOOK, 'utf-8');
    const codexSource = fs.readFileSync(CODEX_HOOK, 'utf-8');
    const pluginSource = fs.readFileSync(PLUGIN_HOOK, 'utf-8');

    expect(claudeSource).toContain('createClaudeHandlers');
    expect(claudeSource).toContain("'__GITNEXUS_CLI_PATH__'");
    expect(claudeSource).not.toContain('createCodexHandlers');

    expect(codexSource).toContain('createCodexHandlers');
    expect(codexSource).toContain("'__GITNEXUS_CLI_PATH__'");
    expect(codexSource).not.toContain('createClaudeHandlers');

    expect(pluginSource).toContain('createBinaryCliRunner');
    expect(pluginSource).toContain('createClaudeHandlers');
    expect(pluginSource).not.toContain('createCodexHandlers');
  });

  it('keeps command spawning in the shared core shell-free and Windows-aware', () => {
    const source = fs.readFileSync(SHARED_CORE, 'utf-8');
    expect(source).not.toMatch(/shell:\s*(true|isWin)/);
    expect(source).toContain('npx.cmd');
    expect(source).toContain('gitnexus.cmd');
    expect(source).toContain('GITNEXUS_HOOK_CLI_PATH');
  });
});

describe('Hook stale detection behavior', () => {
  for (const [label, hookPath, payload] of [
    [
      'Claude',
      CLAUDE_HOOK,
      {
        hook_event_name: 'PostToolUse',
        tool_name: 'Bash',
        tool_input: { command: 'git commit -m "test"' },
        tool_output: { exit_code: 0 },
      },
    ],
    [
      'Plugin',
      PLUGIN_HOOK,
      {
        hook_event_name: 'PostToolUse',
        tool_name: 'Bash',
        tool_input: { command: 'git commit -m "test"' },
        tool_output: { exit_code: 0 },
      },
    ],
  ] as const) {
    it(`${label} emits a stale warning when HEAD differs from meta`, () => {
      fs.writeFileSync(
        path.join(gitNexusDir, 'meta.json'),
        JSON.stringify({ lastCommit: 'aaaaaaa0000000000000000000000000deadbeef', stats: {} }),
      );

      const result = runHook(hookPath, { ...payload, cwd: tmpDir });
      const output = parseHookOutput(result.stdout);

      expect(output).not.toBeNull();
      expect(output!.hookEventName).toBe('PostToolUse');
      expect(output!.additionalContext).toContain('stale');
    });

    it(`${label} stays silent when HEAD matches meta`, () => {
      fs.writeFileSync(
        path.join(gitNexusDir, 'meta.json'),
        JSON.stringify({ lastCommit: getHeadCommit(), stats: {} }),
      );

      const result = runHook(hookPath, { ...payload, cwd: tmpDir });
      expect(result.stdout.trim()).toBe('');
    });
  }

  it('Codex parses tool_response payloads and emits stale warnings', () => {
    fs.writeFileSync(
      path.join(gitNexusDir, 'meta.json'),
      JSON.stringify({ lastCommit: 'aaaaaaa0000000000000000000000000deadbeef', stats: {} }),
    );

    const result = runHook(CODEX_HOOK, {
      hook_event_name: 'PostToolUse',
      tool_name: 'Bash',
      tool_input: { command: 'git commit -m "test"' },
      tool_response: JSON.stringify({ exitCode: 0 }),
      cwd: tmpDir,
    });

    const output = parseHookOutput(result.stdout);
    expect(output).not.toBeNull();
    expect(output!.hookEventName).toBe('PostToolUse');
    expect(output!.additionalContext).toContain('stale');
  });

  it('Codex fails closed on ambiguous tool_response payloads', () => {
    const result = runHook(CODEX_HOOK, {
      hook_event_name: 'PostToolUse',
      tool_name: 'Bash',
      tool_input: { command: 'git commit -m "test"' },
      tool_response: JSON.stringify({ status: 'ok' }),
      cwd: tmpDir,
    });

    expect(result.stdout.trim()).toBe('');
  });

  it('all runtimes reject relative cwd values', () => {
    for (const hookPath of [CLAUDE_HOOK, CODEX_HOOK, PLUGIN_HOOK]) {
      const result = runHook(hookPath, {
        hook_event_name: 'PostToolUse',
        tool_name: 'Bash',
        tool_input: { command: 'git commit -m "test"' },
        tool_output: { exit_code: 0 },
        cwd: 'relative/path',
      });
      expect(result.stdout.trim()).toBe('');
    }
  });
});
