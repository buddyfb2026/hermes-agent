import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const styles = readFileSync(resolve(process.cwd(), 'src/styles.css'), 'utf8')
const botPlugin = readFileSync(resolve(process.cwd(), 'src/plugins/hermes-bots/plugin.js'), 'utf8')

describe('human turns stand out across chat surfaces', () => {
  it('tints only normal user bubbles with a theme-aware blue treatment', () => {
    expect(styles).toMatch(/\[data-slot='aui_user-message-root'\] \.composer-human-message,\s*\[data-group-user-message='true'\]/)
    expect(styles).toMatch(/background: color-mix\(in srgb, #60a5fa 20%, var\(--dt-user-bubble\)\)/)
    expect(styles).toMatch(/box-shadow: inset 3px 0 0/)
  })

  it('marks Group Chat human entries without marking member entries', () => {
    expect(botPlugin).toMatch(/'data-group-user-message': isUser \? 'true' : undefined/)
    expect(botPlugin).toMatch(/isUser \? 'rounded-md border px-2 py-1\.5' : 'px-2 py-1'/)
  })

  it('keeps the HUD bubble on the same blue-accent system', () => {
    expect(styles).toMatch(/--hud-bubble-fill: color-mix\(in srgb, #60a5fa 20%, var\(--dt-user-bubble\) 65%\)/)
  })
})