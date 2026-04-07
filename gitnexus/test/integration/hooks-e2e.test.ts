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

const CLAUDE_LIKE_HOOKS = [
  { name: 'Claude', path: CLAUDE_HOOK },
  { name: 'Plugin', path: PLUGIN_HOOK },
];

let tmpDir: string;
let gitNexusDir: string;
let cliStubPath: string;
let hookEnv: NodeJS.ProcessEnv;

beforeAll(() => {
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hooks-e2e-'));
  gitNexusDir = path.join(tmpDir, '.gitnexus');
  fs.mkdirSync(gitNexusDir, { recursive: true });

  spawnSync('git', ['init'], { cwd: tmpDir, stdio: 'pipe' });
  spawnSync('git', ['config', 'user.email', 'test@test.com'], { cwd: tmpDir, stdio: 'pipe' });
  spawnSync('git', ['config', 'user.name', 'Test'], { cwd: tmpDir, stdio: 'pipe' });

  fs.writeFileSync(path.join(tmpDir, 'hello.txt'), 'hello');
  spawnSync('git', ['add', '.'], { cwd: tmpDir, stdio: 'pipe' });
  spawnSync('git', ['commit', '-m', 'init'], { cwd: tmpDir, stdio: 'pipe' });

  cliStubPath = path.join(tmpDir, 'gitnexus-cli-stub.js');
  fs.writeFileSync(
    cliStubPath,
    `#!/usr/bin/env node
const args = process.argv.slice(2);
if (args[0] === 'augment') {
  const pattern = args[2] || '';
  process.stderr.write('[GitNexus] stub result for ' + pattern + '\\n');
  process.exit(0);
}
process.stderr.write('unexpected command\\n');
process.exit(1);
`,
    'utf-8',
  );
  hookEnv = { GITNEXUS_HOOK_CLI_PATH: cliStubPath };
});

afterAll(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
});

function writeMeta(lastCommit: string, embeddings = 0) {
  fs.writeFileSync(path.join(gitNexusDir, 'meta.json'), JSON.stringify({ lastCommit, stats: { embeddings } }));
}

describe.each(CLAUDE_LIKE_HOOKS)('$name hook e2e', ({ path: hookPath }) => {
  it('augments PreToolUse Grep searches through the injected CLI path', () => {
    const result = runHook(
      hookPath,
      {
        hook_event_name: 'PreToolUse',
        tool_name: 'Grep',
        tool_input: { pattern: 'claimDraftContentUpdate' },
        cwd: tmpDir,
      },
      { env: hookEnv },
    );

    const output = parseHookOutput(result.stdout);
    expect(output).not.toBeNull();
    expect(output!.hookEventName).toBe('PreToolUse');
    expect(output!.additionalContext).toContain('claimDraftContentUpdate');
  });

  it('augments PreToolUse Bash rg searches through the injected CLI path', () => {
    const result = runHook(
      hookPath,
      {
        hook_event_name: 'PreToolUse',
        tool_name: 'Bash',
        tool_input: { command: 'rg TaskActionPreconditionError src tests' },
        cwd: tmpDir,
      },
      { env: hookEnv },
    );

    const output = parseHookOutput(result.stdout);
    expect(output).not.toBeNull();
    expect(output!.additionalContext).toContain('TaskActionPreconditionError');
  });

  it('emits a stale warning when HEAD differs from meta.json', () => {
    writeMeta('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');

    const result = runHook(hookPath, {
      hook_event_name: 'PostToolUse',
      tool_name: 'Bash',
      tool_input: { command: 'git commit -m "test"' },
      tool_output: { exit_code: 0 },
      cwd: tmpDir,
    });

    const output = parseHookOutput(result.stdout);
    expect(output).not.toBeNull();
    expect(output!.additionalContext).toContain('stale');
  });

  it('stays silent when meta.json lastCommit matches HEAD', () => {
    const head = spawnSync('git', ['rev-parse', 'HEAD'], {
      cwd: tmpDir,
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    }).stdout.trim();

    writeMeta(head);

    const result = runHook(hookPath, {
      hook_event_name: 'PostToolUse',
      tool_name: 'Bash',
      tool_input: { command: 'git commit -m "test"' },
      tool_output: { exit_code: 0 },
      cwd: tmpDir,
    });

    expect(parseHookOutput(result.stdout)).toBeNull();
  });
});

describe('Codex hook e2e', () => {
  it('augments Bash rg searches on PostToolUse using Codex payloads', () => {
    const result = runHook(
      CODEX_HOOK,
      {
        hook_event_name: 'PostToolUse',
        tool_name: 'Bash',
        tool_input: { command: 'rg TaskActionPreconditionError src tests' },
        tool_response: JSON.stringify({ exitCode: 0 }),
        cwd: tmpDir,
      },
      { env: hookEnv },
    );

    const output = parseHookOutput(result.stdout);
    expect(output).not.toBeNull();
    expect(output!.hookEventName).toBe('PostToolUse');
    expect(output!.additionalContext).toContain('TaskActionPreconditionError');
  });

  it('emits stale warnings from tool_response payloads', () => {
    writeMeta('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 42);

    const result = runHook(CODEX_HOOK, {
      hook_event_name: 'PostToolUse',
      tool_name: 'Bash',
      tool_input: { command: 'git commit -m "test"' },
      tool_response: JSON.stringify({ exitCode: 0 }),
      cwd: tmpDir,
    });

    const output = parseHookOutput(result.stdout);
    expect(output).not.toBeNull();
    expect(output!.additionalContext).toContain('stale');
    expect(output!.additionalContext).toContain('--embeddings');
  });

  it('emits both augmentation and stale warning for mixed Bash commands', () => {
    writeMeta('cccccccccccccccccccccccccccccccccccccccc');

    const result = runHook(
      CODEX_HOOK,
      {
        hook_event_name: 'PostToolUse',
        tool_name: 'Bash',
        tool_input: { command: 'rg claimDraftContentUpdate src tests && git commit -m "test"' },
        tool_response: JSON.stringify({ exitCode: 0 }),
        cwd: tmpDir,
      },
      { env: hookEnv },
    );

    const output = parseHookOutput(result.stdout);
    expect(output).not.toBeNull();
    expect(output!.additionalContext).toContain('claimDraftContentUpdate');
    expect(output!.additionalContext).toContain('stale');
  });

  it('fails closed on ambiguous tool_response payloads', () => {
    writeMeta('dddddddddddddddddddddddddddddddddddddddd');

    const result = runHook(CODEX_HOOK, {
      hook_event_name: 'PostToolUse',
      tool_name: 'Bash',
      tool_input: { command: 'git commit -m "test"' },
      tool_response: JSON.stringify({ status: 'ok' }),
      cwd: tmpDir,
    });

    expect(parseHookOutput(result.stdout)).toBeNull();
  });
});
