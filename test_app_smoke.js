// Small browser-boundary regression test using only Node's built-in modules.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const elements = new Map();
function element(selector) {
  if (!elements.has(selector)) {
    elements.set(selector, {
      hidden: false,
      disabled: false,
      textContent: '',
      innerHTML: '',
      listeners: {},
      addEventListener(name, callback) { this.listeners[name] = callback; },
      setAttribute() {},
      classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    });
  }
  return elements.get(selector);
}

let apiCalls = 0;
const document = {
  querySelector: element,
  querySelectorAll: () => [],
};
const context = {
  document,
  window: { location: { protocol: 'file:' } },
  localStorage: { getItem() { return null; }, setItem() {} },
  fetch() { apiCalls += 1; throw new Error('file mode must not attempt API requests'); },
  URL,
  Date,
  Intl,
  console,
};

const source = fs.readFileSync('app.js', 'utf8');
vm.runInNewContext(source, context, { filename: 'app.js' });

assert.equal(element('#file-open-notice').hidden, false, 'file view explains the local server requirement');
assert.equal(element('#refresh').disabled, true, 'refresh is disabled when no API server exists');
assert.equal(element('#settings-open').disabled, true, 'settings are disabled when data cannot load');
assert.match(element('#story-count').textContent, /server required/i);
assert.equal(apiCalls, 0, 'file view does not issue broken API requests');
process.stdout.write('PASS direct file-open recovery and no broken API requests\n');
