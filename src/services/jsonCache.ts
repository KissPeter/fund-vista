/**
 * Tolerant localStorage JSON cache reads (issue #96).
 *
 * A truncated or hand-edited cache entry makes `JSON.parse` throw a
 * `SyntaxError`. When that happens inside an async loader the rejection
 * escapes the loader, the loading flag is never reset and the dashboard
 * stays stuck on "loading ..." forever - a purely local corruption
 * bricks the page until localStorage is cleared by hand.
 *
 * `readJsonCache` therefore treats an unparsable (or wrongly shaped)
 * entry as a cache miss and drops it, so the next read starts clean.
 * The same pattern the FX cache already uses
 * (`src/services/exchangeRateApi.ts`).
 */

/**
 * Drop a cache entry. Best effort: a failing removal (storage disabled,
 * a full quota) must never turn a cache miss into a throw (#96).
 */
export const removeCacheEntry = (key: string): void => {
  try {
    localStorage.removeItem(key);
  } catch {
    // The entry stays; the next read treats it as a miss again.
  }
};

/**
 * Read a JSON cache entry.
 *
 * @param key - localStorage key.
 * @param isValid - shape guard; a value that fails it is dropped too.
 * @returns the parsed entry, or `null` when the key is absent, corrupt
 * or malformed. A corrupt entry is removed from localStorage.
 */
export const readJsonCache = <T>(
  key: string,
  isValid: (value: unknown) => value is T,
): T | null => {
  let raw: string | null = null;
  try {
    raw = localStorage.getItem(key);
  } catch {
    // Storage can be unavailable (private mode, disabled cookies).
    return null;
  }
  if (raw === null) return null;

  try {
    const parsed: unknown = JSON.parse(raw);
    if (!isValid(parsed)) {
      removeCacheEntry(key);
      return null;
    }
    return parsed;
  } catch {
    // Corrupt entry: drop it, it can never be read again.
    removeCacheEntry(key);
    return null;
  }
};

/**
 * Write a JSON cache entry. Best effort: the cache is an optimisation,
 * so a failing write (a full quota) must not fail the caller - the data
 * it was about to cache has already been fetched (#96).
 *
 * @returns whether the entry was stored.
 */
export const writeJsonCache = (key: string, value: unknown): boolean => {
  try {
    localStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
};
