// Regression test for issue #15: Base Station Location map has zero height on the
// Monitor page because the map CSS was defined inside the Dashboard page's template
// <style>, which SPA navigation (contentDiv.innerHTML = getMonitorContent()) removes.
//
// HOW TO RUN (requires jsdom; this repo's Python suite does not cover frontend):
//     npm init -y && npm install jsdom
//     node tests/frontend/verify_issue15_map_css.js
// Exit 0 = PASS, exit 1 = FAIL. Reproduces the REAL navigation teardown:
//   1. Load templates/spa.html as the document (its <head><style> is persistent).
//   2. Inject the getMonitorContent() map fragment as the swapped-in page content,
//      exactly as performNavigation() does.
//   3. Assert the #map target resolves to a non-zero height via getComputedStyle.
//
// Fails on the pre-fix code (map CSS lived only in the dashboard template, absent
// after the swap) and passes once the rules live in the persistent spa.html <head>.

const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const REPO = path.resolve(__dirname, "..", "..");
const spaHtml = fs.readFileSync(path.join(REPO, "templates", "spa.html"), "utf8");
const appJs = fs.readFileSync(path.join(REPO, "static", "app.js"), "utf8");

// --- Extract the map HTML fragment from getMonitorContent() (the "Base Station
// Location" card), so we inject the same DOM the real Monitor page renders. ---
function extractMapFragment(src) {
  const anchor = src.indexOf('id="map-container"');
  if (anchor === -1) throw new Error("map-container not found in app.js");
  // Walk outward to the enclosing .map-content card-content div start.
  const start = src.lastIndexOf('<div class="card-content map-content"', anchor);
  if (start === -1) throw new Error("map-content wrapper not found");
  // Take a generous slice and close the two divs we opened; jsdom will tidy nesting.
  const slice = src.slice(start, anchor + 400);
  return slice.split("`")[0]; // stop at end of template literal if present
}

const mapFragment = extractMapFragment(appJs);

const dom = new JSDOM(spaHtml, { runScripts: "outside-only", pretendToBeVisual: true });
const { document } = dom.window;

// Simulate navigation to the Monitor page: replace the content container's markup.
// spa.html renders into a content div; create one and inject the monitor fragment.
const content = document.createElement("div");
content.innerHTML = mapFragment;
document.body.appendChild(content);

const mapContainer = document.getElementById("map-container");
const mapDisplay = document.getElementById("map");

let failures = [];
if (!mapContainer) failures.push("#map-container missing after monitor navigation");
if (!mapDisplay) failures.push("#map (OpenLayers target) missing after monitor navigation");

if (mapContainer) {
  const h = dom.window.getComputedStyle(mapContainer).height;
  // The persistent head stylesheet must give the container a real height.
  if (!h || h === "0px" || h === "auto" || h === "") {
    failures.push(`#map-container height resolved to '${h}' (expected 400px) — CSS not persistent across nav`);
  } else if (h !== "400px") {
    failures.push(`#map-container height resolved to '${h}' (expected 400px)`);
  }
}

// Structural guard: the height rule must live in spa.html's persistent <head> style,
// not only inside a per-page JS template that innerHTML swaps away.
const headStyle = spaHtml.slice(spaHtml.indexOf("<style"), spaHtml.indexOf("</style>"));
if (!/\.map-container\s*\{[^}]*height:\s*400px/s.test(headStyle)) {
  failures.push("spa.html <head> <style> does not contain '.map-container { height: 400px }' (not persistent)");
}

if (failures.length) {
  console.log("RESULT: FAIL");
  for (const f of failures) console.log("  - " + f);
  process.exit(1);
}
console.log("RESULT: PASS");
console.log("  #map-container computed height = " + dom.window.getComputedStyle(mapContainer).height);
process.exit(0);
