import { createHash } from 'node:crypto';

// An advisory for a deliberately small class of replies. Unrecognized cases are silent.
function explicitFileRequest(text) {
  if (text.length > 2000 || /[?？]|不要|暂不|无需|不用|如何|怎么|示例|举例|\b(?:example|how to|do not|don't|instead of)\b/i.test(text)) return false;
  const zh = /(?:^|[。！\n，,])\s*(?:请|麻烦)?(?:帮我|给我|为我)?(?:根据[^，。\n]{1,60}[，,]?\s*)?(?:生成|创建|整理成|写出|写|撰写|输出|导出|制作|提供)(?:[^。！？\n]{0,65})(?:文档|文件|报告|表格)/u;
  const en = /^(?:please\s+)?(?:create|generate|write|export|produce)\s+(?:a |an |the )?[^.!?\n]{0,70}\b(?:document|report|file|spreadsheet)\b/i;
  return zh.test(text.trim()) || en.test(text.trim());
}

function planOnly(text) {
  if (!text || text.length > 360 || /[\n`#\[\]:：<>]/.test(text)) return false;
  const sentences = text.trim().split(/[.!。！]+/u).map(s => s.trim()).filter(Boolean);
  const nextAction = /^(?:(?:我(?:会|将|准备|需要|打算)|让我)(?:先|接下来)?(?:查阅|读取|查看|检查|调用|生成|整理|撰写|创建|编写)|(?:let me|i will|i'll|i need to|i must) (?:read|inspect|check|call|request|use|write|create|generate)\b)[^，,；;。!?！？]{0,110}$/iu;
  const unavailable = /^(?:right, )?(?:bash|the shell) (?:isn't|is not) available$/i;
  return sentences.some(s => nextAction.test(s)) && sentences.every(s => nextAction.test(s) || unavailable.test(s));
}

function textOf(message) {
  return message.content.filter(b => b.type === 'text').map(b => b.text).join('\n');
}

function review(session, end, isManaged) {
  let request, final;
  // Read only the current short turn. Any ambiguity, tool action, or earlier answer abstains.
  for (let seq = end.seq - 1; seq >= 0 && end.seq - seq <= 128; seq--) {
    const e = session.eventAt(seq);
    if (!e || e.type === 'turn/end' || e.type === 'tool/result' || e.type === 'assistant/attempt') return;
    if (e.type === 'turn/start') {
      if (e.data.turn !== end.data.turn || !request || !final || request.seq >= final.seq) return;
      const msg = final.data.message;
      if (final.data.turn !== end.data.turn || msg.source?.kind !== 'model' || !isManaged(msg.source.provider, msg.source.model)) return;
      if (!explicitFileRequest(textOf(request.data)) || !planOnly(textOf(msg))) return;
      const sessionRef = createHash('sha256').update(String(session.id)).digest('hex').slice(0, 16);
      return { type: 'delivery-review', code: 'A6_DELIVERY_UNVERIFIED', rule: 'explicit-file-plan-only',
        requestRef: `${sessionRef}/${request.seq}`, turn: end.data.turn, step: final.data.step };
    }
    if (e.type === 'assistant/message') {
      if (final || !Array.isArray(e.data.message?.content) || e.data.message.content.some(b => !['text','reasoning'].includes(b.type))) return;
      final = e;
    }
    if (e.type === 'user/message' && e.data.source?.kind === 'user') {
      if (request || !Array.isArray(e.data.content)) return;
      request = e;
    }
  }
}

export function installDeliveryCheck(ctx, { store, isManaged }) {
  if (typeof isManaged !== 'function') throw new TypeError('isManaged(provider, model) is required');
  return ctx.on('session/event', (session, event) => {
    if (event.type !== 'turn/end' || event.data.reason?.kind !== 'completed') return;
    const result = review(session, event, isManaged);
    if (result) store.logEvent(result);
  });
}
