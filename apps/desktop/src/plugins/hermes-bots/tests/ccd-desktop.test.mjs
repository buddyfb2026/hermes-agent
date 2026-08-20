import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function stateRuntime() {
  const start = source.indexOf("const CCD_STATE_PATH =")
  const end = source.indexOf('/** Flip the activity-toast pref', start)
  assert.ok(start > 0 && end > start)
  const context = {
    atom(value) {
      let current = value
      return { get: () => current, set: next => { current = next } }
    },
    clearInterval() {},
    console,
    JSON,
    Number,
    Promise,
    String,
    setInterval() { return 1 },
    syncCcdIssueRoom() {},
    window: { hermesDesktop: null }
  }
  vm.createContext(context)
  vm.runInContext(`${source.slice(start, end)}\nglobalThis.__test = { normalizeCcdWorker, ccdRosterRow, withCcdWorkerRow }`, context)
  return context.__test
}

function roomRuntime() {
  const start = source.indexOf('function ccdLineDelta(')
  const end = source.indexOf('async function refreshLinearIssueRooms()', start)
  assert.ok(start > 0 && end > start)
  let rooms = {}
  const context = {
    $groupChats: { get: () => rooms, set: value => { rooms = value } },
    Array,
    Date,
    String,
    updateGroupChat(group, mutate) {
      const current = rooms[group] || { log: [], watermarks: {}, members: [] }
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
  vm.runInContext(`${source.slice(start, end)}\nglobalThis.__test = { ccdLineDelta, syncCcdIssueRoom }`, context)
  return { api: context.__test, rooms: () => rooms }
}

test('CCD state normalizes into a model-less roster row', () => {
  const api = stateRuntime()
  const state = api.normalizeCcdWorker({
    state: 'reviewing', issue_key: 'biz-1066', job_id: 'job-1', packet_version: 2,
    queue_status: 'dispatched', tmux_available: true, lines: ['reviewing AC1'],
    last_output_at: 10, updated_at: 11
  })
  assert.equal(state.state, 'reviewing')
  assert.equal(state.issueKey, 'BIZ-1066')
  assert.match(state.preview, /BIZ-1066 v2: independent review in progress/)
  const roster = api.withCcdWorkerRow({ profiles: [{ name: 'default' }, { name: 'ccd', stale: true }] }, state)
  const rows = JSON.parse(JSON.stringify(roster.profiles))
  assert.equal(rows.filter(row => row.name === 'ccd').length, 1)
  const ccd = rows.find(row => row.name === 'ccd')
  assert.equal(ccd.externalWorker, 'ccd')
  assert.equal(ccd.externalWorking, true)
  assert.equal(ccd.display_name, 'CCD')
})

test('idle CCD history never retroactively creates an issue room', () => {
  const { api, rooms } = roomRuntime()
  api.syncCcdIssueRoom({ state: 'idle', issueKey: 'BIZ-1066', jobId: 'job-1', packetVersion: 2, lines: ['old'] })
  assert.deepEqual(Object.keys(rooms()), [])
})

test('active CCD joins the issue room, preserves members, and appends only deltas', () => {
  const { api, rooms } = roomRuntime()
  rooms()['BIZ-1066'] = {
    log: [], watermarks: {}, members: [{ name: 'hermes2', title: 'Iron Man' }], externalWorkers: {}
  }
  const first = {
    state: 'reviewing', issueKey: 'BIZ-1066', jobId: 'job-1', packetVersion: 2,
    lines: ['reading packet', 'checking AC1'], lastOutputAt: 10
  }
  api.syncCcdIssueRoom(first)
  let room = rooms()['BIZ-1066']
  assert.deepEqual(JSON.parse(JSON.stringify(room.members.map(member => member.name))), ['hermes2', 'ccd'])
  assert.match(room.log.map(entry => entry.text).join('\n'), /CCD joined to independently review BIZ-1066 packet v2/)
  assert.match(room.log.map(entry => entry.text).join('\n'), /checking AC1/)

  api.syncCcdIssueRoom({ ...first, lines: ['reading packet', 'checking AC1', 'checking AC2'] })
  room = rooms()['BIZ-1066']
  const text = room.log.map(entry => entry.text).join('\n')
  assert.equal((text.match(/reading packet/g) || []).length, 1)
  assert.equal((text.match(/checking AC1/g) || []).length, 1)
  assert.equal((text.match(/checking AC2/g) || []).length, 1)

  api.syncCcdIssueRoom({ ...first, state: 'idle', lines: ['reading packet', 'checking AC1', 'checking AC2'] })
  room = rooms()['BIZ-1066']
  assert.match(room.log.at(-1).text, /returned to idle/)
  assert.equal(room.externalWorkers.ccd.state, 'idle')
})

test('CCD surface is observer-only and has no Hermes profile/session action path', () => {
  assert.match(source, /externalWorker: 'ccd'/)
  assert.match(source, /Read-only view · real Claude Code reviewer · no Hermes model session · no prompt relay/)
  assert.match(source, /if \(isCcdWorker\) \{\s+closeStudioFleetCodyWorkspace\(\)\s+openCcdWorkspace\(\)\s+return/)
  assert.match(source, /if \(bot\.remoteSource \|\| bot\.externalWorker\) \{\s+return row/)
  assert.match(source, /useRoutines\(bot, !isCcd\)/)
  assert.doesNotMatch(source, /externalWorker:\s*'ccd'[\s\S]{0,300}host\.newChat/)
})
