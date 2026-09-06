import { execFileSync, execSync } from 'child_process';
import { mkdtempSync, rmSync, statSync } from 'fs';
import { tmpdir } from 'os';
import path from 'path';

// Git utilities for repository detection, commit tracking, and diff analysis

export const isGitRepo = (repoPath: string): boolean => {
  try {
    execSync('git rev-parse --is-inside-work-tree', { cwd: repoPath, stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
};

export const getCurrentCommit = (repoPath: string): string => {
  try {
    return execSync('git rev-parse HEAD', { cwd: repoPath }).toString().trim();
  } catch {
    return '';
  }
};

/**
 * The tree id of a checkout's working tree — tracked and untracked files, with ignored
 * files and the `.gitnexus/` index directory excluded — written into its object store
 * through a throwaway index, so the checkout's own index and staging state are untouched.
 * Throws when git cannot capture it.
 */
export const writeWorkingTree = (repoPath: string): string => {
  const scratch = mkdtempSync(path.join(tmpdir(), 'gitnexus-tree-'));
  const env = { ...process.env, GIT_INDEX_FILE: path.join(scratch, 'index') };
  const git = (...args: string[]): string =>
    execFileSync('git', args, {
      cwd: repoPath,
      env,
      encoding: 'utf-8',
      stdio: ['ignore', 'pipe', 'pipe'],
    }).trim();
  try {
    let hasHead = true;
    try {
      execFileSync('git', ['rev-parse', '--verify', '-q', 'HEAD^{commit}'], { cwd: repoPath, stdio: 'ignore' });
    } catch {
      hasHead = false;
    }
    git('read-tree', hasHead ? 'HEAD' : '--empty');
    // The checkout's own ignore rules decide what is content, including the user's global
    // excludes file, so nothing they ignore is captured. The index directory is dropped
    // afterwards rather than through a pathspec or a replacement excludes file: `git add`
    // refuses a pathspec naming an already-ignored path, and overriding core.excludesFile
    // would capture whatever the user ignores only there.
    git('add', '-A', '--', '.');
    git('rm', '--cached', '-r', '-q', '--ignore-unmatch', '--', '.gitnexus');
    return git('write-tree');
  } finally {
    rmSync(scratch, { recursive: true, force: true });
  }
};

/**
 * Find the git repository root from any path inside the repo
 */
export const getGitRoot = (fromPath: string): string | null => {
  try {
    const raw = execSync('git rev-parse --show-toplevel', { cwd: fromPath }).toString().trim();
    // On Windows, git returns /d/Projects/Foo — path.resolve normalizes to D:\Projects\Foo
    return path.resolve(raw);
  } catch {
    return null;
  }
};
/**
 * Check whether a directory contains a .git entry (file or folder).
 *
 * This is intentionally a simple filesystem check rather than running
 * `git rev-parse`, so it works even when git is not installed or when
 * the directory is a git-worktree root (which has a .git file, not a
 * directory).  Use `isGitRepo` for a definitive git answer.
 *
 * @param dirPath - Absolute path to the directory to inspect.
 * @returns `true` when `.git` is present, `false` otherwise.
 */
export const hasGitDir = (dirPath: string): boolean => {
  try {
    statSync(path.join(dirPath, '.git'));
    return true;
  } catch {
    return false;
  }
};
