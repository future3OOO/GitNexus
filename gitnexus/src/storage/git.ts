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
 * The first tracked path the checkout's attributes assign a `filter` driver to, or `''`.
 * A clean filter rewrites content between the working file and the stored blob, so a tree
 * captured from such a checkout describes bytes no reader ever saw.
 */
const filteredPath = (repoPath: string): string => {
  const run = (args: string[], input?: string): string =>
    execFileSync('git', args, {
      cwd: repoPath,
      input,
      encoding: 'utf-8',
      maxBuffer: 64 * 1024 * 1024,
      stdio: ['pipe', 'pipe', 'pipe'],
    });
  // Every path the capture stores, so an untracked source file cannot carry a filter past
  // this check: `git add -A` stores it exactly as it stores a tracked one.
  const paths = run(['ls-files', '-z']) + run(['ls-files', '-z', '--others', '--exclude-standard']);
  if (!paths) return '';
  // check-attr reports "<path>\0filter\0<value>\0" per path, and the value is whatever
  // .gitattributes declared: a driver name, `-` for filter=-, `unset` for -filter, or
  // `unspecified` for no attribute at all. Only a driver git holds a clean command for
  // rewrites anything, so asking git that directly is what separates a real driver from a
  // bare declaration and from a driver whose name happens to be one of those words.
  const reported = run(['check-attr', '--stdin', '-z', 'filter'], paths).split('\0');
  const rewrites = new Map<string, boolean>();
  for (let index = 0; index + 2 < reported.length; index += 3) {
    const value = reported[index + 2];
    if (!value) continue;
    let configured = rewrites.get(value);
    if (configured === undefined) {
      try {
        configured = run(['config', '--get', `filter.${value}.clean`]).trim() !== '';
      } catch {
        configured = false; // git exits non-zero when the key is absent
      }
      rewrites.set(value, configured);
    }
    if (configured) return reported[index];
  }
  return '';
};

/**
 * The tree id of a checkout's working tree — tracked and untracked files, with ignored
 * files and the `.gitnexus/` index directory excluded — written into its object store
 * through a throwaway index, so the checkout's own index and staging state are untouched.
 *
 * The tree is only useful as a line-coordinate baseline while its blobs hold the bytes the
 * files hold, so a checkout configured with a content-rewriting clean filter is refused
 * rather than captured: the caller records no snapshot and later reads refuse for missing
 * metadata, instead of mapping hunks onto lines that were never indexed. Throws when git
 * cannot capture the tree or when a filter makes the capture untrustworthy.
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
    const filtered = filteredPath(repoPath);
    if (filtered) {
      throw new Error(
        `a clean filter rewrites ${filtered} on the way into the object store, so the captured tree would not hold the bytes this checkout holds`,
      );
    }
    git('read-tree', hasHead ? 'HEAD' : '--empty');
    // The checkout's own ignore rules decide what is content, including the user's global
    // excludes file, so nothing they ignore is captured, and overriding core.excludesFile
    // would capture whatever the user ignores only there. An unignored index directory is
    // excluded up front rather than dropped from the tree afterwards: `git add` writes its
    // blobs before any later removal can help, measured at 252 KiB of unreachable objects
    // per capture on a 16 MB database. `git add` refuses a pathspec naming an
    // already-ignored path, so the pathspec is passed only when the rule is absent.
    let ignoresIndex = true;
    try {
      execFileSync('git', ['check-ignore', '-q', '--', '.gitnexus'], { cwd: repoPath, stdio: 'ignore' });
    } catch {
      ignoresIndex = false;
    }
    git('add', '-A', '--', '.', ...(ignoresIndex ? [] : [':(exclude).gitnexus']));
    // The pathspec stops `add` from writing the directory's blobs, but `read-tree HEAD`
    // has already seeded any copy committed to HEAD, so the entry is dropped as well.
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
