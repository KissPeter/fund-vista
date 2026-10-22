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
      localStorage.removeItem(key);
      return null;
    }
    return parsed;
  } catch {
    // Corrupt entry: drop it, it can never be read again.
    localStorage.removeItem(key);
    return null;
  }
};
