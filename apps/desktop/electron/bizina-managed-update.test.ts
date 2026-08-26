import { describe, expect, it } from 'vitest'

import {
  BIZINA_STABLE_BRANCH,
  isManagedBizinaBranch,
  managedNextCommand,
  managedUpdaterPath,
  parseManagedPrepareRecord
} from './bizina-managed-update'

describe('Bizina managed Desktop updates', () => {
  it('uses managed updates only on the exact stable branch with the repo script', () => {
    expect(isManagedBizinaBranch(BIZINA_STABLE_BRANCH, true)).toBe(true)
    expect(isManagedBizinaBranch('main', true)).toBe(false)
    expect(isManagedBizinaBranch(BIZINA_STABLE_BRANCH, false)).toBe(false)
  })

  it('resolves the managed updater inside the selected checkout', () => {
    expect(managedUpdaterPath('/opt/hermes')).toBe('/opt/hermes/scripts/bizina-managed_update.py')
  })

  it('parses strict prepare records and produces truthful next actions', () => {
    const prepared = parseManagedPrepareRecord(
      JSON.stringify({ status: 'prepared', candidate_root: '/tmp/next', conflicts: [] })
    )
    expect(prepared).not.toBeNull()
    expect(managedNextCommand(prepared!)).toMatch(/hermes-safe-update --verify/)

    const conflicts = parseManagedPrepareRecord(
      JSON.stringify({ status: 'conflicts', candidate_root: '/tmp/next', conflicts: ['plugin.js', 'styles.css'] })
    )
    expect(conflicts).not.toBeNull()
    expect(managedNextCommand(conflicts!)).toBe('open "/tmp/next"')
    expect(parseManagedPrepareRecord('{}')).toBeNull()
    expect(parseManagedPrepareRecord('not json')).toBeNull()
  })
})
