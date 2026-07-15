const fs = require("fs");
const path = require("path");

const SUPPORTED_SCHEMA_VERSION = 1;
const DEFAULT_DATA_DIR = path.resolve(__dirname, "..", "..", "corpus", "data");

function dataDir() {
  return path.resolve(process.env.OLIVER_CORPUS_DIR || DEFAULT_DATA_DIR);
}

function assertCorpusContract(root = dataDir()) {
  const manifestPath = path.join(root, "manifest.json");
  if (!fs.existsSync(manifestPath)) {
    throw new Error(`Corpus manifest missing at ${manifestPath}; regenerate with python -m agent.corpus_gen`);
  }
  let manifest;
  try {
    manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  } catch (error) {
    throw new Error(`Corpus manifest is not valid JSON: ${error.message}`);
  }
  if (manifest.schemaVersion !== SUPPORTED_SCHEMA_VERSION) {
    throw new Error(
      `Unsupported corpus schema version ${manifest.schemaVersion}; website supports ${SUPPORTED_SCHEMA_VERSION}`
    );
  }
  return manifest;
}

module.exports = { SUPPORTED_SCHEMA_VERSION, assertCorpusContract, dataDir };
