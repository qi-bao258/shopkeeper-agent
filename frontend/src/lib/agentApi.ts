/**
 * 智能体接口客户端
 * 封装后端 /api/query SSE 流式接口请求与事件解析逻辑
 */
import type { AgentEvent } from "../types/agent";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL?.replace(/\/$/, "") ?? "";

type QueryOptions = {
  signal?: AbortSignal;
  onEvent: (event: AgentEvent) => void;
  /** 会话标识；首轮留空，由服务端生成并通过 session 事件回传 */
  sessionId?: string | null;
};

export async function streamQuery(query: string, options: QueryOptions) {
  const response = await fetch(`${API_BASE_URL}/api/query`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify({ query, session_id: options.sessionId ?? undefined }),
    signal: options.signal,
  });

  if (!response.ok) {
    throw new Error(`接口请求失败：HTTP ${response.status}`);
  }

  if (!response.body) {
    throw new Error("浏览器未返回可读取的流式响应。");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split(/\n\n/);
    buffer = chunks.pop() ?? "";

    for (const chunk of chunks) {
      const event = parseSseChunk(chunk);
      if (event) {
        options.onEvent(event);
      }
    }
  }

  buffer += decoder.decode();
  const tail = parseSseChunk(buffer);
  if (tail) {
    options.onEvent(tail);
  }
}

/**
 * 拉取指定会话的历史轮次
 * 用于浏览器刷新后恢复对话——SSE 是单向流，历史恢复必须走这个接口
 */
export async function fetchSessionHistory(sessionId: string) {
  const response = await fetch(
    `${API_BASE_URL}/api/session/${encodeURIComponent(sessionId)}`,
  );
  if (!response.ok) {
    throw new Error(`会话历史请求失败：HTTP ${response.status}`);
  }
  return (await response.json()) as {
    session_id: string;
    turns: Array<{
      question: string;
      resolved_question: string;
      sql: string | null;
      row_count: number;
      created_at: number;
    }>;
  };
}

/** 清空服务端会话历史 */
export async function clearSession(sessionId: string) {
  const response = await fetch(
    `${API_BASE_URL}/api/session/${encodeURIComponent(sessionId)}`,
    { method: "DELETE" },
  );
  // 404 表示服务端已无该会话（例如已过期），对前端而言等价于「已清空」
  if (!response.ok && response.status !== 404) {
    throw new Error(`清空会话失败：HTTP ${response.status}`);
  }
}

function parseSseChunk(chunk: string): AgentEvent | null {
  const payload = chunk
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.replace(/^data:\s?/, ""))
    .join("\n")
    .trim();

  if (!payload) return null;

  try {
    return JSON.parse(payload) as AgentEvent;
  } catch {
    return {
      type: "error",
      message: `无法解析后端事件：${payload}`,
    };
  }
}
