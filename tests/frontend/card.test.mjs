import test from 'node:test';
import assert from 'node:assert/strict';
globalThis.HTMLElement = class {};
globalThis.customElements = {get:() => true};
globalThis.window = {};
const {controls, mapHealth, escapeHTML, cardView, sessionMetrics, historyColumns, EufyMowerCard} = await import('../../custom_components/eufy_robomow/frontend/eufy-mower-card.js');
const now = Date.parse('2026-09-06T12:00:00Z');
const mower = (state, options = {}) => ({state, attributes:{operating_mode:'control', supported_features:7, telemetry_updated_at:new Date(now).toISOString(), ...options}});
test('observe-only and stale status disable physical controls', () => {
  assert.equal(controls(mower('docked', {operating_mode:'observe_only'}), now).start, false);
  assert.equal(controls(mower('docked', {telemetry_updated_at:'2026-09-06T11:58:00Z'}), now).start, false);
  assert.equal(controls(mower('docked'), now).start, true);
  assert.equal(controls(mower('unavailable'), now).start, false);
});
test('pending start cannot repeat, safety pause stays available', () => {
  assert.equal(controls(mower('docked', {command:{state:'pending'}}), now).start, false);
  assert.equal(controls(mower('mowing', {command:{state:'pending'}}), now).pause, true);
  assert.equal(controls(mower('mowing', {supported_features:1}), now).pause, false);
});
test('the drive home offers dock but no pause, which the E15 ignores', () => {
  assert.equal(controls(mower('returning'), now).pause, false);
  assert.equal(controls(mower('returning'), now).dock, true);
  assert.equal(controls(mower('paused'), now).start, true);
});
test('old image is visibly cached even when image entity is available', () => {
  const image = {state:'2026-09-06T11:55:00Z', attributes:{acquisition_status:'healthy', acquisition_last_success:'2026-09-06T11:55:00Z'}};
  assert.equal(mapHealth(image, true, now).kind, 'warn');
  assert.equal(mapHealth(image, false, now).kind, 'good');
  image.attributes.acquisition_status = 'failed';
  assert.equal(mapHealth(image, false, now).kind, 'warn');
});
test('entity values cannot inject markup', () => {
  assert.equal(escapeHTML('<img onerror="x">'), '&lt;img onerror=&quot;x&quot;&gt;');
});
test('existing cards retain their combined view and invalid sections are rejected', () => {
  assert.equal(cardView(), 'all');
  for (const view of ['overview', 'history', 'planning', 'settings']) assert.equal(cardView(view), view);
  assert.throws(() => cardView('zone-control'), /paneelweergave/);
  assert.throws(() => cardView(null), /paneelweergave/);
});
test('a connected mower with an unknown activity is connected but offers no command', () => {
  const resting = controls(mower('unknown'), now);
  assert.equal(resting.connected, true);
  assert.equal(resting.fresh, false);
  assert.equal(resting.settings, true, 'settings do not depend on the activity');
  for (const name of ['start', 'pause', 'dock']) assert.equal(resting[name], false, name);
  const stale = controls(mower('unknown', {telemetry_updated_at:'2026-09-06T11:58:00Z'}), now);
  assert.equal(stale.connected, false);
  assert.equal(stale.settings, false);
  assert.equal(controls(mower('unavailable'), now).connected, false);
  assert.equal(controls(undefined, now).connected, false);
  assert.equal(controls(mower('unknown', {operating_mode:'observe_only'}), now).settings, false);
  const docked = controls(mower('docked'), now);
  assert.equal(docked.connected && docked.fresh && docked.start, true);
});
test('session metrics without a bridge source are hidden', () => {
  const all = {progress:true, distance:true, area:true, duration:true};
  assert.deepEqual(sessionMetrics(mower('unknown', {backend:'bridge'}), null), {progress:false, distance:false, area:false, duration:false});
  assert.deepEqual(sessionMetrics(mower('mowing', {backend:'bridge'}), {mowing_seconds:20}), {progress:false, distance:false, area:false, duration:true}, 'the session store times a bridge session');
  assert.deepEqual(sessionMetrics(mower('docked', {backend:'local'}), null), all);
  assert.deepEqual(sessionMetrics(mower('docked'), null), all, 'older integrations without the attribute keep the metrics');
  assert.deepEqual(historyColumns([{distance_m:null, area_raw:null}, {}]), {distance:false, area:false});
  assert.deepEqual(historyColumns([{distance_m:null, area_raw:null}, {distance_m:12.5, area_raw:0}]), {distance:true, area:true});
  assert.deepEqual(historyColumns([]), {distance:false, area:false});
});
function fakeRoot() {
  const elements = new Map();
  const element = () => ({textContent:'', hidden:false, disabled:false, className:'', style:{}, innerHTML:'', attributes:{},
    setAttribute(name, value) { this.attributes[name] = value; }, removeAttribute(name) { delete this.attributes[name]; }, getAttribute(name) { return this.attributes[name] ?? null; }});
  const get = key => { if (!elements.has(key)) elements.set(key, element()); return elements.get(key); };
  return {getElementById:get, querySelector:get, querySelectorAll:() => []};
}
function render(mowerState, attributes, session = {}, view = 'overview') {
  const card = Object.create(EufyMowerCard.prototype);
  card.shadowRoot = fakeRoot();
  card.config = {entity:'lawn_mower.m', session:'sensor.s', view};
  card._localBusy = false;
  card._hass = {states:{'lawn_mower.m':mower(mowerState, attributes), 'sensor.s':{state:'idle', attributes:{current_session:null, recent_sessions:[], ...session}}}};
  const realNow = Date.now;
  Date.now = () => now;
  try { card._update(); } finally { Date.now = realNow; }
  return id => card.shadowRoot.getElementById(id);
}
test('the card renders a connected unknown activity on the bridge without empty metrics', () => {
  const el = render('unknown', {backend:'bridge'});
  assert.equal(el('activity').textContent, 'Activiteit onbekend');
  assert.equal(el('connection-status').textContent, 'Verbonden');
  assert.match(el('mode').textContent, /meldt nu geen activiteit/);
  assert.equal(el('settings-details').hidden, false);
  for (const id of ['start', 'pause', 'dock']) assert.equal(el(id).disabled, true, id);
  for (const id of ['progress-metric', '.progress-track', 'distance-row', 'area-row', 'area-note', 'duration-row']) assert.equal(el(id).hidden, true, id);
  const mowing = render('mowing', {backend:'bridge'}, {current_session:{mowing_seconds:120}});
  assert.equal(mowing('duration-row').hidden, false);
  assert.equal(mowing('duration').textContent, '2 min');
  assert.equal(mowing('distance-row').hidden, true);
  const stale = render('unknown', {backend:'bridge', telemetry_updated_at:'2026-09-06T11:58:00Z'});
  assert.equal(stale('activity').textContent, 'Status onbekend');
  assert.equal(stale('connection-status').textContent, 'Geen actuele status');
  assert.equal(stale('mode').textContent, 'Bediening wacht op actuele maaierstatus.');
  assert.equal(stale('settings-details').hidden, true);
  const local = render('docked', {backend:'local'});
  assert.equal(local('start').disabled, false);
  for (const id of ['progress-metric', '.progress-track', 'distance-row', 'area-row', 'duration-row']) assert.equal(local(id).hidden, false, id);
});
test('the history table drops distance and area columns without values', () => {
  const row = {started_at:'2026-09-27T15:42:14Z', mowing_seconds:0, distance_m:null, area_raw:null, start_observed:false, end_observed:false, observation_gap:true, pause_count:1, paused_seconds:12};
  const bare = render('unknown', {backend:'bridge'}, {recent_sessions:[row]}, 'history')('history').innerHTML;
  assert.equal((bare.match(/<th>/g) ?? []).length, 3);
  assert.equal((bare.match(/<td>/g) ?? []).length, 3);
  assert.doesNotMatch(bare, /Afstand|Oppervlakte/);
  const measured = render('docked', {backend:'local'}, {recent_sessions:[row, {...row, distance_m:12.5, area_raw:3}]}, 'history')('history').innerHTML;
  assert.equal((measured.match(/<th>/g) ?? []).length, 5);
  assert.equal((measured.match(/<td>/g) ?? []).length, 10);
});
