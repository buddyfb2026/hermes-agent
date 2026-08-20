import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function runtime(pages = []) {
  const start = source.indexOf("const LINEAR_ISSUE_ROOM_ROOT =")
  const end = source.indexOf('/** Ensure the member\'s per-group session exists', start)
  assert.ok(start > 0 && end > start, 'issue-room observer slice must remain extractable')
  const slice = source.slice(start, end)
  let rooms = {}
  let requestIndex = 0
  const groupStore = { get: () => rooms, set: next => { rooms = next } }
  const context = {
    $groupChats: groupStore,
    Date,
    JSON,
    Math,
    Number,
    Promise,
    String,
    clearInterval() {},
    console,
    setInterval() { return 1 },
    updateGroupChat(group, mutate) {
      const current = rooms[group] || { log: [], watermarks: {}, epoch: 0, running: false }
      const next = mutate({
        ...current,
        log: [...(current.log || [])],
        watermarks: { ...(current.watermarks || {}) }
      })
      rooms = { ...rooms, [group]: next }
      return next
    },
    async requestForBot(_member, method, params) {
      assert.equal(method, 'session.messages')
      const page = pages[requestIndex++] || { messages: [], next_offset: params.offset }
      if (page.expectedOffset !== undefined) assert.equal(params.offset, page.expectedOffset)
      return page
    },
    window: { hermesDesktop: null }
  }
  vm.createContext(context)
  vm.runInContext(`${slice}\nglobalThis.__test = { syncLinearIssueRoom, issueRoomMessageText, syncPrReviewIssueRoom, normalizePrReviewRecord }`, context)
  return { api: context.__test, rooms: () => rooms }
}

const base = {
  issue_key: 'BIZ-9999',
  issue_id: 'linear-uuid',
  job_id: 'job-1',
  members: [{ profile: 'hermes3', callsign: 'Captain America' }],
  authoring: {
    profile: 'hermes3',
    callsign: 'Captain America',
    status: 'authoring',
    session_id: 'session-1',
    baseline_message_count: 5
  }
}

test('claim creates issue room and mirrors only post-baseline authoring output', async () => {
  const { api, rooms } = runtime([{
    expectedOffset: 5,
    next_offset: 9,
    messages: [
      { role: 'user', content: 'private synthetic packet prompt' },
      { role: 'assistant', tool_calls: [{ function: { name: 'read_file' } }] },
      { role: 'tool', name: 'read_file', content: 'Section 5 evidence' },
      { role: 'assistant', content: 'Packet draft written.' }
    ]
  }])
  await api.syncLinearIssueRoom(base)
  const room = rooms()['BIZ-9999']
  assert.deepEqual(JSON.parse(JSON.stringify(room.members.map(member => member.name))), ['hermes3'])
  assert.equal(room.automation.cursor, 9)
  assert.equal(room.automation.status, 'authoring')
  const text = room.log.map(entry => entry.text).join('\n')
  assert.match(text, /Captain America claimed packet authoring/)
  assert.match(text, /read_file/)
  assert.match(text, /Section 5 evidence/)
  assert.match(text, /Packet draft written/)
  assert.doesNotMatch(text, /private synthetic packet prompt/)
})

test('re-poll resumes at cursor without duplicating claim or messages', async () => {
  const { api, rooms } = runtime([
    { expectedOffset: 5, next_offset: 6, messages: [{ role: 'assistant', content: 'first' }] },
    { expectedOffset: 6, next_offset: 6, messages: [] }
  ])
  await api.syncLinearIssueRoom(base)
  await api.syncLinearIssueRoom(base)
  const lines = rooms()['BIZ-9999'].log.map(entry => entry.text)
  assert.equal(lines.filter(line => /claimed packet authoring/.test(line)).length, 1)
  assert.equal(lines.filter(line => line === 'first').length, 1)
})

test('retry preserves prior member and seats the new claiming Avenger', async () => {
  const { api, rooms } = runtime([
    { expectedOffset: 5, next_offset: 5, messages: [] },
    { expectedOffset: 2, next_offset: 2, messages: [] }
  ])
  await api.syncLinearIssueRoom(base)
  await api.syncLinearIssueRoom({
    ...base,
    job_id: 'job-2',
    members: [...base.members, { profile: 'hermes4', callsign: 'Black Widow' }],
    authoring: {
      ...base.authoring,
      profile: 'hermes4',
      callsign: 'Black Widow',
      session_id: 'session-2',
      baseline_message_count: 2
    }
  })
  const room = rooms()['BIZ-9999']
  assert.deepEqual(JSON.parse(JSON.stringify(room.members.map(member => member.name))), ['hermes3', 'hermes4'])
  assert.equal(room.automation.job_id, 'job-2')
  assert.equal(room.automation.cursor, 2)
})

test('archived lifecycle hides the active room without deleting its history', async () => {
  const { api, rooms } = runtime([{ expectedOffset: 5, next_offset: 5, messages: [] }])
  await api.syncLinearIssueRoom({ ...base, room_state: 'archived' })
  const room = rooms()['BIZ-9999']
  assert.equal(room.lifecycle.state, 'archived')
  assert.ok(room.log.length > 0)
})

test('explicit PR review metadata seats a read-only Frontier observer and dedupes deltas', () => {
  const { api, rooms } = runtime()
  const record = {
    issue_key: 'BIZ-9999', pr_number: 42, review_id: 'thread-1',
    provider: 'ChatGPT Desktop', state: 'reviewing', lines: ['Checking packet compliance']
  }
  api.syncPrReviewIssueRoom(record)
  api.syncPrReviewIssueRoom(record)
  const room = rooms()['BIZ-9999']
  const observer = room.members.find(member => member.name === 'chatgpt-review')
  assert.equal(observer.externalWorker, 'pr-review')
  assert.equal(room.externalWorkers.prReview.prNumber, 42)
  assert.equal(room.log.filter(entry => /joined to review PR #42/.test(entry.text)).length, 1)
  assert.equal(room.log.filter(entry => entry.detail).length, 1)
})

test('PR observer rejects records without explicit issue, PR, or review identity', () => {
  const { api } = runtime()
  assert.equal(api.normalizePrReviewRecord({ pr_number: 42, review_id: 'x' }), null)
  assert.equal(api.normalizePrReviewRecord({ issue_key: 'BIZ-1', review_id: 'x' }), null)
  assert.equal(api.normalizePrReviewRecord({ issue_key: 'BIZ-1', pr_number: 42 }), null)
})
