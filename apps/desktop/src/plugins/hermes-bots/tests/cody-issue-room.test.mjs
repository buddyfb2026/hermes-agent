import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function runtime() {
  const start = source.indexOf('function codyLineDelta(')
  const end = source.indexOf('async function refreshLinearIssueRooms()', start)
  assert.ok(start > 0 && end > start, 'Cody issue-room slice must remain extractable')
  let rooms = {}
  const context = {
    $groupChats: { get: () => rooms, set: value => { rooms = value } },
    Array,
    Date,
    String,
    updateGroupChat(group, mutate) {
      const current = rooms[group] || { log: [], watermarks: {}, members: [], externalWorkers: {} }
      const next = mutate({
        ...current,
        log: [...(current.log || [])],
        watermarks: { ...(current.watermarks || {}) },
        members: [...(current.members || [])],
        externalWorkers: { ...(current.externalWorkers || {}) }
      })
      rooms = { ...rooms, [group]: next }
      return next
    }
  }
  vm.createContext(context)
  vm.runInContext(`${source.slice(start, end)}\nglobalThis.__test = { codyLineDelta, syncCodyIssueRoom }`, context)
  return { api: context.__test, rooms: () => rooms }
}

const working = {
  state: 'working',
  issueKey: 'BIZ-1066',
  jobId: 'biz-1066-r2',
  title: 'Fix discovery accounting',
  lines: ['reading tests', 'writing regression'],
  lastOutputAt: 10,
  sourcePath: '/queue/active/biz-1066-r2.md',
  logPath: '/logs/biz-1066-r2.codex.log',
  worktree: '/Users/buddystudio1/CodyWork/biz-1066-r2'
}

test('unlinked or merely queued Cody work creates no issue room', () => {
  const { api, rooms } = runtime()
  api.syncCodyIssueRoom({ ...working, issueKey: '' })
  api.syncCodyIssueRoom({ ...working, state: 'queued' })
  assert.deepEqual(Object.keys(rooms()), [])
})

test('working Cody joins the existing issue room without removing Avengers or CCD', () => {
  const { api, rooms } = runtime()
  rooms()['BIZ-1066'] = {
    log: [], watermarks: {},
    members: [{ name: 'hermes2', title: 'Iron Man' }, { name: 'ccd', title: 'CCD' }],
    externalWorkers: { ccd: { jobId: 'ccd-1', state: 'idle' } }
  }
  api.syncCodyIssueRoom(working)
  const room = rooms()['BIZ-1066']
  assert.deepEqual(JSON.parse(JSON.stringify(room.members.map(member => member.name))), ['hermes2', 'ccd', 'cody'])
  assert.equal(room.externalWorkers.ccd.jobId, 'ccd-1')
  assert.equal(room.externalWorkers.cody.jobId, 'biz-1066-r2')
  const text = room.log.map(entry => entry.text).join('\n')
  assert.match(text, /Cody joined to code BIZ-1066 through the hardened Studio Fleet lane/)
  assert.match(text, /writing regression/)
})

test('Cody transcript polling appends only new suffix lines', () => {
  const { api, rooms } = runtime()
  api.syncCodyIssueRoom(working)
  api.syncCodyIssueRoom({ ...working, lines: ['reading tests', 'writing regression', 'running suite'] })
  const text = rooms()['BIZ-1066'].log.map(entry => entry.text).join('\n')
  assert.equal((text.match(/reading tests/g) || []).length, 1)
  assert.equal((text.match(/writing regression/g) || []).length, 1)
  assert.equal((text.match(/running suite/g) || []).length, 1)
})

test('Cody remains seated when its active run ends', () => {
  const { api, rooms } = runtime()
  api.syncCodyIssueRoom(working)
  api.syncCodyIssueRoom({ ...working, state: 'idle' })
  const room = rooms()['BIZ-1066']
  assert.equal(room.members.some(member => member.name === 'cody'), true)
  assert.equal(room.externalWorkers.cody.state, 'idle')
  assert.match(room.log.at(-1).text, /active run ended/)
})

test('source contract keeps Studio Fleet as the only executor', () => {
  assert.match(source, /externalWorker: 'studio-fleet-cody'/)
  assert.match(source, /linear.*trigger-neutral routing field/)
  assert.doesNotMatch(source, /syncCodyIssueRoom[\s\S]{0,3000}(?:prompt\.submit|host\.newChat|codex exec)/)
})
