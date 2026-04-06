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
});
