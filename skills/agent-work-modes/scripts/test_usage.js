// Run: node skills/agent-work-modes/scripts/test_usage.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, 'loop-status.html'), 'utf8');
const js = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(js); // Also check the complete page's JavaScript syntax.
const elements = {};
const context = vm.createContext({
  G: {}, samples: [],
  $: id => elements[id] ||= {innerHTML: ''}, $$: () => [],
  usageNodes: () => context.samples,
  esc: String, cleanProj: String, modeColor: () => 'green', fmtT: String,
});
for (const name of ['fmtN', 'usd']) {
  vm.runInContext(js.match(new RegExp(`const ${name} = [^\\n]+`))[0], context);
}
for (const name of ['knownCost', 'usageAmount', 'drawUsage']) {
  vm.runInContext(js.match(new RegExp(`function ${name}\\([^]*?\\n}`))[0], context);
}
const sample = (tokens, cost, mode = 'conversation') => ({tokens, cost, mode, project: 'test'});
function check(samples, total, share) {
  context.samples = samples;
  assert.equal(vm.runInContext('usageAmount(samples)', context), total);
  vm.runInContext('drawUsage()', context);
  assert.ok(elements['#ukpis'].innerHTML.includes(total));
  assert.ok(elements['#ukpis'].innerHTML.includes(`${share}%<em>`));
  for (const id of ['#ukpis', '#umodes', '#uprojects', '#udays', '#uhours']) {
    assert.ok(!/unknown|NaN|Infinity/.test(elements[id].innerHTML), id);
  }
}
check([sample(1500, null), sample(500, null, 'steering a loop')], '2 k tok', 25);
check([sample(1500, 1), sample(500, 9, 'steering a loop')], '2 k tok ($10.00)', 25);
check([sample(1500, 1), sample(500, null, 'steering a loop')], '2 k tok', 25);
assert.ok(elements['#umodes'].innerHTML.includes('2 k tok ($1.00)'));
assert.ok(elements['#umodes'].innerHTML.includes('500 tok'));
check([sample(0, 0)], '0 tok ($0.00)', 0);
check([], '0 tok', 0);
console.log('ok: tokens, optional prices, mixed pricing, steering share, zero and empty usage');
