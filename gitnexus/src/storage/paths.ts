import path from 'path';

/**
 * Path equality as the registry defines it: resolved, and case-insensitive on Windows.
 * Shared by the registry and the local backend's repo identity so the rule has one owner.
 */
export const samePath = (a: string, b: string): boolean => {
  const [x, y] = [path.resolve(a), path.resolve(b)];
  return process.platform === 'win32' ? x.toLowerCase() === y.toLowerCase() : x === y;
};
