/**
 * A repo keeps its identity across registry refreshes.
 *
 * repoId() detects a name collision by comparing the stored path of the existing handle with
 * path.resolve() of the incoming entry. On a second refresh the same entry compares against
 * itself, so any registry path that path.resolve() normalises (a trailing slash here, every
 * path on Windows) reads as a collision with itself: the repo is re-keyed with a hash and the
 * prune step evicts the original key. impactByUid refreshes before it initialises, so it is the
 * caller that trips over the eviction with "Unknown repo".
 */
import { describe, it, expect, beforeAll, vi } from 'vitest';
import { LocalBackend } from '../../src/mcp/local/local-backend.js';
import { listRegisteredRepos } from '../../src/storage/repo-manager.js';
import { withTestLbugDB } from '../helpers/test-indexed-db.js';
import {
  LOCAL_BACKEND_SEED_DATA,
  LOCAL_BACKEND_FTS_INDEXES,
} from '../fixtures/local-backend-seed.js';

vi.mock('../../src/storage/repo-manager.js', () => ({
  listRegisteredRepos: vi.fn().mockResolvedValue([]),
  cleanupOldKuzuFiles: vi.fn().mockResolvedValue({ found: false, needsReindex: false }),
}));

withTestLbugDB(
  'local-backend-repo-identity',
  (handle) => {
    describe('repo identity across registry refreshes', () => {
      let backend: LocalBackend;

      beforeAll(async () => {
        backend = (handle as any)._backend;
      });

      it('UID impact still finds a repo whose registry path is not in resolved form', async () => {
        const marker = 'REPO_EVICTED_ON_REFRESH';
        const context = await backend.callTool('context', { repo: 'test-repo', name: 'login' });
        expect(context.status).toBe('found');

        const result = await backend.callTool('impact', {
          repo: 'test-repo',
          uid: context.symbol.uid,
          direction: 'upstream',
        });

        expect(result.error, `${marker}: ${JSON.stringify(result.error)}`).toBeUndefined();
        // The failure fallback echoes the uid back as target.id; only a resolved target carries its file.
        expect(result.target.filePath, marker).toBe('src/auth.ts');
      });

      it.skipIf(process.platform !== 'win32')(
        'a re-registration that only changes path casing keeps the repo id on Windows',
        async () => {
          const marker = 'REPO_EVICTED_ON_CASE_CHANGE';
          const context = await backend.callTool('context', { repo: 'test-repo', name: 'login' });
          expect(context.status).toBe('found');
          const [entry] = await listRegisteredRepos();
          vi.mocked(listRegisteredRepos).mockResolvedValue([{ ...entry, path: '/TEST/REPO/' }]);

          const result = await backend.callTool('impact', {
            repo: 'test-repo',
            uid: context.symbol.uid,
            direction: 'upstream',
          });

          expect(result.error, `${marker}: ${JSON.stringify(result.error)}`).toBeUndefined();
          expect(result.target.filePath, marker).toBe('src/auth.ts');
        },
      );
    });
  },
  {
    seed: LOCAL_BACKEND_SEED_DATA,
    ftsIndexes: LOCAL_BACKEND_FTS_INDEXES,
    poolAdapter: true,
    afterSetup: async (handle) => {
      vi.mocked(listRegisteredRepos).mockResolvedValue([
        {
          // Trailing slash: path.resolve() strips it, so the stored and resolved forms differ.
          name: 'test-repo',
          path: '/test/repo/',
          storagePath: handle.tmpHandle.dbPath,
          indexedAt: new Date().toISOString(),
          lastCommit: 'abc123',
          stats: { files: 2, nodes: 3, communities: 1, processes: 1 },
        },
      ]);

      const backend = new LocalBackend();
      await backend.init();
      (handle as any)._backend = backend;
    },
  },
);
