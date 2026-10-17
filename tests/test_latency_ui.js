const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.events = {}; this.text = ''; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join('\n'); }
  append(...children) { this.children.push(...children); }
  replaceChildren() { this.text = ''; this.children = []; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  addEventListener(event, callback) { this.events[event] = callback; }
  getBoundingClientRect() { return {width:900, left:0}; }
}
const context = vm.createContext({document: {
  createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag)}});
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
vm.runInContext(source.slice(0, source.indexOf("$('login-form').addEventListener")), context);
const start = Date.parse('2026-10-16T00:00:00Z'), end = start + 86400000;
const series = [
  {key:'component:application', label:'application', points:[[start, 0], [end-1000, 100]]},
  {key:'component:database', label:'database', points:[[start, 500], [end-1000, 2500]]}];

function render(items = series) {
  const node = new Element('div');
  context.renderLatency(node, {from:new Date(start).toISOString(), to:new Date(end).toISOString(),
    monitors:[{name:'Example <script>', series:items}]});
  return node;
}
function descendants(node, tag) {
  return [...node.children.filter(child => child.tag === tag), ...node.children.flatMap(child => descendants(child, tag))];
}

test('chart starts at zero and extends exactly ten percent above the highest series value', () => {
  const model = context.latencyModel(series);
  assert.equal(model.maximum, 2500);
  assert.equal(model.ceiling, 2750);
  const node = render();
  const chart = descendants(node, 'svg')[0];
  assert.ok(chart.attributes['aria-label'].includes(`0 to ${context.latencyNumber(2750)} milliseconds`));
  assert.ok(node.textContent.includes(`Peak ${context.latencyNumber(2500)} ms`));
  const paths = descendants(chart, 'path');
  assert.equal(paths.length, 2);
  assert.notEqual(paths[0].attributes.stroke, paths[1].attributes.stroke);
  assert.match(node.textContent, /application/);
  assert.match(node.textContent, /database/);
  assert.equal(descendants(node, 'script').length, 0);
});

test('null measurements break paths and isolated measurements stay visible', () => {
  const d = context.latencyPath([[1, 12], [2, null], [3, 50], [4, 0]], value => value, value => value);
  assert.equal((d.match(/M/g) || []).length, 2);
  assert.match(d, /M1.00 12.00h0.01/);
  assert.match(d, /M3.00 50.00h0.01 L4.00 0.00/);
  assert.equal(context.latencyModel([{points:[[1, 0]]}]).ceiling, 1);
  assert.match(render([]).textContent, /No latency measurements recorded/);
});

test('keyboard inspection exposes the timestamp and all series values', () => {
  const node = render();
  const chart = descendants(node, 'svg')[0];
  chart.events.focus();
  assert.ok(node.textContent.includes(`application: 100 ms · database: ${context.latencyNumber(2500)} ms`));
  chart.events.keydown({key:'Home', preventDefault() {}});
  assert.match(node.textContent, /application: 0 ms · database: 500 ms/);
  chart.events.keydown({key:'ArrowRight', preventDefault() {}});
  assert.ok(node.textContent.includes(`application: 100 ms · database: ${context.latencyNumber(2500)} ms`));
});
