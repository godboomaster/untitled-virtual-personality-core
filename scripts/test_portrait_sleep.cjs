// Run: node scripts/test_portrait_sleep.cjs (uses installed desktop TypeScript).
// Hook lifecycle harness: exercise useBody with motion, wake and settings updates.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('../desktop/node_modules/typescript');
const root = path.resolve(__dirname, '..');

function load(file, imports = {}) {
  const code = ts.transpileModule(fs.readFileSync(path.join(root, file), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const exports = {};
  vm.runInNewContext(code, { exports, require: (id) => {
    assert.ok(id in imports, `Unexpected import: ${id}`);
    return imports[id];
  }, window: { setTimeout: () => 1, clearTimeout: () => {} } }, { filename: file });
  return exports;
}

const types = load('web/src/portrait/animTypes.ts');
function mount(options) {
  let slots = [], cursor = 0, effects = [], dirty = false, body;
  const listeners = new Map();
  const hooks = {
    useState(initial) {
      const i = cursor++;
      if (!(i in slots)) slots[i] = initial;
      return [slots[i], (value) => {
        const next = typeof value === 'function' ? value(slots[i]) : value;
        if (!Object.is(slots[i], next)) { slots[i] = next; dirty = true; }
      }];
    },
    useRef(initial) {
      const i = cursor++;
      return slots[i] ??= { current: initial };
    },
    useEffect(effect, deps) {
      const i = cursor++;
      const prev = slots[i];
      if (!prev || !deps || deps.some((v, j) => !Object.is(v, prev.deps[j]))) {
        effects.push(() => {
          prev?.cleanup?.();
          slots[i] = { deps, cleanup: effect() };
        });
      }
    },
  };
  const { useBody } = load('desktop/src/portrait/useBody.ts', {
    react: hooks,
    '@tauri-apps/api/core': { invoke: async () => false },
    './winEvents': { listen: (event, fn) => { listeners.set(event, fn); return Promise.resolve(() => {}); } },
    '../../../web/src/portrait/animTypes': types,
  });
  function render(patch = {}) {
    options = { ...options, ...patch };
    let count = 0;
    do {
      assert.ok(count++ < 20, 'Hook effects must settle');
      cursor = 0; dirty = false; effects = [];
      body = useBody(options);
      for (const effect of effects) effect();
    } while (dirty);
    return body;
  }
  return {
    render,
    event(state) { listeners.get('portrait-motion')({ payload: { state, dir: 1 } }); return render(); },
  };
}

const anim = (id, triggers = [], extra = {}) => ({
  ...types.NEW_ANIM, id, name: id, atlas: 'data:image/webp;base64,test',
  frames: 9, fw: 256, fh: 256, triggers, ...extra,
});
const sleep = anim('00000001');
const fallAsleep = anim('00000002', ['sleep'], { loop: true });
const wake = anim('00000003', ['wake']);
const held = anim('00000004', ['grab']);
const idle = anim('00000005', ['idle'], { loop: true });
const secondSleep = anim('00000006');
const data = { settings: { every_min: 2, walk: true, sleep_anim: sleep.id }, anims: [sleep, fallAsleep, wake, held, idle, secondSleep] };
const options = { anims: data, on: true, physics: false, asleep: true, free: false, calm: false,
  loops: { drop: false, think: false, focus: false }, headEl: () => null };
const app = mount(options);
let body = app.render();
assert.equal(body.showing.anim.id, sleep.id, 'Already asleep on startup shows selected animation');
assert.equal(body.showing.loop, true, 'Sleep loops even without loop flag or triggers');
const stableKey = body.showing.key;
assert.equal(app.render().showing.key, stableKey, 'Unrelated renders do not restart sleep');
assert.equal(app.render({ anims: structuredClone(data) }).showing.key, stableKey, 'Polling with a fresh copy of the same data does not restart sleep');
body.play('sleep'); body = app.render();
assert.equal(body.showing.anim.id, fallAsleep.id, 'Falls-asleep transition takes priority');
assert.equal(body.showing.loop, false, 'Transition cannot trap the sleep cycle in an infinite loop');
body.onEnd(); body = app.render();
assert.equal(body.showing.anim.id, sleep.id, 'Transition returns to continuous sleep');
assert.equal(app.event('held').showing.anim.id, held.id, 'Dragging keeps its own animation');
assert.equal(app.event('idle').showing.anim.id, sleep.id, 'Sleep resumes after releasing');
body = app.render({ asleep: false, calm: true });
assert.equal(body.showing, null, 'Wake immediately stops sleeping');
body.play('wake'); body = app.render();
assert.equal(body.showing.anim.id, wake.id, 'Wake event still plays');
body.onEnd(); body = app.render();
body.act(idle.id); body = app.render();
assert.equal(body.showing.anim.id, idle.id);
assert.equal(app.render({ asleep: true, calm: false }).showing.anim.id, sleep.id, 'Entering sleep interrupts idle loop');
body = app.render({ anims: { ...data, settings: { ...data.settings, sleep_anim: secondSleep.id } } });
assert.equal(body.showing.anim.id, secondSleep.id, 'Changing selection updates a sleeping avatar');
assert.notEqual(body.showing.key, stableKey, 'Changing selection refreshes sprite and hit mask');
assert.equal(app.render({ on: false }).showing, null, 'Global animations toggle disables sleep');
assert.equal(app.render({ on: true }).showing.anim.id, secondSleep.id);
assert.equal(app.render({ anims: { ...data, anims: data.anims.filter((a) => a.id !== sleep.id) } }).showing, null, 'Deleted selection falls back to portrait');
assert.equal(app.render({ anims: { ...data, anims: data.anims.map((a) => ({ ...a, enabled: false })) } }).showing, null, 'Disabled selection falls back to portrait');
assert.equal(app.render({ anims: { ...data, settings: { ...data.settings, sleep_anim: null } } }).showing, null, 'Clearing selection falls back to portrait');
assert.equal(app.render({ anims: { ...data, settings: { every_min: 4, walk: true } } }).showing, null, 'Older settings remain valid');
body = app.render({ anims: data }); body.play('sleep'); body = app.render();
assert.equal(app.render({ asleep: false, calm: true }).showing, null, 'Wake also cancels an unfinished falls-asleep transition');
assert.equal(types.sleepAnimFor({ ...data, anims: [{ ...sleep, atlas: undefined }] }), null);
assert.equal(types.sleepAnimFor({ ...data, anims: [{ ...sleep, frames: 0 }] }), null);
console.log('PASS: sleep startup, transitions, loop, wake, motion, settings, toggles and fallbacks');
