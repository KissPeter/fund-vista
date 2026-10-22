/**
 * Regression tests for the tolerant localStorage cache reader (issue #96).
 *
 * A truncated or hand-edited cache entry used to make `JSON.parse` throw
 * inside `loadYieldData`; the rejection escaped the async loader, the
 * loading flag was never reset and the dashboard stayed stuck on
 * "loading yields" forever. The reader must treat such an entry as a
 * cache miss and drop it.
 *
 * Run: `npm test` (node's built-in test runner, no extra dependency).
 */
import test from "node:test";
import assert from "node:assert/strict";

import { readJsonCache, removeCacheEntry, writeJsonCache } from "./jsonCache.ts";

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

const isStringEntry = (value: unknown): value is { data: string; timestamp: number } =>
  typeof value === "object" &&
  value !== null &&
  typeof (value as { data?: unknown }).data === "string" &&
  typeof (value as { timestamp?: unknown }).timestamp === "number";

/** Install the storage double for one test. */
function withStorage(storage: FakeStorage): void {
  (globalThis as unknown as { localStorage: FakeStorage }).localStorage = storage;
}

test("a valid entry is returned", () => {
  const storage = new FakeStorage();
  withStorage(storage);
  storage.setItem("k", JSON.stringify({ data: "1.23%", timestamp: 42 }));

  const entry = readJsonCache("k", isStringEntry);

  assert.deepEqual(entry, { data: "1.23%", timestamp: 42 });
  assert.ok(storage.has("k"), "a valid entry is kept");
});

test("a missing key is a miss and nothing is removed", () => {
  const storage = new FakeStorage();
  withStorage(storage);

  assert.equal(readJsonCache("absent", isStringEntry), null);
  assert.equal(storage.has("absent"), false);
});

test("a corrupt entry is a miss and is removed", () => {
  const storage = new FakeStorage();
  withStorage(storage);
  storage.setItem("k", '{"data": "1.23%", "timesta'); // truncated

  assert.doesNotThrow(() => readJsonCache("k", isStringEntry));
  assert.equal(readJsonCache("k", isStringEntry), null);
  assert.equal(storage.has("k"), false, "the corrupt entry is dropped");
});

test("an entry of the wrong shape is a miss and is removed", () => {
  const storage = new FakeStorage();
  withStorage(storage);
  storage.setItem("k", JSON.stringify({ data: 123, timestamp: "nope" }));

  assert.equal(readJsonCache("k", isStringEntry), null);
  assert.equal(storage.has("k"), false, "the malformed entry is dropped");
});

test("a non-object JSON value is a miss and is removed", () => {
  const storage = new FakeStorage();
  withStorage(storage);
  storage.setItem("k", "42");

  assert.equal(readJsonCache("k", isStringEntry), null);
  assert.equal(storage.has("k"), false);
});

test("an unavailable storage is a miss, not a throw", () => {
  (globalThis as unknown as { localStorage: unknown }).localStorage = {
    getItem() {
      throw new Error("storage disabled");
    },
    removeItem() {
      throw new Error("storage disabled");
    },
  };

  assert.doesNotThrow(() => readJsonCache("k", isStringEntry));
  assert.equal(readJsonCache("k", isStringEntry), null);
});

test("a failing removal does not turn a corrupt entry into a throw", () => {
  const storage = new FakeStorage();
  withStorage(storage);
  storage.setItem("k", "{broken");
  storage.removeItem = () => { throw new Error("QuotaExceededError"); };

  assert.doesNotThrow(() => readJsonCache("k", isStringEntry));
  assert.equal(readJsonCache("k", isStringEntry), null, "still a miss");
});

test("a failing write is reported, not thrown", () => {
  const storage = new FakeStorage();
  withStorage(storage);
  storage.setItem = () => { throw new Error("QuotaExceededError"); };

  assert.doesNotThrow(() => writeJsonCache("k", { data: "1.23%" }));
  assert.equal(writeJsonCache("k", { data: "1.23%" }), false, "the write failed");
});

test("a successful write stores the entry", () => {
  const storage = new FakeStorage();
  withStorage(storage);

  assert.equal(writeJsonCache("k", { data: "1.23%" }), true);
  assert.deepEqual(JSON.parse(storage.getItem("k") as string), { data: "1.23%" });
});

test("a failing removal is swallowed", () => {
  const storage = new FakeStorage();
  withStorage(storage);
  storage.removeItem = () => { throw new Error("QuotaExceededError"); };

  assert.doesNotThrow(() => removeCacheEntry("k"));
});
