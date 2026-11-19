// Parity check: the JS browser site must give the same job as the Python app (sim_job.run_job).
// usage: node parity_test.js <python_reference.json>
globalThis.window = globalThis;
const fs = require('fs'), path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', '..', 'app', 'MASKIT_Desktop.html'), 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).slice(0, 4).join('\n');
(0, eval)(scripts.replace('const DATA=', 'globalThis.DATA_=').replace(/\bDATA\b(?!_)/g, 'DATA_'));
const S = require('./browser_site.js'), E = globalThis.MASKIT, M = E.buildModel(globalThis.DATA_);
const ref = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
let bad = 0;
for (const [hw, mach, fault, strat, seed, outcome, worst, rounds, sensings] of ref) {
  const P = S.settings(hw, 'history');
  const r = S.runJob(M, P, E.rng(seed), E.rng((seed ^ 0x5A5A5A5A) >>> 0), strat, mach, fault);
  if (r.outcome !== outcome || Math.abs(r.worst - worst) > 1e-6 || r.rounds !== rounds || r.sensings !== sensings) {
    bad++; if (bad <= 5) console.log('MISMATCH', hw, mach, fault, strat, seed, {py: [outcome, worst, rounds, sensings], js: [r.outcome, +r.worst.toFixed(6), r.rounds, r.sensings]});
  }
}
console.log(`${ref.length - bad}/${ref.length} jobs identical`);
process.exit(bad ? 1 : 0);
