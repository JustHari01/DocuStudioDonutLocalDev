"use strict";

function requestInput(value) {
  if (typeof value === "string") return value;
  if (!Array.isArray(value)) throw new Error("Responses input must be text");
  const chunks = [];
  for (const item of value) {
    if (typeof item === "string") chunks.push(item);
    else if (item && typeof item === "object") {
      const text = contentText(item.content);
      if (text) chunks.push(text);
    }
  }
  const result = chunks.join("\n\n");
  if (!result.trim()) throw new Error("Responses input must contain text");
  return result;
}

function contentText(value) {
  if (typeof value === "string") return value;
  if (!Array.isArray(value)) return "";
  const chunks = [];
  for (const part of value) {
    if (typeof part === "string") chunks.push(part);
    else if (part && typeof part === "object" && typeof part.text === "string") {
      chunks.push(part.text);
    }
  }
  return chunks.join("");
}

// Codex adds its own developer/environment context to a Responses request. The
// browser chat already owns the conversation history, so only forward the most
// recent human message. This keeps a pasted Master Prompt byte-for-byte intact
// and prevents Codex transport instructions from leaking into the saved chat.
function latestUserInput(value) {
  if (typeof value === "string") return value;
  if (!Array.isArray(value)) throw new Error("Responses input must be text");
  let latest = "";
  for (const item of value) {
    if (!item || typeof item !== "object" || item.role !== "user") continue;
    const text = contentText(item.content);
    if (text.trim()) latest = text;
  }
  if (latest.trim()) return latest;
  return requestInput(value);
}

function codexTurnIdentity(headers, body) {
  let threadId = "";
  const raw = headers && typeof headers["x-codex-turn-metadata"] === "string"
    ? headers["x-codex-turn-metadata"]
    : "";
  if (raw && raw.length <= 4096) {
    try {
      const metadata = JSON.parse(raw);
      if (metadata && typeof metadata.thread_id === "string") threadId = metadata.thread_id;
    } catch {}
  }
  if (threadId) return `thread:${threadId}`;
  if (body && typeof body.prompt_cache_key === "string" && body.prompt_cache_key) {
    return `cache:${body.prompt_cache_key}`;
  }
  return "";
}

function isCodexResponsesRequest(request, body) {
  if (Array.isArray(body?.input)) return true;
  if (typeof request?.headers?.["x-codex-turn-metadata"] === "string") return true;
  const accept = String(request?.headers?.accept || "").toLowerCase();
  return accept.includes("text/event-stream");
}

function codexModelCatalog() {
  const contextWindow = 1_000_000;
  return {
    models: [{
      slug: "chatgpt-web/auto",
      display_name: "ChatGPT Web · Current Web selection",
      description: "Keep the model and reasoning level selected by the user in this ChatGPT conversation.",
      input_modalities: ["text"],
      visibility: "list",
      supported_in_api: true,
      priority: 0,
      shell_type: "shell_command",
      base_instructions: "",
      default_reasoning_level: "none",
      supported_reasoning_levels: [{
        effort: "none",
        description: "The relay never changes the ChatGPT model or reasoning picker.",
      }],
      default_reasoning_summary: "none",
      support_verbosity: true,
      default_verbosity: "low",
      apply_patch_tool_type: "freeform",
      web_search_tool_type: "text_and_image",
      truncation_policy: { mode: "tokens", limit: 10_000 },
      supports_parallel_tool_calls: false,
      supports_image_detail_original: false,
      context_window: contextWindow,
      max_context_window: contextWindow,
      effective_context_window_percent: 100,
      auto_compact_token_limit: 900_000,
      comp_hash: "docurandom-chatgpt-web-1.0.21",
      experimental_supported_tools: [],
      supports_search_tool: false,
      use_responses_lite: false,
      node_repl_auto_review_required: false,
      node_repl_disabled: true,
      include_skills_usage_instructions: false,
      include_plugin_usage_instructions: false,
      include_apps_usage_instructions: false,
      multi_agent_version: "disabled",
      tool_mode: null,
      availability_nux: null,
      upgrade: null,
      additional_speed_tiers: [],
      service_tiers: [],
      default_service_tier: null,
    }],
  };
}

module.exports = {
  codexModelCatalog,
  codexTurnIdentity,
  contentText,
  isCodexResponsesRequest,
  latestUserInput,
  requestInput,
};
