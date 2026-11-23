const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("src/openripper/static/app.js", "utf8");
const context = {};
vm.createContext(context);
vm.runInContext(
  source.slice(
    source.indexOf("function transferPhase("),
    source.indexOf("function transferMarkup("),
  ),
  context,
);
const { transferActivity, observeWrite } = context;
const job = {
  status: "ripping",
  telemetry: { bytes_written: 10000, seconds_since_write: 0 },
};
assert.equal(transferActivity(job, 2).kind, "writing");
assert.equal(transferActivity(job, 12).kind, "waiting");
assert.equal(transferActivity(job, 16).kind, "stale");
assert.equal(
  transferActivity(
    { ...job, telemetry: { ...job.telemetry, seconds_since_write: 70 } },
    0,
  ).kind,
  "stalled",
);
assert.equal(
  transferActivity({ status: "ripping", telemetry: {} }, 0).kind,
  "waiting",
);
assert.equal(transferActivity({ status: "scanning" }, 0).kind, "working");
const first = observeWrite(null, job.telemetry, 1000);
const repeated = observeWrite(first, job.telemetry, 20000);
assert.equal(
  repeated.at,
  1000,
  "Fresh snapshots must not imply fresh writes when bytes are unchanged",
);
assert.equal(
  observeWrite(repeated, { ...job.telemetry, bytes_written: 20000 }, 21000).at,
  21000,
);
assert.equal(observeWrite(null, { bytes_written: 100 }, 1000).at, null);
console.log("Live feedback freshness and write detection passed");

Object.assign(context, {
  formatBytes: n => `${n} B`, formatDuration: n => `${n}s`,
  elapsedTime: () => '30m elapsed', utcDate: value => new Date(value),
});
const stopped = { status: 'ripping', progress: .2, started_at: '2026-01-01T00:00:00Z',
  telemetry: { bytes_per_second: 0, seconds_since_write: 120, eta_seconds: 300 }, };
assert.equal(context.jobFeedback(stopped).speed, '0 B/s');
assert.equal(context.jobFeedback(stopped).eta, 'Waiting for data');
assert.equal(context.jobFeedback({...stopped, telemetry: {bytes_per_second: 0}}).eta, 'Calculating…');
assert.equal(context.jobFeedback({...stopped, telemetry: {bytes_per_second: 1000, eta_seconds: 120}}).eta, '~120s');
console.log('Stopped transfers show zero speed and suppress misleading ETAs');

const preparation = {
  ...stopped,
  telemetry: { ...stopped.telemetry, phase: "opening_title", bytes_written: 15000 },
};
const verification = {
  ...preparation,
  telemetry: { ...preparation.telemetry, phase: "verifying_video" },
};
assert.equal(transferActivity(preparation, 5).label, "Preparing title");
assert.match(transferActivity(preparation, 5).detail, /MakeMKV is opening the disc/);
assert.equal(transferActivity(verification, 5).label, "Verifying video");
for (const phase of [preparation, verification]) {
  assert.equal(transferActivity(phase, 5).kind, "working");
  assert.equal(transferActivity(phase, 16).kind, "stale");
  const feedback = context.jobFeedback(phase);
  assert.equal(feedback.speed, null);
  assert.equal(feedback.eta, null);
  assert.equal(feedback.warning, "");
  assert.match(feedback.bytes, /^15000 B/);
}
const warning = "Disc read error; MakeMKV is retrying";
const withWarning = {
  ...preparation,
  telemetry: { ...preparation.telemetry, read_warning: warning },
};
assert.equal(context.jobFeedback(withWarning).warning, warning);
assert.equal(context.jobFeedback(stopped).warning.length > 0, true);
console.log("Opening and verification display their phase and retain real read warnings");
