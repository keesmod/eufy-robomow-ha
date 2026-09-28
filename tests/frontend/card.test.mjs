import test from 'node:test';
import assert from 'node:assert/strict';
globalThis.HTMLElement = class {};
globalThis.customElements = {get:() => true};
globalThis.window = {};
const {controls, mapHealth, escapeHTML, cardView, sessionMetricsAvailable, historyColumns} = await import('../../custom_components/eufy_robomow/frontend/eufy-mower-card.js');
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
  assert.equal(sessionMetricsAvailable(mower('unknown', {backend:'bridge'})), false);
  assert.equal(sessionMetricsAvailable(mower('docked', {backend:'local'})), true);
  assert.equal(sessionMetricsAvailable(mower('docked')), true, 'older integrations without the attribute keep the metrics');
  assert.deepEqual(historyColumns([{distance_m:null, area_raw:null}, {}]), {distance:false, area:false});
  assert.deepEqual(historyColumns([{distance_m:null, area_raw:null}, {distance_m:12.5, area_raw:0}]), {distance:true, area:true});
  assert.deepEqual(historyColumns([]), {distance:false, area:false});
});
