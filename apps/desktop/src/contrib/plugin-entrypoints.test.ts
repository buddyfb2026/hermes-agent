import { describe, expect, it } from 'vitest'

import { selectBundledPluginEntrypoints } from './plugin-entrypoints'

describe('bundled plugin entrypoint selection', () => {
  it('keeps standalone JavaScript plugins', () => {
    const js = { default: { id: 'runtime-compatible' } }

    expect(selectBundledPluginEntrypoints({ '../plugins/example/plugin.js': js })).toEqual({
      modules: { '../plugins/example/plugin.js': js },
      collisions: []
    })
  })

  it('prefers authored TSX over an emitted JavaScript twin regardless of discovery order', () => {
    const js = { default: { id: 'kanban-js' } }
    const tsx = { default: { id: 'kanban-tsx' } }

    const forward = selectBundledPluginEntrypoints({
      '../plugins/kanban/plugin.js': js,
      '../plugins/kanban/plugin.tsx': tsx
    })
    const reverse = selectBundledPluginEntrypoints({
      '../plugins/kanban/plugin.tsx': tsx,
      '../plugins/kanban/plugin.js': js
    })

    for (const result of [forward, reverse]) {
      expect(result.modules).toEqual({ '../plugins/kanban/plugin.tsx': tsx })
      expect(result.collisions).toEqual([
        {
          key: '../plugins/kanban/plugin',
          kept: '../plugins/kanban/plugin.tsx',
          ignored: '../plugins/kanban/plugin.js'
        }
      ])
    }
  })

  it('prefers TypeScript over JavaScript when no TSX entrypoint exists', () => {
    const js = { default: { id: 'plain-js' } }
    const ts = { default: { id: 'authored-ts' } }

    const result = selectBundledPluginEntrypoints({
      '../plugins/example/plugin.js': js,
      '../plugins/example/plugin.ts': ts
    })

    expect(result.modules).toEqual({ '../plugins/example/plugin.ts': ts })
    expect(result.collisions[0]).toMatchObject({
      kept: '../plugins/example/plugin.ts',
      ignored: '../plugins/example/plugin.js'
    })
  })
})
