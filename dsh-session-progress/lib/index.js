/**
 * dsh-session-progress — Host half.
 *
 * One contribution: a system-prompt section that tells the model to keep a
 * short plan with `todo_write`. The pill in the session header renders that
 * plan — the Session's `todos` projection — so the steps the user reads are the
 * model's own words rather than a guess folded out of tool names.
 *
 * No package dependencies: node builtins only, plus the Host's `systemPrompt`
 * service. When that service is absent this half never applies, which is
 * harmless: the Client half falls back to folding the turn's tool calls.
 */

export const name = 'dsh-session-progress'
export const inject = ['systemPrompt']

/**
 * Kept short on purpose. Every sentence here is paid for on every request, so
 * it states only what changes behaviour: when to write a plan, what one step
 * looks like, and when not to bother.
 */
const PROGRESS_SECTION = `## 进度栏

会话表头有一条进度栏，展示当前回合的步骤。它是给用户看的执行计划，不是内部草稿。

- 预计要三步以上的任务，动手前先调用 todo_write 写下 3–6 步；每完成一步立即更新状态。清单在每轮 turn 开始时清空，新一轮要重新写。
- 每一步是一句短标题：不超过 12 个字，动词开头（例如「读取插件现状」「收紧合并规则」），不写工具名，不写成段落。
- 步骤要覆盖整件事，不是每个工具调用：同一件事里的读文件、改文件、跑命令合成一步。
- 一步就能做完的小改动不必写清单，进度栏会自动显示本回合的轨迹摘要。
`

const SECTION_NAME = 'dsh-session-progress'

export function apply(ctx) {
  ctx.effect(
    () =>
      ctx.systemPrompt.section({
        name: SECTION_NAME,
        // Late in the prompt: this is display guidance, not a rule of the job.
        order: 640,
        text: PROGRESS_SECTION,
      }),
    'dsh-session-progress: progress section',
  )
}
