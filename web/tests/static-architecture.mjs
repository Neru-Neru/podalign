import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
const app = readFileSync("src/App.tsx", "utf8");
const vite = readFileSync("vite.config.ts", "utf8");
assert.ok(existsSync("src/audioWorker.ts"));
assert.ok(app.includes("BrowserPipeline"));
assert.ok(!app.includes("/api/"));
assert.ok(vite.includes("VITE_BASE_PATH"));
console.log("static browser architecture checks: passed");
