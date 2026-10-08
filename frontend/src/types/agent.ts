/**
 * 智能体类型定义
 * 定义问数智能体前端使用的 SSE 事件、流程步骤和聊天消息类型
 */
export type ProgressStatus = "running" | "success" | "error";

export type ProgressEvent = {
  type: "progress";
  step: string;
  status: ProgressStatus;
};

export type ResultEvent = {
  type: "result";
  data: unknown;
  /** 本轮执行的最终 SQL，供前端展示与排错 */
  sql?: string;
  /** 追问改写后的独立问题，多轮场景下与用户原始提问不同 */
  resolved_query?: string;
};

export type ErrorEvent = {
  type: "error";
  message: string;
};

/**
 * 会话事件
 * 首轮请求返回，携带服务端生成的 session_id；
 * 前端保存后在后续轮次回传，即可延续同一份对话历史。
 */
export type SessionEvent = {
  type: "session";
  session_id: string;
};

export type AgentEvent =
  | ProgressEvent
  | ResultEvent
  | ErrorEvent
  | SessionEvent;

export type StepState = {
  step: string;
  status: ProgressStatus;
  updatedAt: number;
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  createdAt: number;
  status?: "streaming" | "done" | "error";
  steps?: StepState[];
  result?: unknown;
  /** 本轮 SQL，用于在结果区展示 */
  sql?: string;
  /** 追问改写结果；与用户原问题不同时说明发生了指代补全 */
  resolvedQuery?: string;
  error?: string;
};
