import { parentPort, workerData } from 'node:worker_threads';
import { parseCandidates } from './selector.js';

let payload;
try { payload = JSON.parse(workerData.text); } catch { /* handled below */ }
if (!payload || payload.success === false
  || (!Array.isArray(payload.data) && !Array.isArray(payload.data?.items) && !Array.isArray(payload))) {
  parentPort.postMessage(null);
} else {
  const { model, cfg, cacheRates, measuredRates, measuredStats, contextWindows } = workerData;
  const { candidates, total, rejected } = parseCandidates(payload, model, cfg, {
    cacheRates, measuredRates, measuredStats, contextWindows,
  });

  parentPort.postMessage({ total, rejected, candidates, eligible: candidates.length });
}
