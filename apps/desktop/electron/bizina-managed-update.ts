import path from 'node:path'

export const BIZINA_STABLE_BRANCH = 'biab-208-v020-20260819'

export function managedUpdaterPath(updateRoot: string): string {
  return path.join(updateRoot, 'scripts', 'bizina-managed_update.py')
}

export function isManagedBizinaBranch(branch: string, scriptExists: boolean): boolean {
  return scriptExists && branch === BIZINA_STABLE_BRANCH
}

export interface ManagedPrepareRecord {
  status: 'conflicts' | 'prepared'
  candidate_root: string
  conflicts?: string[]
}

export function parseManagedPrepareRecord(stdout: string): ManagedPrepareRecord | null {
  try {
    const parsed = JSON.parse(stdout)
    if (!parsed || !['conflicts', 'prepared'].includes(parsed.status) || typeof parsed.candidate_root !== 'string') {
      return null
    }
    return {
      status: parsed.status,
      candidate_root: parsed.candidate_root,
      conflicts: Array.isArray(parsed.conflicts) ? parsed.conflicts.map(String) : []
    }
  } catch {
    return null
  }
}

export function managedNextCommand(record: ManagedPrepareRecord): string {
  return record.status === 'prepared'
    ? `hermes-safe-update --verify ${JSON.stringify(record.candidate_root)}`
    : `open ${JSON.stringify(record.candidate_root)}`
}
