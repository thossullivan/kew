#!/usr/bin/env node
// Build-time check for the npm bundled-dependency overlay (#1432).
//
// The agent Dockerfiles replace four packages inside npm's vendored tree with
// patched versions, because npm ships them with published CVEs and cannot
// install into its own prefix. This asserts the replacement landed AND that
// each replaced package still works.
//
// It RUNS the code rather than modelling npm. An earlier version walked the
// dependency graph and compared semver ranges — a proxy for "npm still works"
// that had to be right about npm's resolver, and four review rounds found it
// wrong in four different ways. Calling the packages is both simpler and
// stronger evidence: a dependency that fails to resolve throws here.

const assert = require("assert");
const fs = require("fs");
const http = require("http");
const path = require("path");

const BUNDLE = process.argv[2] || "/usr/lib/node_modules/npm/node_modules";
const MIN_BUNDLE_ENTRIES = 50;
const EXPECTED = {
  "brace-expansion": "5.0.11",
  "ip-address": "10.3.1",
  tar: "7.5.21",
  undici: "6.29.1",
};

const load = (name) => require(path.join(BUNDLE, name));
const versionOf = (name) =>
  JSON.parse(fs.readFileSync(path.join(BUNDLE, name, "package.json"), "utf8")).version;

assert(
  fs
    .readdirSync(BUNDLE, { withFileTypes: true })
    .filter((entry) => {
      const manifest = path.join(BUNDLE, entry.name, "package.json");
      return entry.isDirectory() && fs.existsSync(manifest) && fs.statSync(manifest).isFile();
    }).length >= MIN_BUNDLE_ENTRIES,
  "the path does not look like npm's bundled node_modules"
);

for (const [name, want] of Object.entries(EXPECTED)) {
  const got = versionOf(name);
  assert.strictEqual(
    got,
    want,
    `${name}: bundle has ${got}, overlay intended ${want} — the copy did not land`
  );
  const prefix = path.join(BUNDLE, name) + path.sep;
  const entry = require.resolve(path.join(BUNDLE, name));
  assert(
    entry.startsWith(prefix) && !entry.slice(prefix.length).split(path.sep).includes("node_modules"),
    `${name}: package loads code outside its own package directory`
  );
  assert(
    !fs.existsSync(path.join(BUNDLE, name, "node_modules")),
    `${name}: package carries a nested node_modules, so a second copy of a dependency could shadow the patched one.`
  );
}

// brace-expansion: the glob path npm uses.
const { expand } = load("brace-expansion");
assert.strictEqual(typeof expand, "function", "brace-expansion has no named expand export");
assert.deepStrictEqual(expand("a{b,c}d"), ["abd", "acd"], "brace-expansion is broken");

// undici: exercise both registry request paths against a loopback server.
const ROUND_TRIP_BODY = "kew npm overlay round trip";

const listen = (server) =>
  new Promise((resolve, reject) => {
    const onError = (error) => reject(error);
    server.once("error", onError);
    server.listen(0, "127.0.0.1", () => {
      server.off("error", onError);
      resolve();
    });
  });

const closeServer = (server) => {
  if (!server.listening) return Promise.resolve();
  return new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
};

async function exerciseUndici() {
  const undici = load("undici");
  assert.strictEqual(typeof undici.request, "function", "undici request is broken");
  assert.strictEqual(typeof undici.fetch, "function", "undici fetch is broken");
  assert.strictEqual(typeof undici.Agent, "function", "undici Agent is broken");
  const agent = new undici.Agent();
  assert.strictEqual(typeof agent.close, "function", "undici Agent close is broken");
  const server = http.createServer((_request, response) => {
    response.writeHead(200, { "Content-Type": "text/plain" });
    response.end(ROUND_TRIP_BODY);
  });

  try {
    await listen(server);
    const address = server.address();
    assert(address && typeof address === "object", "loopback server has no address");
    const url = `http://127.0.0.1:${address.port}/overlay`;

    const requestResponse = await undici.request(url, { dispatcher: agent });
    assert.strictEqual(requestResponse.statusCode, 200, "undici request status is broken");
    assert.strictEqual(
      await requestResponse.body.text(),
      ROUND_TRIP_BODY,
      "undici request body is broken"
    );

    const fetchResponse = await undici.fetch(url, { dispatcher: agent });
    assert.strictEqual(fetchResponse.status, 200, "undici fetch status is broken");
    assert.strictEqual(
      await fetchResponse.text(),
      ROUND_TRIP_BODY,
      "undici fetch body is broken"
    );
  } finally {
    let cleanupError;
    try {
      const closing = agent.close();
      assert(closing && typeof closing.then === "function", "undici Agent close is broken");
      await closing;
    } catch (error) {
      cleanupError = error;
    }
    try {
      await closeServer(server);
    } catch (error) {
      cleanupError ||= error;
    }
    if (cleanupError) throw cleanupError;
  }
}

// tar: create and read back, which is what `npm pack` and `npm install` do.
const tar = load("tar");
const work = fs.mkdtempSync("/tmp/overlay-");
fs.writeFileSync(path.join(work, "f.txt"), "hi");
tar.c({ sync: true, file: path.join(work, "a.tgz"), cwd: work }, ["f.txt"]);
const entries = [];
tar.t({ sync: true, file: path.join(work, "a.tgz"), onentry: (e) => entries.push(e.path) });
assert.deepStrictEqual(entries, ["f.txt"], "tar is broken");
const extracted = path.join(work, "extracted");
fs.mkdirSync(extracted);
tar.x({ sync: true, file: path.join(work, "a.tgz"), cwd: extracted });
const extractedFile = path.join(extracted, "f.txt");
assert(
  fs.existsSync(extractedFile) && fs.readFileSync(extractedFile, "utf8") === "hi",
  "tar extraction is broken"
);
fs.rmSync(work, { recursive: true, force: true });

// ip-address: the proxy/agent path npm uses to reach a registry.
const { Address4 } = load("ip-address");
assert.strictEqual(new Address4("192.168.1.1").correctForm(), "192.168.1.1", "ip-address is broken");

exerciseUndici().then(
  () => {
    console.log(
      `npm overlay verified: ${Object.keys(EXPECTED).length} replaced packages at their ` +
        "intended versions, each exercised"
    );
  },
  (error) => {
    console.error(error);
    process.exitCode = 1;
  }
);
