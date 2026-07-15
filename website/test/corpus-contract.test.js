const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { assertCorpusContract } = require("../lib/corpus");

function scratch() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "rwbc-corpus-contract-"));
}

test("accepts the supported corpus version", () => {
  const root = scratch();
  fs.writeFileSync(path.join(root, "manifest.json"), JSON.stringify({ schemaVersion: 1 }));
  assert.deepEqual(assertCorpusContract(root), { schemaVersion: 1 });
});

test("rejects missing and unsupported manifests", () => {
  const missing = scratch();
  assert.throws(() => assertCorpusContract(missing), /manifest missing/);

  const future = scratch();
  fs.writeFileSync(path.join(future, "manifest.json"), JSON.stringify({ schemaVersion: 2 }));
  assert.throws(() => assertCorpusContract(future), /Unsupported corpus schema version 2/);
});
