import { Context } from '@deepseek-ai/cordis'
import Llm, {createUserMessage} from '@deepseek-ai/dsh-llm'
import AgentLoop from '@deepseek-ai/dsh-agent-loop'
import Agents from '@deepseek-ai/dsh-agent'
import Session from '@deepseek-ai/dsh-session'
import Projections from '@deepseek-ai/dsh-session-projection'
import Prompt from '@deepseek-ai/dsh-system-prompt'
import Tools from '@deepseek-ai/dsh-tools'
import * as NativeExecutor from '@deepseek-ai/dsh-tool-executor-native'
import {mkdirSync, readFileSync, appendFileSync, writeFileSync} from 'node:fs'
import {resolve} from 'node:path'
import {randomUUID} from 'node:crypto'
import {parseArgs} from 'node:util'
import {backend} from './bridge.mjs'
import {graphPlugin} from './plugin.mjs'
import {CandidateAdapter} from './adapters.mjs'

const {values:args} = parseArgs({options:{
  workspace:{type:'string'}, 'prompt-file':{type:'string'}, config:{type:'string'}, script:{type:'string'},
  'max-calls':{type:'string'}, 'audit-dir':{type:'string'}
}})
const jsonFile = path => JSON.parse(readFileSync(path,'utf8'))
async function main() {
  if (!args.workspace || !args['prompt-file'] || Boolean(args.config) === Boolean(args.script)) {
    throw new Error('Usage: run --workspace DIR --prompt-file FILE (--config FILE | --script FILE) [--max-calls N] [--audit-dir DIR]')
  }
  const workspace = resolve(args.workspace)
  const id = 'dsh-'+randomUUID()
  const logDir = resolve(args['audit-dir'] || resolve(workspace,'.dsh',id))
  mkdirSync(logDir,{recursive:true,mode:0o700})
  const eventsPath = resolve(logDir,'session.jsonl')
  // Refuse to merge independent sessions in an explicitly reused audit directory.
  writeFileSync(eventsPath,'',{flag:'wx',mode:0o600})
  const configPath = args.config && resolve(args.config)
  const config = configPath ? jsonFile(configPath) : {}
  const settings = typeof config.model === 'object' ? {...config,...config.model} : config
  const model = typeof settings.model === 'string' ? settings.model : settings.model_name || 'gpt-6-luna'
  const script = args.script ? jsonFile(args.script) : null
  if (args.script && (!Array.isArray(script) || !script.length)) throw new Error('Expect a non-empty array of scripted actions')
  const requestedLimit = Number(args['max-calls'] || script?.length || config.candidate_max_turns_per_stage || 35)
  if (!Number.isSafeInteger(requestedLimit) || requestedLimit < 1) throw new Error('max-calls must be a positive integer')
  const prompt = readFileSync(resolve(args['prompt-file']),'utf8')
  const {tools:catalog, request_id:requestId, mode, system_prompt:systemPrompt,
    max_model_requests:maxModelRequests} = await backend(workspace,'catalog')
  if (!requestId) throw new Error('Backend did not bind the tool catalog to a task request')
  const limit = mode === 'ingest' || mode === 'm01_semantic' || mode === 'm03_maintenance' ? Math.min(requestedLimit, maxModelRequests) : requestedLimit
  if (!Number.isSafeInteger(limit) || limit < 1) throw new Error('Invalid backend model request budget')
  const state = {outcomes:[], committed:null, error:null, usage:null}
  const adapter = new CandidateAdapter({script,configPath,logDir,limit,state,mode})
  const ctx = new Context()
  // This explicitly assembled preset mounts no filesystem, shell, code, MCP or child agent tools.
  for (const mod of [Session,Projections,Llm,Prompt,Tools,NativeExecutor,Agents,AgentLoop]) {
    await ctx.plugin(mod, mod === AgentLoop ? {agents:[],maxParallelToolCalls:1} : {})
  }
  const provider = script ? 'offline-actions' : 'efunds'
  ctx.llm.registerAdapter([provider], adapter)
  ctx.on('session/event', (session,event) => {
    if (session.id === id) appendFileSync(eventsPath,JSON.stringify(event)+'\n',{mode:0o600})
  })
  ctx.on('agent/error', ({error}) => { state.error = error?.message || String(error) })
  const handle = await ctx.agents.create({sessionId:id,meta:{cwd:workspace},agentOptions:{provider,model},
    setup: async scoped => { await scoped.plugin(graphPlugin({workspace,catalog,requestId,state,adapters:adapter,mode,systemPrompt})) }
  })
  try {
    handle.agent.followup(createUserMessage({source:{kind:'user'},content:[{type:'text',text:prompt}]}))
    await handle.agent.whenIdle()
    const result = {host:'deepseek-harness',dsh_version:'0.2.2-rc.1',session_id:id,
      committed:Boolean(state.committed || state.persisted), ...(state.committed || state.persisted),
      session_complete:Boolean(state.committed), model_source:provider, mode,
      steps:adapter.calls, tool_outcomes:state.outcomes.length, usage:state.usage, audit_dir:logDir}
    if (!state.committed) result.error = state.error || 'Session ended without a validated terminal tool receipt'
    writeFileSync(resolve(logDir,'receipt.json'),JSON.stringify(result,null,2)+'\n',{mode:0o600})
    return result
  } finally {
    try { await handle.dispose() } finally { await ctx.fiber.dispose() }
  }
}
try {
  const result = await main()
  process.stdout.write(JSON.stringify(result)+'\n')
  if (!result.committed) process.exitCode=1
} catch (error) {
  process.stderr.write('DSH host failed: '+String(error?.message || error)+'\n')
  process.exitCode=1
}
