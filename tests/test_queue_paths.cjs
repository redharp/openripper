const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync("src/openripper/static/app.js", "utf8");
const start = source.indexOf("function finalFolder(job)");
const end = source.indexOf("function ripLocation(job)", start);
const context = {
  appState: { preferences: {
    output_mode: "disc", disc_folder_template: "{disc} - {job_id}",
  } },
};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
const job = {
  id: "abc123", disc_name: "  A: Very / Long Movie... ",
  stage_path: "\\\\server\\movies\\.openripper-staging\\abc123",
};
assert.equal(context.finalFolder(job), "\\\\server\\movies\\A - Very - Long Movie - abc123");
assert.equal(context.finalFolder({ ...job, stage_path: "/library/.openripper-staging/abc123" }),
  "/library/A - Very - Long Movie - abc123");
assert.equal(context.finalFolder({ ...job, destination_folder: "/server-authoritative" }),
  "/server-authoritative");
assert.equal(context.finalFolder({ ...job, stage_path: "/unexpected/location" }), "");
context.appState.preferences.disc_folder_template = "{title} - {job_id}";
assert.equal(context.finalFolder(job), "");
console.log("Final folder compatibility checks passed");
