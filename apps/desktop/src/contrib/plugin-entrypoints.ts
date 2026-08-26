export interface BundledPluginEntrypointCollision {
  key: string
  kept: string
  ignored: string
}

export interface BundledPluginEntrypointSelection<T> {
  modules: Record<string, T>
  collisions: BundledPluginEntrypointCollision[]
}

const EXTENSION_PRIORITY: Record<string, number> = {
  '.js': 0,
  '.ts': 1,
  '.tsx': 2
}

function entrypointKey(path: string): string {
  return path.replace(/\.(?:js|ts|tsx)$/, '')
}

function extensionPriority(path: string): number {
  const extension = path.match(/\.(?:js|ts|tsx)$/)?.[0] ?? ''
  return EXTENSION_PRIORITY[extension] ?? -1
}

/**
 * Vite's glob returns every matching extension. A generated plugin.js beside
 * plugin.tsx must never register the same plugin twice: prefer authored TSX,
 * then TS, while preserving standalone JS plugins as valid entrypoints.
 */
export function selectBundledPluginEntrypoints<T>(
  discovered: Record<string, T>
): BundledPluginEntrypointSelection<T> {
  const selected = new Map<string, [string, T]>()
  const collisions: BundledPluginEntrypointCollision[] = []

  for (const [path, module] of Object.entries(discovered).sort(([left], [right]) => left.localeCompare(right))) {
    const key = entrypointKey(path)
    const current = selected.get(key)

    if (!current) {
      selected.set(key, [path, module])
      continue
    }

    const [currentPath] = current
    const nextWins = extensionPriority(path) > extensionPriority(currentPath)
    const kept = nextWins ? path : currentPath
    const ignored = nextWins ? currentPath : path

    collisions.push({ key, kept, ignored })
    if (nextWins) {
      selected.set(key, [path, module])
    }
  }

  return {
    modules: Object.fromEntries([...selected.values()]),
    collisions
  }
}
