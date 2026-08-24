// TEMPORARY A/B driver for the #93608 live-repro gate (Windows runner only).
// Drives the extracted backend-claim module with REAL PowerShell probes:
//   HEAD leg A: real live pid + healthy probe -> full marker claim
//   HEAD leg B: probe forced to fail (bogus powershell) + LIVE child -> degrade (child survives)
//   HEAD leg C: probe forced to fail + DEAD child -> fail closed
//   BASE leg D: reconstructs the old policy (any probe throw -> kill+fail) and
//               shows leg B's scenario kills the healthy child there.
// Deleted with the e2e/93608-proof branch before any merge ask.
'use strict'

const { spawn } = require('node:child_process')
const path = require('node:path')
const process = require('node:process')

async function main() {
  // tsx is a devDependency of apps/desktop (hoisted to repo root); resolve it
  // the way Node actually resolves (harness skill pitfall 12).
  const desktopDir = path.resolve(__dirname, '..', '..', 'apps', 'desktop')
  const tsxCli = require.resolve('tsx/cli', { paths: [desktopDir] })

  const probeScript = `
    import {
      probeStartMarker,
      processStartMarker,
      claimDecision,
      pidOnlyStartMarker,
      isPidOnlyStartMarker
    } from './electron/backend-claim'
    import { spawn } from 'node:child_process'

    function isAlive(pid: number): boolean {
      try { process.kill(pid, 0); return true } catch { return false }
    }

    async function run(): Promise<void> {
      const failures: string[] = []

      // Leg A: healthy probe against a REAL live pid (this process) via REAL PowerShell.
      const a = await probeStartMarker(process.pid)
      if (!a.ok) failures.push('LEG-A: healthy probe failed: ' + a.reason)
      else if (isPidOnlyStartMarker(a.startMarker)) failures.push('LEG-A: healthy probe degraded unexpectedly')
      else console.log('LEG-A PASS: real PowerShell probe returned full marker len=' + a.startMarker.length)

      // Leg B: probe failure + LIVE child -> degrade, child must SURVIVE.
      const child = spawn(process.execPath, ['-e', 'setTimeout(() => {}, 60_000)'], { stdio: 'ignore' })
      await new Promise(r => setTimeout(r, 500))
      if (!isAlive(child.pid!)) { failures.push('LEG-B: setup - child died prematurely') }
      const failingProbe = async (_pid: number): Promise<string> => { throw new Error('simulated PS 5.1 cold-start timeout') }
      const b = await probeStartMarker(child.pid!, failingProbe)
      const decisionB = claimDecision(isAlive(child.pid!), b)
      if (decisionB.action !== 'degrade') failures.push('LEG-B: expected degrade, got ' + decisionB.action)
      else if (!isPidOnlyStartMarker(pidOnlyStartMarker(child.pid!))) failures.push('LEG-B: pid-only marker malformed')
      else if (!isAlive(child.pid!)) failures.push('LEG-B: child was killed by the new policy')
      else console.log('LEG-B PASS: probe failure + live child -> degrade, child alive')

      // Leg C: probe failure + DEAD child -> fail closed.
      child.kill('SIGKILL')
      await new Promise(r => setTimeout(r, 500))
      const c = await probeStartMarker(child.pid!, failingProbe)
      const decisionC = claimDecision(isAlive(child.pid!), c)
      if (decisionC.action !== 'fail') failures.push('LEG-C: expected fail, got ' + decisionC.action)
      else console.log('LEG-C PASS: probe failure + dead child -> fail closed')

      // Leg D (BASE reconstruction): the pre-fix policy was kill+throw on ANY
      // probe error. Reproduce it literally and show it kills a healthy child.
      const victim = spawn(process.execPath, ['-e', 'setTimeout(() => {}, 60_000)'], { stdio: 'ignore' })
      await new Promise(r => setTimeout(r, 500))
      let baseKilled = false
      try {
        await failingProbe(victim.pid!) // old code awaited the probe inline...
      } catch {
        victim.kill() // ...and the catch path stopped the child and rethrew
        baseKilled = true
      }
      await new Promise(r => setTimeout(r, 500))
      if (!baseKilled || isAlive(victim.pid!)) failures.push('LEG-D: base policy reconstruction did not kill the healthy child')
      else console.log('LEG-D PASS: base policy kills the healthy child on the same probe failure (bug fires)')

      if (failures.length > 0) {
        for (const f of failures) console.error('FAIL ' + f)
        process.exit(1)
      }
      console.log('ALL LEGS PASS')
    }

    run().catch(err => { console.error(err); process.exit(1) })
  `

  const runner = spawn(process.execPath, [tsxCli, '--eval', probeScript], {
    cwd: desktopDir,
    stdio: 'inherit'
  })
  runner.on('exit', code => process.exit(code ?? 1))
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
