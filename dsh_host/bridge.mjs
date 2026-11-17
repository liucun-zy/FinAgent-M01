// The only subprocesses owned by this preset have fixed executable/argument prefixes.
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'

export const hostDir = fileURLToPath(new URL('.', import.meta.url))
const repo = resolve(hostDir, '..')
export const python = process.env.DSH_PYTHON || resolve(repo, '.venv/bin/python')

export async function pythonJson(argv, payload, signal) {
  signal?.throwIfAborted()
  return new Promise((accept, reject) => {
    const proc = spawn(python, argv, { cwd: repo, stdio: ['pipe', 'pipe', 'pipe'] })
    const stdout = []
    const stderr = []
    const onAbort = () => proc.kill('SIGTERM')
    signal?.addEventListener('abort', onAbort, {once: true})
    proc.stdout.on('data', chunk => stdout.push(chunk))
    proc.stderr.on('data', chunk => stderr.push(chunk))
    proc.on('error', reject)
    proc.stdin.on('error', () => {}) // close owns final protocol/error reporting
    proc.on('close', code => {
      signal?.removeEventListener('abort', onAbort)
      if (signal?.aborted) return reject(signal.reason)
      if (code !== 0) {
        // ModelClient writes redacted failure details in its own protected audit.
        // A raw provider exception can contain input, headers or credentials.
        const detail = argv[0] === '-m' ? Buffer.concat(stderr).toString().slice(-800) : 'see model audit'
        return reject(new Error('Python service exited ' + code + ': ' + detail))
      }
      try { accept(JSON.parse(Buffer.concat(stdout).toString('utf8'))) }
      catch { reject(new Error('Python service returned an invalid JSON envelope')) }
    })
    proc.stdin.end(JSON.stringify(payload) + '\n')
  })
}

export async function backend(workspace, op, args = {}, signal) {
  const envelope = await pythonJson(['-m','action_trace_demo.dsh_bridge','--workspace',workspace], {op,args}, signal)
  if (envelope.ok !== true) throw new Error(typeof envelope.error === 'string' ? envelope.error : JSON.stringify(envelope.error))
  return envelope.result
}
