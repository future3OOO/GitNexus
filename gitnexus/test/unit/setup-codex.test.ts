import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'fs/promises';
import os from 'os';
import path from 'path';

const execFileMock = vi.fn((...args: any[]) => {
  const callback = args.at(-1);
  if (typeof callback === 'function') {
    callback(null, '', '');
  }
});

vi.mock('child_process', () => ({
  execFile: execFileMock,
}));

describe('setupCommand codex execution', () => {
  let tempHome: string;
  let originalHome: string | undefined;
  let originalUserProfile: string | undefined;
  let platformDescriptor: PropertyDescriptor | undefined;

  const setPlatform = (value: NodeJS.Platform) => {
    Object.defineProperty(process, 'platform', {
      value,
      configurable: true,
    });
  };

  beforeEach(async () => {
    vi.resetModules();
    vi.clearAllMocks();

    originalHome = process.env.HOME;
    originalUserProfile = process.env.USERPROFILE;
    tempHome = await fs.mkdtemp(path.join(os.tmpdir(), 'gn-codex-setup-'));
    process.env.HOME = tempHome;
    process.env.USERPROFILE = tempHome;

    await fs.mkdir(path.join(tempHome, '.codex'), { recursive: true });

    platformDescriptor = Object.getOwnPropertyDescriptor(process, 'platform');
    setPlatform('win32');
    vi.spyOn(console, 'log').mockImplementation(() => {});
  });

  afterEach(async () => {
    vi.restoreAllMocks();

    if (platformDescriptor) {
      Object.defineProperty(process, 'platform', platformDescriptor);
    }

    process.env.HOME = originalHome;
    process.env.USERPROFILE = originalUserProfile;
    await fs.rm(tempHome, { recursive: true, force: true });
  });

  it('invokes codex mcp add with shell enabled on Windows', async () => {
    const { setupCommand } = await import('../../src/cli/setup.js');

    await setupCommand();

    expect(execFileMock).toHaveBeenCalledWith(
      'codex',
      ['mcp', 'add', 'gitnexus', '--', 'cmd', '/c', 'npx', '-y', 'gitnexus@latest', 'mcp'],
      { shell: true },
      expect.any(Function),
    );

    const codexConfig = await fs.readFile(path.join(tempHome, '.codex', 'config.toml'), 'utf-8');
    expect(codexConfig).toContain('[mcp_servers.gitnexus]');
    expect(codexConfig).toContain('[features]');
    expect(codexConfig).toContain('codex_hooks = true');

    const hooksJson = JSON.parse(await fs.readFile(path.join(tempHome, '.codex', 'hooks.json'), 'utf-8'));
    expect(hooksJson.hooks.PostToolUse).toHaveLength(1);
    expect(hooksJson.hooks.PostToolUse[0].matcher).toBe('Bash');
    expect(hooksJson.hooks.PostToolUse[0].hooks[0].command).toContain('gitnexus-hook.cjs');

    await expect(
      fs.access(path.join(tempHome, '.codex', 'hooks', 'gitnexus', 'gitnexus-hook.cjs')),
    ).resolves.toBeUndefined();
    const installedHook = await fs.readFile(
      path.join(tempHome, '.codex', 'hooks', 'gitnexus', 'gitnexus-hook.cjs'),
      'utf-8',
    );
    expect(installedHook).toContain('GitNexus Codex Hook');

    const agentsContent = await fs.readFile(path.join(tempHome, '.codex', 'AGENTS.md'), 'utf-8');
    expect(agentsContent).toContain('<!-- gitnexus:start -->');
    expect(agentsContent).toContain('~/.agents/skills/gitnexus-exploring/SKILL.md');
    expect(agentsContent).toContain('~/.codex/hooks.json');
  });

  it('invokes codex mcp add without shell on non-Windows and still enables global Codex hooks', async () => {
    setPlatform('darwin');

    const { setupCommand } = await import('../../src/cli/setup.js');

    await setupCommand();

    expect(execFileMock).toHaveBeenCalledWith(
      'codex',
      ['mcp', 'add', 'gitnexus', '--', 'npx', '-y', 'gitnexus@latest', 'mcp'],
      { shell: false },
      expect.any(Function),
    );

    const codexConfig = await fs.readFile(path.join(tempHome, '.codex', 'config.toml'), 'utf-8');
    expect(codexConfig).toContain('[mcp_servers.gitnexus]');
    expect(codexConfig).toContain('codex_hooks = true');

    const hooksJson = JSON.parse(await fs.readFile(path.join(tempHome, '.codex', 'hooks.json'), 'utf-8'));
    expect(hooksJson.hooks.PostToolUse[0].matcher).toBe('Bash');

    const agentsContent = await fs.readFile(path.join(tempHome, '.codex', 'AGENTS.md'), 'utf-8');
    expect(agentsContent).toContain('GitNexus — Global Workflow');
  });

  it('skips Codex setup entirely when ~/.codex is missing', async () => {
    await fs.rm(path.join(tempHome, '.codex'), { recursive: true, force: true });

    const { setupCommand } = await import('../../src/cli/setup.js');

    await setupCommand();

    expect(execFileMock).not.toHaveBeenCalled();
    await expect(fs.access(path.join(tempHome, '.agents', 'skills'))).rejects.toThrow();
  });

  it('overwrites codex_hooks = false when hooks are installed', async () => {
    await fs.writeFile(
      path.join(tempHome, '.codex', 'config.toml'),
      '[features]\ncodex_hooks = false\n',
      'utf-8',
    );

    const { setupCommand } = await import('../../src/cli/setup.js');

    await setupCommand();

    const codexConfig = await fs.readFile(path.join(tempHome, '.codex', 'config.toml'), 'utf-8');
    expect(codexConfig).toContain('codex_hooks = true');
    expect(codexConfig).not.toContain('codex_hooks = false');
  });

  it('updates codex_hooks in existing CRLF sections instead of duplicating features', async () => {
    await fs.writeFile(
      path.join(tempHome, '.codex', 'config.toml'),
      '[features]\r\ncodex_hooks = false\r\n',
      'utf-8',
    );

    const { setupCommand } = await import('../../src/cli/setup.js');

    await setupCommand();

    const codexConfig = await fs.readFile(path.join(tempHome, '.codex', 'config.toml'), 'utf-8');
    expect(codexConfig.match(/\[features\]/g)).toHaveLength(1);
    expect(codexConfig).toContain('codex_hooks = true');
    expect(codexConfig).not.toContain('codex_hooks = false');
  });

  it('updates indented codex_hooks keys inside an existing features section', async () => {
    await fs.writeFile(
      path.join(tempHome, '.codex', 'config.toml'),
      '[features]\n  codex_hooks = false\n',
      'utf-8',
    );

    const { setupCommand } = await import('../../src/cli/setup.js');

    await setupCommand();

    const codexConfig = await fs.readFile(path.join(tempHome, '.codex', 'config.toml'), 'utf-8');
    expect(codexConfig.match(/^(\s*)codex_hooks = true$/m)).not.toBeNull();
    expect(codexConfig).not.toContain('codex_hooks = false');
    expect(codexConfig.match(/codex_hooks = true/g)).toHaveLength(1);
  });

  it('updates commented features headers instead of appending a duplicate section', async () => {
    await fs.writeFile(
      path.join(tempHome, '.codex', 'config.toml'),
      '[features] # existing comment\ncodex_hooks = false\n',
      'utf-8',
    );

    const { setupCommand } = await import('../../src/cli/setup.js');

    await setupCommand();

    const codexConfig = await fs.readFile(path.join(tempHome, '.codex', 'config.toml'), 'utf-8');
    expect(codexConfig.match(/\[features\]/g)).toHaveLength(1);
    expect(codexConfig).toContain('codex_hooks = true');
    expect(codexConfig).not.toContain('codex_hooks = false');
  });

  it('repairs malformed hook buckets instead of throwing', async () => {
    await fs.writeFile(
      path.join(tempHome, '.codex', 'hooks.json'),
      JSON.stringify({ hooks: { PostToolUse: {} } }, null, 2),
      'utf-8',
    );

    const { setupCommand } = await import('../../src/cli/setup.js');

    await setupCommand();

    const hooksJson = JSON.parse(await fs.readFile(path.join(tempHome, '.codex', 'hooks.json'), 'utf-8'));
    expect(Array.isArray(hooksJson.hooks.PostToolUse)).toBe(true);
    expect(hooksJson.hooks.PostToolUse[0].matcher).toBe('Bash');
    expect(hooksJson.hooks.PostToolUse[0].hooks[0].timeout).toBe(20);
  });

  it('updates existing Codex hook entries in place when the command already exists', async () => {
    await fs.mkdir(path.join(tempHome, '.codex', 'hooks', 'gitnexus'), { recursive: true });
    await fs.writeFile(
      path.join(tempHome, '.codex', 'hooks', 'gitnexus', 'gitnexus-hook.cjs'),
      '#!/usr/bin/env node\n',
      'utf-8',
    );
    await fs.writeFile(
      path.join(tempHome, '.codex', 'hooks.json'),
      JSON.stringify(
        {
          hooks: {
            PostToolUse: [
              {
                matcher: 'Bash',
                hooks: [
                  {
                    type: 'command',
                    command: `node "${path.join(tempHome, '.codex', 'hooks', 'gitnexus', 'gitnexus-hook.cjs').replace(/\\/g, '/')}"`,
                    timeout: 10,
                    statusMessage: 'Old message',
                  },
                ],
              },
            ],
          },
        },
        null,
        2,
      ),
      'utf-8',
    );

    const { setupCommand } = await import('../../src/cli/setup.js');

    await setupCommand();

    const hooksJson = JSON.parse(await fs.readFile(path.join(tempHome, '.codex', 'hooks.json'), 'utf-8'));
    expect(hooksJson.hooks.PostToolUse).toHaveLength(1);
    expect(hooksJson.hooks.PostToolUse[0].hooks).toHaveLength(1);
    expect(hooksJson.hooks.PostToolUse[0].hooks[0].timeout).toBe(20);
    expect(hooksJson.hooks.PostToolUse[0].hooks[0].statusMessage).toBe(
      'Reviewing Bash output with GitNexus...',
    );
  });
});
