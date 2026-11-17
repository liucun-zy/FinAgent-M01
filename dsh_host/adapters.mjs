import { resolve } from 'node:path'
import { LlmAdapter, attributionHeaders, resolveRetryPolicy } from '@deepseek-ai/dsh-llm'
import { pythonJson, hostDir } from './bridge.mjs'

function wireMessages(messages) {
  return messages.map(message => {
    const text = message.content.filter(block => block.type === 'text').map(block => block.text).join('\n')
    const result = {role: message.role, content: text}
    if (message.role === 'tool') result.tool_call_id = message.toolCallId
    if (message.role === 'assistant') {
      const calls = message.content.filter(block => block.type === 'tool-call')
      if (calls.length) result.tool_calls = calls.map(block => ({
        id:block.id, type:'function', function:{name:block.name, arguments:block.arguments}
      }))
    }
    return result
  })
}

// Script references are resolved from observed DSH tool outcomes, never from the DAG by a side channel.
function resolveReferences(value, outcomes) {
  if (Array.isArray(value)) return value.map(item => resolveReferences(item, outcomes))
  if (!value || typeof value !== 'object') return value
  if (Object.keys(value).length === 1 && typeof value.$ref === 'string') {
    let target = outcomes
    for (const key of value.$ref.split('.')) {
      if (target == null || !Object.hasOwn(target, key)) throw new Error('Unresolved scripted reference '+value.$ref)
      target = target[key]
    }
    return target
  }
  return Object.fromEntries(Object.entries(value).map(([key, child]) => [key,resolveReferences(child,outcomes)]))
}

export function continuationHint(choice) {
  if (choice.finish_reason !== 'length') return null
  return 'The previous model response reached its output-token limit. Check the latest stored tool result '
    + 'and artifact_list or artifact_read before continuing. Send one complete smaller tool call per step. '
    + 'Maintain large workbooks in batches using the latest proposal_id with artifact_repair. '
    + 'Specify before:null for new cells and exact before-images for existing cells.'
}

export class CandidateAdapter extends LlmAdapter {
  constructor({script, configPath, logDir, limit, state, mode}) {
    super()
    Object.assign(this,{script,configPath,logDir,limit,state,mode,calls:0})
  }
  providerRetryPolicy() {
    // Python ModelClient accounts for and retries individual transport attempts.
    return resolveRetryPolicy({mode:'normal',maxRetries:0}, 'action-trace model adapter')
  }
  canRequest() {
    return this.calls < this.limit && (!this.script || this.calls < this.script.length)
  }
  async *stream(options) {
    if (!this.canRequest()) throw new Error('Candidate model or scripted action budget exhausted before committed submission')
    const step = this.calls++
    let response
    if (this.script) {
      const action = this.script[step]
      if (action?.response) {
        response = action.response
      } else {
      if (!action?.name || !action.arguments || typeof action.arguments !== 'object') throw new Error('Invalid scripted action at step '+step)
      response = {choices:[{message:{tool_calls:[{id:'script_'+step, function:{
        name:action.name,arguments:JSON.stringify(resolveReferences(action.arguments,this.state.outcomes))
      }}]}, finish_reason:'tool_calls'}]}
      }
    } else {
      const messages = wireMessages(options.messages)
      if (options.system) messages.unshift({role:'system',content:options.system})
      if (this.nextHint) messages.push({role:'user',content:this.nextHint})
      if (step + 1 === this.limit) messages.push({role:'user',content:this.mode === 'ingest'
        ? 'This is the last extraction request. Save supported source claims through save_extraction; if a partial receipt exists, correct only its rejected claims. Earlier accepted facts remain saved. Record unresolved work without replacing accepted facts.'
        : this.mode === 'm03_maintenance'
        ? 'This is the last maintenance request. Submit direct business fields in each after state and separate input dispositions through commit_maintenance. Do not use copy/set wrappers. Repair only unfinished scope using previous_receipt_id if needed. No budget is reset.'
        : 'This is the last model request in this session. Commit through the terminal tool if its evidence requirements are met.'})
      const tools = (options.tools || []).map(({name,description,parameters}) => ({type:'function',function:{name,description,parameters}}))
      const receipt = await pythonJson([resolve(hostDir,'model_call.py'),this.configPath,this.logDir], {
        messages, tools, attribution:attributionHeaders(), task:this.mode === 'm03_maintenance' ? 'dsh_m03_maintenance' : undefined
      }, options.signal)
      if (receipt.budget_error) throw new Error(receipt.budget_error)
      response = receipt.completion
      this.state.usage = receipt.usage
    }
    const choice = response?.choices?.[0]
    if (!choice?.message) throw new Error('Model returned no assistant message')
    if ((this.mode === 'ingest' || this.mode === 'm01_semantic') && choice.finish_reason === 'length') {
      throw new Error('Extraction output was truncated; this response was not saved. Previously accepted claims remain saved; unresolved claims stay pending.')
    }
    if (this.mode === 'm03_maintenance' && choice.finish_reason === 'length') {
      throw new Error('Maintenance output was truncated; this proposal was not submitted. Earlier receipts, if any, remain valid.')
    }
    this.nextHint = this.mode === 'ingest' || this.mode === 'm01_semantic' || this.mode === 'm03_maintenance' ? null : continuationHint(choice)
    let index = 0
    if (choice.message.content) {
      const text = choice.message.content
      if (typeof text !== 'string') throw new Error('Expected a text completion')
      yield {type:'block-start',index,blockType:'text'}
      yield {type:'text-delta',index,text}
      yield {type:'block-end',index,block:{type:'text',text}}
      index++
    }
    for (const call of choice.message.tool_calls || []) {
      const {id, function:fn} = call
      if (!id || !fn?.name) throw new Error('Malformed tool call')
      // Provider envelope differences are normalized once here, never guessed
      // recursively by the business executor (a string change_sets stays invalid).
      const argumentsText = typeof fn.arguments === 'string' ? fn.arguments
        : fn.arguments && typeof fn.arguments === 'object' && !Array.isArray(fn.arguments)
        ? JSON.stringify(fn.arguments) : null
      if (argumentsText === null) throw new Error('Malformed tool arguments envelope')
      yield {type:'block-start',index,blockType:'tool-call'}
      yield {type:'tool-call-delta',index,id,name:fn.name,argumentsDelta:argumentsText}
      yield {type:'block-end',index,block:{type:'tool-call',id,name:fn.name,arguments:argumentsText}}
      index++
    }
    if (response.usage && Number.isFinite(response.usage.prompt_tokens) && Number.isFinite(response.usage.completion_tokens)) {
      const cached = response.usage.prompt_tokens_details?.cached_tokens || 0
      yield {type:'usage',usage:{inputTokens:response.usage.prompt_tokens-cached, outputTokens:response.usage.completion_tokens,cacheReadTokens:cached}}
    }
    yield {type:'finish',reason:{kind:choice.message.tool_calls?.length ? 'tool-calls' : choice.finish_reason==='length' ? 'max-tokens' : 'stop'}}
  }
}
