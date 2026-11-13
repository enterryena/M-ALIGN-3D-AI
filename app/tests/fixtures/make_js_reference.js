// Regenerate js_reference_v354.json rows from the JS engine embedded in MALIGN_Desktop.html.
// usage: node make_js_reference.js <scenario> <estimator> <n> <seed>   (prints JSON rows [ok, worst, steps])
globalThis.window = globalThis;
const fs = require('fs'), path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', '..', 'MALIGN_Desktop.html'), 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).slice(0, 4).join('\n');
(0, eval)(scripts.replace('const DATA=', 'globalThis.DATA_=').replace(/\bDATA\b(?!_)/g, 'DATA_'));
const E = globalThis.MALIGN, M = E.buildModel(globalThis.DATA_);
const presets = {ideal: {modelBias: 0, noise: .1, gapSd: 0, tiltSd: 0, tolC: .7, act: .2},
  target: {modelBias: .25, noise: .2, gapSd: .3, tiltSd: .05, tolC: .7, act: .2},
  base: {modelBias: .5, noise: .3, gapSd: 1, tiltSd: .2, tolC: 3, act: .5}};
const base = {R0: 30, theta0: .1, K: .8, iters: 6, tolMM: 5, fitGap: true, mirror: true, useHead: [1, 1, 1, 1, 1], searchMM: 36, stopMM: 1};
const [scen, est, n, seed] = process.argv.slice(2);
const P = {...base, ...presets[scen], estimator: est}, rnd = E.rng(+seed), rows = [];
for (let i = 0; i < +n; i++) { const r = E.runOnce(M, P, rnd, false); rows.push([r.ok ? 1 : 0, r.worst, r.steps]); }
process.stdout.write(JSON.stringify(rows));
