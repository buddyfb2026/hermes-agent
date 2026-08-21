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
    syncCodyIssueRoom() {},
    syncCcdIssueRoom() {},
    setInterval() { return 1 },
    window: { hermesDesktop: desktop }
  }
  vm.createContext(context)
  vm.runInContext(`${slice}\nglobalThis.__test = { $studioFleetCody, refreshStudioFleetCody, refreshStudioFleetHistory, studioFleetFrontmatter, studioFleetIssueKey, studioFleetProgress, studioFleetResult }`, context)
  return context.__test
}

test('real Cody activity overlays the existing profile with issue and checklist progress', async () => {
  const desktop = {
    async readDir(path) {
      if (path.endsWith('/active')) return { entries: [{ isDirectory: false, name: 'build-map.md', path: '/active/build-map.md' }] }
      if (path.endsWith('/pending') || path.endsWith('/done') || path.endsWith('/bounced')) return { entries: [] }
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
    jobId: 'build-map',
    issueKey: 'BIZ-1278',
    sourcePath: '/active/build-map.md',
    title: 'Build Procore map',
    identity: 'BIZ-1278',
    lines: ['• inspect files', '✓ migration complete', '→ running tests'],
    logPath: '/Users/buddystudio1/Projects/studio-fleet/cody/logs/build-map.codex.log',
    worktree: '/Users/buddystudio1/CodyWork/build-map',
    logBytes: '• inspect files\n✓ migration complete\n→ running tests'.length,
    lastOutputAt: api.$studioFleetCody.get().lastOutputAt,
    history: [],
    refreshedAt: api.$studioFleetCody.get().refreshedAt
  })
})

test('today history preserves one thread per active, done, and bounced job', async () => {
  const today = new Date().toISOString().slice(0, 10)
  const records = {
    '/active/live.md': `---\nid: live\ncreated: ${today}T10:00:00Z\nlinear: BIZ-1\ntitle: Live job\n---`,
    '/done/good.md': `---\nid: good\ncreated: ${today}T09:00:00Z\nlinear: BIZ-2\ntitle: Good job\n---\n- **outcome:** \`done\`\n- **acceptance:** 2/2 passed`,
    '/bounced/bad.md': `---\nid: bad\ncreated: ${today}T08:00:00Z\ntitle: Bad job\n---\n- **outcome:** \`bounced\` (\`timeout\`)`
  }
  const api = runtime({
    async readDir(path) {
      const stage = path.split('/').at(-1)
      if (stage === 'pending') return { entries: [] }
      const ids = stage === 'active' ? ['live'] : stage === 'done' ? ['good'] : stage === 'bounced' ? ['bad'] : []
      return { entries: ids.map(id => ({ isDirectory: false, name: `${id}.md`, path: `/${stage}/${id}.md` })) }
    },
    async readFileText(path) {
      if (records[path]) return { text: records[path] }
      if (path.endsWith('.codex.log')) return { text: `output for ${path}` }
      throw new Error(`unexpected file ${path}`)
    }
  })
  await api.refreshStudioFleetCody()
  const history = JSON.parse(JSON.stringify(api.$studioFleetCody.get().history))
  assert.deepEqual(history.map(job => [job.id, job.stage, job.result.outcome]), [
    ['live', 'active', 'active'],
    ['good', 'done', 'done'],
    ['bad', 'bounced', 'bounced']
  ])
  assert.equal(history[0].issueKey, 'BIZ-1')
  assert.equal(history[1].result.acceptance, '2/2 passed')
})

test('issue-room routing uses only the explicit trigger-neutral linear field', () => {
  const api = runtime({})
  assert.equal(api.studioFleetIssueKey({ linear: 'BIZ-1066' }), 'BIZ-1066')
  assert.equal(api.studioFleetIssueKey({ linear: 'bad' }), '')
  assert.equal(api.studioFleetIssueKey({ id: 'biz-1066-fix', title: 'BIZ-1066 fix' }), '')
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
  assert.match(source, /openStudioFleetCodyWorkspace\(\)/)
  assert.match(source, /closeStudioFleetCodyWorkspace\(\)/)
  assert.match(source, /studioFleetCody\.history\?\.length > 0/)
  assert.match(source, /selecting any unrelated Bot still closes it/)
  assert.match(source, /title: 'Cody · Studio Fleet'/)
  assert.match(source, /\$botWorkspaceProfile\.set\('cody'\)/)
  assert.match(source, /const visibleProfile = workspaceProfile \|\| focusedProfile/)
  assert.match(source, /workspaceProfile \|\| focusedProfile \|\| selected/)
  assert.doesNotMatch(source, /append_message|prompt\.submit.*Studio Fleet/)
})
