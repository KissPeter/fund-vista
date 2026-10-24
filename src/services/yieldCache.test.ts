/**
 * Regression tests for the yield loader (issue #96).
 *
 * A truncated or hand-edited localStorage entry used to make `JSON.parse`
 * throw inside `loadYieldData`; the rejection escaped the async loader,
 * the loading flag was never reset and the dashboard stayed stuck on
 * "loading yields" forever. The loader must treat such an entry as a
 * cache miss, drop it, and always clear the loading flag.
 *
 * Run: `npm test` (node's built-in test runner, no extra dependency).
 */
import test from "node:test";
import assert from "node:assert/strict";

import { loadYields, yieldCacheKey, yieldFailCacheKey } from "./yieldCache.ts";

/** Minimal in-memory localStorage double. */
class FakeStorage {
  private readonly data = new Map<string, string>();

  getItem(key: string): string | null {
    return this.data.has(key) ? (this.data.get(key) as string) : null;
  }

  setItem(key: string, value: string): void {
    this.data.set(key, value);
  }

  removeItem(key: string): void {
    this.data.delete(key);
  }

  has(key: string): boolean {
    return this.data.has(key);
  }
}

/** What the loader reported back, for the assertions. */
interface Recorded {
  loading: boolean[];
  progress: number[];
  loaded: { yields: Record<number, string>; range: number } | null;
}

function setup(storage: FakeStorage): Recorded {
  (globalThis as unknown as { localStorage: FakeStorage }).localStorage = storage;
  return { loading: [], progress: [], loaded: null };
}

const FUNDS = [{ primaryKey: 1 }, { primaryKey: 2 }];
const RANGE = 12;

test("a corrupt cache entry does not throw and the load completes", async () => {
  const storage = new FakeStorage();
  const recorded = setup(storage);
  storage.setItem(yieldCacheKey(1, RANGE), '{"data": "1.23%", "timesta');

  await loadYields(FUNDS, FUNDS.length, RANGE, {
    fetchYield: async (primaryKey) => `${primaryKey}.00%`,
    setLoading: (loading) => recorded.loading.push(loading),
    setProgress: (percent) => recorded.progress.push(percent),
    onLoaded: (yields, range) => { recorded.loaded = { yields, range }; },
    sleep: async () => undefined,
  });

  assert.deepEqual(recorded.loaded?.yields, { 1: "1.00%", 2: "2.00%" });
  // The corrupt entry is dropped and replaced by the fresh, valid one.
  const stored = storage.getItem(yieldCacheKey(1, RANGE));
  assert.ok(stored !== null, "the fetched yield is cached again");
  const entry = JSON.parse(stored as string);
  assert.equal(entry.data, "1.00%");
  assert.equal(typeof entry.timestamp, "number");
});

test("the loading flag is cleared even when the load throws", async () => {
  const storage = new FakeStorage();
  const recorded = setup(storage);

  await assert.rejects(
    loadYields(FUNDS, FUNDS.length, RANGE, {
      // The progress callback throws - an unexpected failure outside the
      // per-fund try/catch, i.e. the whole load gives up.
      fetchYield: async (primaryKey) => `${primaryKey}.00%`,
      setLoading: (loading) => recorded.loading.push(loading),
      setProgress: () => { throw new Error("boom"); },
      onLoaded: () => { recorded.loaded = { yields: {}, range: RANGE }; },
      sleep: async () => undefined,
    }),
    /boom/,
  );

  assert.deepEqual(recorded.loading, [true, false], "the flag is cleared on the error path");
});

test("a full localStorage does not lose the fetched yields", async () => {
  const storage = new FakeStorage();
  const recorded = setup(storage);
  // The cache write fails (full quota), but the yield was fetched fine.
  storage.setItem = () => { throw new Error("QuotaExceededError"); };

  await loadYields(FUNDS, FUNDS.length, RANGE, {
    fetchYield: async (primaryKey) => `${primaryKey}.00%`,
    setLoading: (loading) => recorded.loading.push(loading),
    setProgress: (percent) => recorded.progress.push(percent),
    onLoaded: (yields, range) => { recorded.loaded = { yields, range }; },
    sleep: async () => undefined,
  });

  assert.deepEqual(
    recorded.loaded?.yields,
    { 1: "1.00%", 2: "2.00%" },
    "the yields survive a failing cache write",
  );
  assert.deepEqual(recorded.loading, [true, false]);
});

test("the loading flag is set and cleared on the happy path", async () => {
  const storage = new FakeStorage();
  const recorded = setup(storage);

  await loadYields(FUNDS, FUNDS.length, RANGE, {
    fetchYield: async (primaryKey) => `${primaryKey}.00%`,
    setLoading: (loading) => recorded.loading.push(loading),
    setProgress: (percent) => recorded.progress.push(percent),
    onLoaded: (yields, range) => { recorded.loaded = { yields, range }; },
    sleep: async () => undefined,
  });

  assert.deepEqual(recorded.loading, [true, false]);
  assert.equal(recorded.loaded?.range, RANGE);
});

test("a fresh cached yield is used without a fetch", async () => {
  const storage = new FakeStorage();
  const recorded = setup(storage);
  const now = Date.now();
  storage.setItem(yieldCacheKey(1, RANGE), JSON.stringify({ data: "9.99%", timestamp: now }));

  let fetched = 0;
  await loadYields([FUNDS[0]], 1, RANGE, {
    fetchYield: async (primaryKey) => {
      fetched += 1;
      return `${primaryKey}.00%`;
    },
    setLoading: (loading) => recorded.loading.push(loading),
    setProgress: (percent) => recorded.progress.push(percent),
    onLoaded: (yields, range) => { recorded.loaded = { yields, range }; },
    now: () => now,
    sleep: async () => undefined,
  });

  assert.equal(fetched, 0, "the cached value is used");
  assert.deepEqual(recorded.loaded?.yields, { 1: "9.99%" });
});

test("a corrupt failure entry is a miss, not a skip", async () => {
  const storage = new FakeStorage();
  const recorded = setup(storage);
  storage.setItem(yieldFailCacheKey(1, RANGE), "{oops");

  await loadYields([FUNDS[0]], 1, RANGE, {
    fetchYield: async (primaryKey) => `${primaryKey}.00%`,
    setLoading: (loading) => recorded.loading.push(loading),
    setProgress: (percent) => recorded.progress.push(percent),
    onLoaded: (yields, range) => { recorded.loaded = { yields, range }; },
    sleep: async () => undefined,
  });

  assert.equal(storage.has(yieldFailCacheKey(1, RANGE)), false, "the corrupt entry is dropped");
  assert.deepEqual(recorded.loaded?.yields, { 1: "1.00%" }, "the yield is fetched, not skipped");
});
