import assert from "node:assert/strict";
import { createHash, webcrypto } from "node:crypto";

if (!globalThis.crypto) globalThis.crypto = webcrypto;

let listener = null;
globalThis.chrome = {
  runtime: {
    onMessage: {
      addListener(callback) {
        listener = callback;
      },
    },
  },
  tabs: {
    async query() {
      return [{ id: 42, active: true, url: "https://solo2.jzxhnh.com/app/submissions" }];
    },
  },
  scripting: {
    async executeScript({ func, args }) {
      const previousDocument = globalThis.document;
      globalThis.document = { cookie: "solo_qa_csrf=csrf-test" };
      try {
        return [{ result: await func(...args) }];
      } finally {
        globalThis.document = previousDocument;
      }
    },
  },
};

const trace = new Blob(['{"type":"result","result":"done"}\n'], { type: "application/x-ndjson" });
const traceBytes = Buffer.from(await trace.arrayBuffer());
const traceDigest = createHash("sha256").update(traceBytes).digest("hex");
const localStates = [];
const localSyncs = [];
const requests = [];
let createdCount = 0;
let transientRepairFailures = 0;
let uploadValidationFailures = 0;
let promptHistoryBootstrapRequired = false;

function shanghaiDayKey(value = new Date()) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(value);
  const fields = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${fields.year}-${fields.month}-${fields.day}`;
}

const todayKey = shanghaiDayKey();
const yesterdayKey = shanghaiDayKey(
  new Date(new Date(`${todayKey}T12:00:00+08:00`).getTime() - 24 * 60 * 60 * 1000),
);

function jsonResponse(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

globalThis.fetch = async (url, options = {}) => {
  const href = String(url);
  requests.push({
    href,
    method: options.method || "GET",
    credentials: options.credentials || "",
    headers: Object.fromEntries(new Headers(options.headers || {}).entries()),
  });
  const payloadMatch = href.match(/\/api\/solo-qa\/turns\/(abc123abc123|def456def456|fed789fed789)\/1\/payload$/);
  if (payloadMatch) {
    const runId = payloadMatch[1];
    const suffix = runId === "abc123abc123" ? "one" : (runId === "def456def456" ? "two" : "fix");
    return jsonResponse({
      key: `${runId}:1`,
      values: {
        "User Prompt": `完成真实提交链路 ${suffix}`,
        "SessionID": `session-${suffix}`,
        "TurnID/PromptID": `turn-${suffix}`,
        "当前对话轮次排序": 1,
      },
      payload_sha256: "a".repeat(64),
      trajectory: {
        name: "trace.jsonl",
        size: trace.size,
        sha256: traceDigest,
        url: `http://127.0.0.1:8765/api/solo-qa/turns/${runId}/1/trajectory`,
      },
      solo_qa: runId === "fed789fed789"
        ? {
          state: "local_changed",
          remote_id: "555",
          remote_status: "PENDING_FIX",
          qc_summary: "交付完整性描述与历史记录重复",
          payload_changed: true,
        }
        : { state: "not_submitted", remote_id: "" },
    });
  }
  if (/\/api\/solo-qa\/turns\/(abc123abc123|def456def456|fed789fed789)\/1\/trajectory$/.test(href)) {
    return new Response(trace, { status: 200 });
  }
  if (href.endsWith("/api/solo-qa/state")) {
    localStates.push(JSON.parse(options.body));
    return jsonResponse({ state: localStates.at(-1).state });
  }
  if (href.endsWith("/api/solo-qa/prompt-history-status")) {
    return jsonResponse({
      bootstrap_required: promptHistoryBootstrapRequired,
      bootstrapped: !promptHistoryBootstrapRequired,
      indexed_prompts: 120,
    });
  }
  if (href.endsWith("/api/solo-qa/sync")) {
    const body = JSON.parse(options.body);
    localSyncs.push(body);
    return jsonResponse({ matched: body.items.length, unmatched: 0 });
  }
  if (href.includes("/api/v1/submissions?page=1&page_size=20&keyword=")) {
    return jsonResponse({ items: [], meta: { total: 0 } });
  }
  if (href.endsWith("/api/v1/submissions?page=1&page_size=20")) {
    return jsonResponse({
      items: [
        { id: 901, submitted_at: `${todayKey}T09:15:00+08:00` },
        { id: 900, submitted_at: `${yesterdayKey}T23:59:00+08:00` },
      ],
      meta: { total: promptHistoryBootstrapRequired ? 2 : 146 },
    });
  }
  if (href.endsWith("/api/v1/submissions/901")) {
    return jsonResponse({
      id: 901,
      status: "QC_PASSED",
      submitted_at: `${todayKey}T09:15:00+08:00`,
      session_id: "session-history",
      turn_id: "turn-history",
      round_no: 2,
      user_prompt: "为陶坯称重流程增加批次复核",
      repo_url: "https://github.com/example/ceramic-review.git",
      repo_name: "ceramic-review",
      task_type: "Feature 迭代",
      score_delivery: 5,
      desc_delivery: "  本次交付核对了独有业务对象。  ",
      instruction_following: {
        score: 4,
        description: "第 2 轮逐项对照了题面约束。",
      },
      data: {
        score_planning: 5,
        desc_planning: "按三个真实阶段推进并记录状态。",
        score_reasoning: 4,
        desc_reasoning: "根据具体报错定位了根因。",
        score_execution: 4,
        desc_execution: "执行过程中发生一次有证据的返工。",
      },
      qc_result: {
        dedup_hits: [{
          field: "desc_delivery",
          submission_id: 812,
          similarity: 0.22,
          excerpt: "公共片段",
          unsafe: { cookie: "must-not-leak" },
        }],
      },
    });
  }
  if (href.endsWith("/api/v1/submissions/900")) {
    return jsonResponse({
      id: 900,
      status: "DISCARDED",
      submitted_at: `${yesterdayKey}T23:59:00+08:00`,
      session_id: "session-old",
      turn_id: "turn-old",
      round_no: 1,
      values: {
        user_prompt: "旧的陶坯称重批次复核题面",
        repo_url: "https://github.com/example/ceramic-review.git",
        task_type: "Feature 迭代",
      },
      qc_summary: "与同仓库历史题面语义雷同",
    });
  }
  if (href.endsWith("/api/v1/submissions/555") && (options.method || "GET") === "GET") {
    return jsonResponse({
      id: 555,
      status: "PENDING_FIX",
      session_id: "session-fix",
      turn_id: "turn-fix",
      round_no: 1,
      qc_summary: "交付完整性描述与历史记录重复",
    });
  }
  if (href.endsWith("/api/v1/submissions/form-schema")) {
    return jsonResponse({
      fingerprint: "schema-test",
      attachment_max_mb: 20,
      fields: [
        { field_key: "user_prompt", label: "User Prompt", field_type: "textarea", is_required: true },
        { field_key: "session_id", label: "SessionID", field_type: "text", is_required: true },
        { field_key: "turn_id", label: "TurnID/PromptID", field_type: "text", is_required: true },
        { field_key: "round_no", label: "当前对话轮次排序", field_type: "number", is_required: true },
        { field_key: "trace_file", label: "轨迹文件", field_type: "attachment", is_required: true },
      ],
    });
  }
  if (href.endsWith("/api/v1/submissions/upload")) {
    assert.equal(options.method, "POST");
    assert.ok(options.body instanceof FormData);
    if (uploadValidationFailures > 0) {
      uploadValidationFailures -= 1;
      return jsonResponse({ detail: "轨迹上传暂不可用" }, 422);
    }
    return jsonResponse({ name: "trace.jsonl", path: "uploads/trace.jsonl", size: trace.size });
  }
  if (href.endsWith("/api/v1/submissions") && options.method === "POST") {
    const body = JSON.parse(options.body);
    assert.equal(body.schema_fingerprint, "schema-test");
    assert.match(body.data.user_prompt, /^完成真实提交链路 (one|two)$/);
    assert.equal(body.data.trace_file[0].path, "uploads/trace.jsonl");
    createdCount += 1;
    return jsonResponse({ id: 122 + createdCount, status: "SUBMITTED", message: "提交成功" });
  }
  if (href.endsWith("/api/v1/submissions/555") && options.method === "PUT") {
    if (transientRepairFailures > 0) {
      transientRepairFailures -= 1;
      return jsonResponse({ detail: "502 Bad Gateway" }, 502);
    }
    const body = JSON.parse(options.body);
    assert.equal(body.schema_fingerprint, "schema-test");
    assert.equal(body.data.user_prompt, "完成真实提交链路 fix");
    assert.equal(body.data.trace_file[0].path, "uploads/trace.jsonl");
    assert.match(body.comment, /质检结论/);
    return jsonResponse({ id: 555, status: "SUBMITTED", message: "返修已提交" });
  }
  throw new Error(`unexpected request: ${href}`);
};

await import("../chrome-solo-qa-helper/background.js");
assert.equal(typeof listener, "function");

const response = await new Promise((resolve) => {
  const asynchronous = listener(
    {
      type: "SOLO_QA_SUBMIT",
      payload: { turn_keys: ["abc123abc123:1", "def456def456:1"] },
    },
    { url: "http://127.0.0.1:8765/#exports" },
    resolve,
  );
  assert.equal(asynchronous, true);
});

assert.equal(response.ok, true);
assert.equal(response.data.results[0].outcome, "submitted");
assert.equal(response.data.results[0].remote_id, "123");
assert.equal(response.data.results[1].outcome, "submitted");
assert.equal(response.data.results[1].remote_id, "124");
assert.deepEqual(
  localStates.map((item) => item.state),
  ["submitting", "qc_pending", "submitting", "qc_pending"],
);
assert.equal(requests.filter((item) => item.href.endsWith("/submissions/form-schema")).length, 1);
assert.equal(requests.filter((item) => item.href.endsWith("/submissions/upload")).length, 2);
assert.equal(
  requests.filter((item) => item.href.endsWith("/submissions") && item.method === "POST").length,
  2,
);
assert.equal(requests.filter((item) => /\/submissions\/(123|124)$/.test(item.href)).length, 0);
const remoteWrites = requests.filter((item) => item.href.startsWith("/api/v1/") && item.method === "POST");
assert.ok(remoteWrites.length >= 2);
assert.ok(remoteWrites.every((item) => item.credentials === "include"));
assert.ok(remoteWrites.every((item) => item.headers["x-csrf-token"] === "csrf-test"));

const repairResponse = await new Promise((resolve) => {
  const asynchronous = listener(
    {
      type: "SOLO_QA_REPAIR",
      payload: { turn_keys: ["fed789fed789:1"] },
    },
    { url: "http://127.0.0.1:8765/#exports" },
    resolve,
  );
  assert.equal(asynchronous, true);
});

assert.equal(repairResponse.ok, true);
assert.equal(repairResponse.data.results[0].outcome, "resubmitted");
assert.equal(repairResponse.data.results[0].remote_id, "555");
assert.equal(
  requests.filter((item) => item.href.endsWith("/submissions/555") && item.method === "PUT").length,
  1,
);
assert.equal(localStates.at(-1).state, "qc_pending");

transientRepairFailures = 1;
const retriedRepairResponse = await new Promise((resolve) => {
  listener(
    {
      type: "SOLO_QA_REPAIR",
      payload: { turn_keys: ["fed789fed789:1"] },
    },
    { url: "http://127.0.0.1:8765/#exports" },
    resolve,
  );
});
assert.equal(retriedRepairResponse.ok, true);
assert.equal(retriedRepairResponse.data.results[0].outcome, "resubmitted");
assert.equal(
  requests.filter((item) => item.href.endsWith("/submissions/555") && item.method === "PUT").length,
  3,
);

uploadValidationFailures = 1;
const continuedBatchResponse = await new Promise((resolve) => {
  listener(
    {
      type: "SOLO_QA_SUBMIT",
      payload: { turn_keys: ["abc123abc123:1", "def456def456:1"] },
    },
    { url: "http://127.0.0.1:8765/#exports" },
    resolve,
  );
});
assert.equal(continuedBatchResponse.ok, true);
assert.equal(continuedBatchResponse.data.results.length, 2);
assert.equal(continuedBatchResponse.data.results[0].outcome, "failed");
assert.equal(continuedBatchResponse.data.results[1].outcome, "submitted");
assert.equal(continuedBatchResponse.data.failed, 1);
assert.equal(continuedBatchResponse.data.stopped, false);

const syncResponse = await new Promise((resolve) => {
  const asynchronous = listener(
    { type: "SOLO_QA_SYNC", payload: {} },
    { url: "http://127.0.0.1:8765/#exports" },
    resolve,
  );
  assert.equal(asynchronous, true);
});

assert.equal(syncResponse.ok, true);
assert.equal(syncResponse.data.matched, 1);
assert.equal(syncResponse.data.remote_total, 1);
assert.equal(syncResponse.data.account_total, 146);
assert.equal(syncResponse.data.scope_date, todayKey);
assert.equal(syncResponse.data.partial, false);
assert.equal(localSyncs.length, 1);
assert.equal(localSyncs[0].complete, false);
assert.equal(localSyncs[0].items.length, 1);
assert.equal("remote_ids" in localSyncs[0], false);
assert.equal(
  requests.some((item) => item.href.endsWith("/api/v1/submissions/900")),
  false,
);
assert.equal(
  requests.some((item) => item.href.includes("/api/v1/submissions?page=2")),
  false,
);
const synced = localSyncs[0].items[0];
assert.deepEqual(synced.delivery, {
  score: 5,
  description: "本次交付核对了独有业务对象。",
});
assert.deepEqual(synced.instruction, {
  score: 4,
  description: "第 2 轮逐项对照了题面约束。",
});
assert.equal(synced.planning.description, "按三个真实阶段推进并记录状态。");
assert.equal(synced.user_prompt, "为陶坯称重流程增加批次复核");
assert.equal(synced.repo_url, "https://github.com/example/ceramic-review.git");
assert.equal(synced.task_type, "Feature 迭代");
assert.equal(synced.reasoning.score, 4);
assert.equal(synced.execution.score, 4);
assert.equal(synced.dedup_hits.length, 1);
assert.equal(synced.dedup_hits[0].submission_id, 812);
assert.equal("unsafe" in synced.dedup_hits[0], false);

promptHistoryBootstrapRequired = true;
const syncCountBeforeBootstrap = localSyncs.length;
const bootstrapResponse = await new Promise((resolve) => {
  listener(
    { type: "SOLO_QA_SYNC", payload: {} },
    { url: "http://127.0.0.1:8765/#exports" },
    resolve,
  );
});
assert.equal(bootstrapResponse.ok, true);
assert.equal(bootstrapResponse.data.full_history, true);
assert.equal(bootstrapResponse.data.scope_date, "全部历史");
assert.equal(bootstrapResponse.data.remote_total, 2);
assert.equal(localSyncs.length, syncCountBeforeBootstrap + 2);
assert.equal(localSyncs.at(-2).items.length, 2);
assert.equal(localSyncs.at(-1).history_bootstrap_complete, true);
assert.equal(localSyncs.at(-2).items.find((item) => item.id === 900).user_prompt,
  "旧的陶坯称重批次复核题面");
