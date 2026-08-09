import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { lbugMocks, repoMocks, gitMocks, stalenessMocks } = vi.hoisted(() => ({
  lbugMocks: {
    initLbug: vi.fn().mockResolvedValue(undefined),
    executeQuery: vi.fn().mockResolvedValue([]),
    executeParameterized: vi.fn().mockResolvedValue([]),
    closeLbug: vi.fn().mockResolvedValue(undefined),
    isLbugReady: vi.fn().mockReturnValue(true),
  },
  repoMocks: {
    listRegisteredRepos: vi.fn(),
    cleanupOldKuzuFiles: vi.fn().mockResolvedValue({ found: false, needsReindex: false }),
  },
  gitMocks: {
    isGitRepo: vi.fn().mockReturnValue(true),
  },
  stalenessMocks: {
    checkStaleness: vi.fn().mockReturnValue({ isStale: false, commitsBehind: 0 }),
  },
}));

vi.mock('../../src/core/lbug/pool-adapter.js', async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, ...lbugMocks };
});

vi.mock('../../src/mcp/core/lbug-adapter.js', async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, ...lbugMocks };
});

vi.mock('../../src/storage/repo-manager.js', () => repoMocks);
vi.mock('../../src/storage/git.js', () => gitMocks);
vi.mock('../../src/core/git-staleness.js', () => stalenessMocks);
vi.mock('../../src/core/search/bm25-index.js', () => ({
  searchFTSFromLbug: vi.fn().mockResolvedValue([]),
}));
vi.mock('../../src/mcp/core/embedder.js', () => ({
  embedQuery: vi.fn().mockResolvedValue([]),
  getEmbeddingDims: vi.fn().mockReturnValue(384),
}));

import { LocalBackend } from '../../src/mcp/local/local-backend.js';

const MOCK_REPO_ENTRY = {
  name: 'test-project',
  path: '/tmp/test-project',
  storagePath: '/tmp/.gitnexus/test-project',
  indexedAt: '2024-06-01T12:00:00Z',
  lastCommit: 'abc1234567890',
  stats: { files: 10, nodes: 50, edges: 100, communities: 3, processes: 5 },
};

describe('LocalBackend staleness throttle', () => {
  let dateNow: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.clearAllMocks();
    repoMocks.listRegisteredRepos.mockResolvedValue([MOCK_REPO_ENTRY]);
    stalenessMocks.checkStaleness.mockReturnValue({ isStale: false, commitsBehind: 0 });
    dateNow = vi.spyOn(Date, 'now').mockReturnValue(1000);
  });

  afterEach(() => {
    dateNow.mockRestore();
  });

  it('does not run synchronous git staleness checks on every initialized graph call', async () => {
    const backend = new LocalBackend();
    await backend.init();

    await backend.callTool('query', { query: 'auth' });
    expect(stalenessMocks.checkStaleness).toHaveBeenCalledTimes(1);

    await backend.callTool('query', { query: 'auth' });
    expect(stalenessMocks.checkStaleness).toHaveBeenCalledTimes(1);

    dateNow.mockReturnValue(7001);
    await backend.callTool('query', { query: 'auth' });
    expect(stalenessMocks.checkStaleness).toHaveBeenCalledTimes(2);
  });
});
