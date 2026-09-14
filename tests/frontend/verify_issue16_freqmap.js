// Regression test for issue #16: Monitor page logs a 404 for /static/freq_map.json.
//
// The frequency-overlay file never existed in this fork OR upstream, so the
// unconditional fetch('/static/freq_map.json') always 404s. A try/catch hides the
// JS error but browsers still log the 404 to the console/network panel — the only
// way to stop that is to not issue the request. frequencyMap defaults to {} and
// getFrequencyInfo() already degrades to {band:'Unknown', freq:'Unknown'} when the
// map is empty, so removing the fetch changes no user-visible behaviour.
//
// HOW TO RUN (Node stdlib only; no deps):
//     node tests/frontend/verify_issue16_freqmap.js
// Exit 0 = PASS, exit 1 = FAIL.
//
// Checks:
//   1. STATIC: app.js issues no unconditional fetch('/static/freq_map.json').
//   2. BEHAVIOUR: getFrequencyInfo() returns the graceful Unknown fallback when
//      frequencyMap is empty (extracted and executed in a vm sandbox).

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const REPO = path.resolve(__dirname, "..", "..");
const appJs = fs.readFileSync(path.join(REPO, "static", "app.js"), "utf8");

let failures = [];

// --- Check 1: no unconditional freq_map.json fetch remains ---
// Match a fetch(...) whose argument literally references the missing file.
const badFetch = /fetch\(\s*['"`][^'"`]*freq_map\.json[^'"`]*['"`]\s*\)/.test(appJs);
if (badFetch) {
  failures.push("app.js still calls fetch('/static/freq_map.json') — the 404 will still be logged");
}

// --- Check 2: getFrequencyInfo degrades gracefully with an empty frequency map ---
// Extract the getFrequencyInfo function body and its constellationMap dependency,
// then exercise it in an isolated sandbox with frequencyMap = {}.
function extractFn(src, name) {
  const start = src.indexOf(`function ${name}`);
  if (start === -1) throw new Error(`${name} not found`);
  let depth = 0, started = false, end = start;
  for (let i = start; i < src.length; i++) {
    if (src[i] === "{") { depth++; started = true; }
    else if (src[i] === "}") { depth--; }
    if (started && depth === 0) { end = i + 1; break; }
  }
  return src.slice(start, end);
}

let getFreq;
try {
  getFreq = extractFn(appJs, "getFrequencyInfo");
} catch (e) {
  failures.push("could not locate getFrequencyInfo(): " + e.message);
}

if (getFreq) {
  const sandbox = { frequencyMap: {}, result: null, resultUnknownChannel: null };
  vm.createContext(sandbox);
  try {
    vm.runInContext(
      getFreq +
        "\n; result = getFrequencyInfo('GPS', '01');" +
        "\n; resultUnknownChannel = getFrequencyInfo('GPS', null);",
      sandbox
    );
    if (!sandbox.result || sandbox.result.band !== "Unknown" || sandbox.result.freq !== "Unknown") {
      failures.push("getFrequencyInfo with empty map did not return {band:'Unknown', freq:'Unknown'}: " + JSON.stringify(sandbox.result));
    }
    if (!sandbox.resultUnknownChannel || sandbox.resultUnknownChannel.band !== "Unknown") {
      failures.push("getFrequencyInfo with null channel did not degrade to Unknown: " + JSON.stringify(sandbox.resultUnknownChannel));
    }
  } catch (e) {
    failures.push("getFrequencyInfo threw when map is empty (should degrade gracefully): " + e.message);
  }
}

if (failures.length) {
  console.log("RESULT: FAIL");
  for (const f of failures) console.log("  - " + f);
  process.exit(1);
}
console.log("RESULT: PASS");
console.log("  no freq_map.json fetch; getFrequencyInfo degrades to Unknown with empty map");
process.exit(0);
