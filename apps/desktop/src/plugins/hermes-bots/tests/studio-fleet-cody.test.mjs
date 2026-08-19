import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function runtime(desktop) {
  const start = source.indexOf("const STUDIO_FLEET_ROOT =")
  const end = source.indexOf('/** Flip the activity-toast pref', start)
  assert.ok(start > 0 && end > start, 'Studio Fleet observer slice must remain extractable')
  const slice = source.slice(start, end)
  const context = {
    atom(value) {
      let current = value
      return { get: () => current, set: next => { current = next } }
    },
    clearInterval() {},
    console,
    Date,
    setInterval() { return 1 },
    window: { hermesDesktop: desktop }
  }
  vm.createContext(context)
  vm.runInContext(`${slice}\nglobalThis.__test = { $studioFleetCody, refreshStudioFleetCody, studioFleetFrontmatter, studioFleetProgress }`, context)
  return context.__test
}

test('real Cody activity overlays the existing profile with issue and checklist progress', async () => {
  const desktop = {
    async readDir(path) {
      if (path.endsWith('/active')) return { entries: [{ isDirectory: false, name: 'build-map.md', path: '/active/build-map.md' }] }
      if (path.endsWith('/pending')) return { entries: [] }
      throw new Error(`unexpected dir ${path}`)
    },
    async readFileText(path) {
      if (path === '/active/build-map.md') {
        return { text: '---\nlinear: BIZ-1278\ntitle: Build Procore map\n---\n\n## Goal\nShip it.' }
      }
      if (path.endsWith('/build-map.codex.log')) {
        return { text: '• inspect files\n✓ migration complete\n→ running tests' }
      }
      throw new Error(`unexpected file ${path}`)
    }
  }
  const api = runtime(desktop)
  await api.refreshStudioFleetCody()
  assert.deepEqual(JSON.parse(JSON.stringify(api.$studioFleetCody.get())), {
    state: 'working',
    preview: 'BIZ-1278: → running tests',
    task: 'build-map',
    refreshedAt: api.$studioFleetCody.get().refreshedAt
  })
})

test('idle Studio Fleet preserves the normal Cody Bot Chat preview', async () => {
  const api = runtime({
    async readDir() { return { entries: [] } },
    async readFileText() { throw new Error('not expected') }
  })
  await api.refreshStudioFleetCody()
  assert.equal(api.$studioFleetCody.get().state, 'idle')
  assert.equal(api.$studioFleetCody.get().preview, '')
})

test('observer failure is explicit rather than falsely idle', async () => {
  const api = runtime({
    async readDir() { throw new Error('offline') },
    async readFileText() { throw new Error('offline') }
  })
  await api.refreshStudioFleetCody()
  assert.equal(api.$studioFleetCody.get().state, 'unavailable')
  assert.equal(api.$studioFleetCody.get().preview, 'Studio Fleet status unavailable')
})

test('overlay is scoped to the existing canonical cody profile only', () => {
  assert.match(source, /bot\.name === 'cody'/)
  assert.match(source, /isStudioFleetCody && \['working', 'queued', 'unavailable'\]\.includes/)
  assert.match(source, /startStudioFleetCodyObserver\(ctx\)/)
  assert.doesNotMatch(source, /append_message|prompt\.submit.*Studio Fleet/)
})
