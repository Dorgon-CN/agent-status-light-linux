// Read-only bridge: subscribes to the local Paseo daemon and prints agent state
// to stdout as JSON lines for status_light.py. Diagnostics go to stderr.
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { DaemonClient } from "@getpaseo/client/internal/daemon-client";

const PARENT_AGENT_ID_LABEL = "paseo.parent-agent-id";
const RETRY_MIN_MS = 2000;
const RETRY_MAX_MS = 15000;
const EMIT_DEBOUNCE_MS = 80;
const SUBAGENT_EVENT = "agent.provider_subagents.update";
// Statuses whose agents are loaded in the daemon; listing subagents of other
// agents would make the daemon load them.
const LOADED_STATUSES = new Set(["initializing", "idle", "running", "error"]);

const PASEO_HOME = process.env.PASEO_HOME || join(homedir(), ".paseo");
const CLIENT_ID = `agent-status-light-${randomUUID()}`;

// The SDK logs info messages with console.log; keep stdout for the protocol only.
console.log = (...args) => console.error(...args);
const logger = {
  debug: () => {},
  info: () => {},
  warn: (obj, msg) => console.error("[warn]", msg, obj?.err?.message ?? ""),
  error: (obj, msg) => console.error("[error]", msg, obj?.err?.message ?? ""),
};

function send(message) {
  process.stdout.write(JSON.stringify(message) + "\n");
}

function resolveUrl() {
  if (process.env.ASL_PASEO_URL) return process.env.ASL_PASEO_URL;
  let listen = "127.0.0.1:6767";
  try {
    const config = JSON.parse(readFileSync(join(PASEO_HOME, "config.json"), "utf8"));
    const value = config?.daemon?.listen;
    if (typeof value === "string" && value.trim()) listen = value.trim();
  } catch {
    // Missing or unreadable config: fall back to the default port.
  }
  const match = /^\[?([^\]]*?)\]?:(\d+)$/.exec(listen);
  if (!match) return "ws://127.0.0.1:6767/ws";
  let host = match[1];
  if (!host || host === "0.0.0.0" || host === "::") host = "127.0.0.1";
  if (host.includes(":")) host = `[${host}]`;
  return `ws://${host}:${match[2]}/ws`;
}

function normalizeAgent(agent) {
  const parent = agent.labels?.[PARENT_AGENT_ID_LABEL];
  return {
    id: agent.id,
    title: agent.title ?? null,
    provider: agent.provider,
    status: agent.status,
    pendingPermissions: Array.isArray(agent.pendingPermissions) ? agent.pendingPermissions.length : 0,
    requiresAttention: agent.requiresAttention === true,
    attentionReason: agent.attentionReason ?? null,
    attentionTimestamp: agent.attentionTimestamp ?? null,
    parentAgentId: typeof parent === "string" && parent.trim() ? parent.trim() : null,
    sessionId: agent.persistence?.sessionId ?? null,
    workspaceId: agent.workspaceId ?? null,
    cwd: agent.cwd ?? null,
    lastError: typeof agent.lastError === "string" ? agent.lastError.slice(0, 300) : null,
    updatedAt: agent.updatedAt ?? null,
  };
}

function isTracked(agent) {
  return agent && !agent.internal && !agent.archivedAt;
}

const url = resolveUrl();
const agents = new Map();
// Provider-owned subagents (e.g. Claude Code's Agent tool): parent id -> (subagent id -> status).
const subagents = new Map();
const seededParents = new Set();
let providers = {};
let serverId = null;
let emitTimer = null;

function emitState() {
  emitTimer = null;
  send({
    type: "state",
    serverId,
    providers,
    agents: Array.from(agents.values(), (agent) => ({
      ...agent,
      runningSubagents: countRunningSubagents(agent.id),
    })),
  });
}

function scheduleEmit() {
  if (!emitTimer) emitTimer = setTimeout(emitState, EMIT_DEBOUNCE_MS);
}

function countRunningSubagents(parentId) {
  let count = 0;
  for (const status of subagents.get(parentId)?.values() ?? []) if (status === "running") count++;
  return count;
}

function setSubagent(parentId, subagentId, status) {
  let entries = subagents.get(parentId);
  if (!entries) subagents.set(parentId, (entries = new Map()));
  entries.set(subagentId, status);
}

function applySubagentEvent(message) {
  if (message?.type !== SUBAGENT_EVENT) return;
  const payload = message.payload;
  if (payload.kind === "upsert") {
    setSubagent(payload.subagent.parentAgentId, payload.subagent.id, payload.subagent.status);
  } else if (payload.kind === "remove") {
    subagents.get(payload.parentAgentId)?.delete(payload.subagentId);
  } else {
    return;
  }
  scheduleEmit();
}

// Events only report changes, so load the current list once per loaded parent.
function seedSubagents(client, agent) {
  if (!isTracked(agent) || seededParents.has(agent.id) || !LOADED_STATUSES.has(agent.status)) return;
  seededParents.add(agent.id);
  client
    .listProviderSubagents(agent.id)
    .then((payload) => {
      const known = subagents.get(agent.id);
      for (const subagent of payload.subagents ?? []) {
        if (!known?.has(subagent.id)) setSubagent(agent.id, subagent.id, subagent.status);
      }
      scheduleEmit();
    })
    .catch((error) => {
      seededParents.delete(agent.id);
      console.error("[warn] subagent list failed:", agent.id, error?.message ?? error);
    });
}

function applySnapshot(entries) {
  agents.clear();
  for (const entry of entries ?? []) {
    const agent = entry?.agent;
    if (isTracked(agent)) agents.set(agent.id, normalizeAgent(agent));
  }
  scheduleEmit();
}

function applyUpdate(message) {
  if (message?.type !== "agent_update") return;
  const payload = message.payload;
  if (payload.kind === "upsert") {
    if (isTracked(payload.agent)) agents.set(payload.agent.id, normalizeAgent(payload.agent));
    else agents.delete(payload.agent.id);
  } else if (payload.kind === "remove") {
    agents.delete(payload.agentId);
  }
  scheduleEmit();
}

async function loadProviders(client) {
  try {
    const snapshot = await client.getProvidersSnapshot();
    const next = {};
    for (const entry of snapshot.entries ?? []) {
      next[entry.provider] = { label: entry.label ?? entry.provider, enabled: entry.enabled !== false };
    }
    providers = next;
    scheduleEmit();
  } catch (error) {
    console.error("[warn] provider snapshot failed:", error?.message ?? error);
  }
}

// One connection attempt; resolves when the connection ends for any reason.
async function runConnection() {
  const client = new DaemonClient({
    url,
    clientId: CLIENT_ID,
    clientType: "cli",
    // Subagent status upserts are enough; skip their streamed timeline items.
    capabilities: { projected_subagent_timeline: false },
    logger,
    connectTimeoutMs: 8000,
    reconnect: { enabled: false },
  });
  let observation = null;
  let events = null;
  let finished;
  const done = new Promise((resolve) => {
    finished = resolve;
  });
  const unsubscribeStatus = client.subscribeConnectionStatus((state) => {
    if (state.status === "disconnected" || state.status === "disposed") finished(state.reason ?? state.status);
  });
  try {
    send({ type: "connection", status: "connecting", url });
    await client.connect();
    serverId = client.lastServerInfoMessage?.serverId ?? null;
    events = client.observeEvents([SUBAGENT_EVENT]);
    events.subscribe({
      snapshot: () => {},
      update: applySubagentEvent,
      error: (error) => finished(error?.message ?? "event subscription failed"),
    });
    await events.ready;
    observation = client.observeAgents({ filter: { includeArchived: false }, page: { limit: 200 } });
    observation.subscribe({
      snapshot: (payload) => {
        if (payload?.pageInfo?.hasMore) console.error("[warn] more than 200 agents; extra pages are not tracked");
        applySnapshot(payload?.entries);
      },
      update: (message) => {
        applyUpdate(message);
        if (message?.type === "agent_update" && message.payload.kind === "upsert") {
          seedSubagents(client, message.payload.agent);
        }
      },
      error: (error) => finished(error?.message ?? "subscription failed"),
    });
    await observation.ready;
    send({ type: "connection", status: "connected", url, serverId });
    for (const agent of agents.values()) seedSubagents(client, agent);
    void loadProviders(client);
    const reason = await done;
    return { connected: true, reason };
  } catch (error) {
    return { connected: false, reason: error?.message ?? String(error) };
  } finally {
    unsubscribeStatus();
    if (emitTimer) {
      clearTimeout(emitTimer);
      emitTimer = null;
    }
    await observation?.release().catch(() => {});
    await events?.release().catch(() => {});
    await client.close().catch(() => {});
  }
}

async function main() {
  // Exit with the parent: status_light.py owns this process through a pipe.
  process.stdin.on("end", () => process.exit(0));
  process.stdin.on("error", () => process.exit(0));
  process.stdin.resume();
  let delay = RETRY_MIN_MS;
  for (;;) {
    const { connected, reason } = await runConnection();
    agents.clear();
    subagents.clear();
    seededParents.clear();
    if (connected) delay = RETRY_MIN_MS;
    const nextRetryAt = new Date(Date.now() + delay).toISOString();
    send({ type: "connection", status: "disconnected", url, reason, nextRetryAt });
    await new Promise((resolve) => setTimeout(resolve, delay));
    delay = Math.min(delay * 2, RETRY_MAX_MS);
  }
}

main();
