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
      showModal() { this.opened = true; },
      close() { this.opened = false; },
      setAttribute() {},
      classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    });
  }
  return elements.get(selector);
}

let apiCalls = 0;
const localValues = new Map();
const document = {
  querySelector: element,
  querySelectorAll: () => [],
};
const context = {
  document,
  window: { location: { protocol: 'file:' } },
  localStorage: { getItem(key) { return localValues.get(key) || null; }, setItem(key,value) { localValues.set(key,value); } },
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
assert.match(fs.readFileSync('index.html', 'utf8'), /This copy cannot refresh feeds/, 'file view explains why live refresh is unavailable');
assert.doesNotMatch(fs.readFileSync('index.html', 'utf8'), /id="online-reader"/, 'file view does not redirect to another website');
process.stdout.write('PASS direct file-open recovery and no broken API requests\n');

vm.runInNewContext(`config = {glossary:[{id:'carbon_pricing',term:'Carbon pricing',explanation:'A price on emissions.',source_name:'World Bank',source_url:'https://www.worldbank.org/'}]}; drawQuickExplainers()`, context);
assert.equal(element('#quick-explainers').hidden, false, 'curated explainers are available even when no story contains a glossary term');
assert.match(element('#quick-explainer-list').innerHTML, /Carbon pricing/);
element('#quick-explainer-list').listeners.click({target:{closest(){return {dataset:{termId:'carbon_pricing'}};}}});
assert.equal(element('#term-dialog').opened, true, 'browsable glossary terms open their explanation dialog');
assert.equal(element('#term-title').textContent, 'Carbon pricing');
assert.equal(element('#term-source-link').href, 'https://www.worldbank.org/');
assert.match(fs.readFileSync('index.html', 'utf8'), /Morning time budget/);
assert.match(fs.readFileSync('index.html', 'utf8'), /<span>Preferences<\/span>/, 'preferences are visible by name, not only as an icon');
process.stdout.write('PASS visible budget preferences and browsable sourced explainers\n');

vm.runInNewContext(`
  stateless = true;
  localStorage.setItem('morning-signal-reader-v1', JSON.stringify({morningBudget:5}));
`, context);
const budgeted = vm.runInNewContext(`applyBrowserState({
  stories: [
    {id:'three',reading_minutes:3,url:'https://publisher.example/3'},
    {id:'four',reading_minutes:4,url:'https://publisher.example/4'},
    {id:'unknown',url:'https://publisher.example/unknown'}
  ], unverifiedStories:[], hiddenStories:[], reading:{saved:[],history:[]},
  settings:{},selection:{requested:10}
})`, context);
assert.deepEqual(Array.from(budgeted.stories, story => story.id), ['three','unknown']);
assert.equal(budgeted.selection.budget_used_minutes, 5);
assert.equal(budgeted.stories[1].budget_time_label, '2-minute planning allowance');
assert.equal(budgeted.settings.morning_budget, 5);
process.stdout.write('PASS browser-local time budget fits selected stories and labels missing read times\n');
