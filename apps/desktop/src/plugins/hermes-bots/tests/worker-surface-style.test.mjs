import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function between(start, end) {
  const from = source.indexOf(start)
  const to = source.indexOf(end, from)
  assert.ok(from >= 0 && to > from)
  return source.slice(from, to)
}

test('Cody workspace renders a scrollable threaded daily history', () => {
  const view = between('function StudioFleetCodyMainView()', 'function closeStudioFleetCodyWorkspace')
  const thread = between('function CodyJobThread', 'function StudioFleetCodyMainView()')
  assert.match(view, /bg-\(--ui-bg-editor\)/)
  assert.match(view, /Today’s work/)
  assert.match(view, /visibleHistory\.map/)
  assert.match(view, /hidden · Restore/)
  assert.match(view, /flex h-full min-h-0 flex-col overflow-hidden/)
  assert.match(view, /min-h-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-contain/)
  assert.match(view, /flex shrink-0 items-center justify-between/)
  assert.match(thread, /bg-\(--ui-bg-elevated\)/)
  assert.match(thread, /font-mono text-\[0\.75rem\]/)
  assert.match(thread, /break-words whitespace-pre-wrap text-left/)
  assert.match(thread, /aria-expanded/)
  assert.match(thread, /onDismiss\(job\.id\)/)
  assert.match(thread, /Hide this job from today’s view/)
  assert.doesNotMatch(view, /bg-\(--ui-bg-secondary\)/)
})

test('CCD workspace uses a proportional neutral reading surface', () => {
  const view = between('function CcdMainView()', 'function closeCcdWorkspace')
  assert.match(view, /bg-\(--ui-bg-editor\)/)
  assert.match(view, /bg-\(--ui-bg-elevated\)/)
  assert.match(view, /text-\[0\.8125rem\] leading-6/)
  assert.match(view, /whitespace-pre-wrap text-left/)
  assert.match(view, /flex h-full min-h-0 flex-col overflow-hidden/)
  assert.match(view, /min-h-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-contain/)
  assert.match(view, /flex shrink-0 items-center justify-between/)
  assert.doesNotMatch(view, /bg-\(--ui-bg-secondary\)/)
  assert.doesNotMatch(view, /p-4 font-mono/)
})

test('external worker transcript lines discard terminal indentation', () => {
  assert.match(source, /studioFleetTranscriptTail[\s\S]*?\.map\(line => line\.trim\(\)\)/)
  assert.match(source, /raw\.lines\.map\(line => String\(line\)\.trim\(\)\)/)
})
