/**
 * 聊天消息气泡组件
 * 组合展示用户问题、智能体回复、执行流程和结果表格
 */
import { useEffect, useState } from "react";
import { Bot, Code2, Copy, CornerDownRight, UserRound } from "lucide-react";
import { ResultTable } from "./ResultTable";
import { StepRail } from "./StepRail";
import { cn, formatTime, toClipboardText } from "../lib/format";
import type { ChatMessage } from "../types/agent";

export function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";
  const [sqlOpen, setSqlOpen] = useState(false);
  const [sqlCopied, setSqlCopied] = useState(false);

  // 复制 SQL 后短暂回显「已复制」，避免用户不确定是否成功
  useEffect(() => {
    if (!sqlCopied) return;
    const timer = window.setTimeout(() => setSqlCopied(false), 1500);
    return () => window.clearTimeout(timer);
  }, [sqlCopied]);

  // 追问改写结果与原始提问一致时无需提示；仅在发生指代补全时展示
  const showResolved =
    !isUser &&
    Boolean(message.resolvedQuery) &&
    message.resolvedQuery !== message.content &&
    message.resolvedQuery !== undefined;

  const copySql = async () => {
    if (!message.sql) return;
    await navigator.clipboard.writeText(message.sql);
    setSqlCopied(true);
  };

  const copy = async () => {
    const text = message.result ? toClipboardText(message.result) : message.content;
    await navigator.clipboard.writeText(text);
  };

  return (
    <article className={cn("group flex gap-3", isUser && "justify-end")}>
      {!isUser && (
        <div className="mt-1 grid h-9 w-9 shrink-0 place-items-center rounded-full bg-ink text-parchment">
          <Bot className="h-4 w-4" aria-hidden="true" />
        </div>
      )}

      <div className={cn("max-w-[920px] flex-1", isUser && "flex max-w-[760px] justify-end")}>
        <div
          className={cn(
            "relative border px-5 py-4 shadow-line",
            isUser
              ? "border-ink/80 bg-ink text-parchment"
              : "border-ink/10 bg-[#fffaf1]/78 text-ink backdrop-blur",
          )}
        >
          <div className="flex items-start justify-between gap-3">
            <p className="whitespace-pre-wrap text-[15px] leading-7">{message.content}</p>
            {!isUser && message.status !== "streaming" && (
              <button
                type="button"
                onClick={copy}
                className="shrink-0 rounded-full p-1.5 text-ink/45 opacity-0 outline-none transition hover:bg-ink/5 hover:text-ink focus:opacity-100 focus:ring-2 focus:ring-moss/40 group-hover:opacity-100"
                title="复制"
                aria-label="复制"
              >
                <Copy className="h-4 w-4" aria-hidden="true" />
              </button>
            )}
          </div>

          {message.error && (
            <div className="mt-3 border border-tomato/30 bg-tomato/10 px-3 py-2 text-sm text-tomato">
              {message.error}
            </div>
          )}

          {showResolved && (
            <div className="mt-3 flex items-start gap-2 border-l-2 border-moss/45 bg-moss/6 px-3 py-2 text-xs leading-5 text-ink/70">
              <CornerDownRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-moss" aria-hidden="true" />
              <span>
                已结合上文理解为：
                <span className="font-medium text-ink">{message.resolvedQuery}</span>
              </span>
            </div>
          )}

          {!isUser && <StepRail steps={message.steps} />}
          {!isUser && message.result !== undefined && <ResultTable data={message.result} />}

          {!isUser && message.sql && (
            <section className="mt-4 overflow-hidden border border-ink/10 bg-[#20201d]">
              <button
                type="button"
                onClick={() => setSqlOpen((open) => !open)}
                className="flex w-full items-center justify-between px-4 py-2.5 text-xs font-semibold tracking-[0.08em] text-parchment/70 transition hover:text-parchment"
                aria-expanded={sqlOpen}
              >
                <span className="inline-flex items-center gap-2">
                  <Code2 className="h-3.5 w-3.5" aria-hidden="true" />
                  SQL
                </span>
                <span className="text-parchment/45">{sqlOpen ? "收起" : "展开"}</span>
              </button>
              {sqlOpen && (
                <div className="relative border-t border-white/10">
                  <button
                    type="button"
                    onClick={copySql}
                    className="absolute right-2 top-2 rounded px-2 py-1 text-[11px] text-parchment/55 transition hover:bg-white/10 hover:text-parchment"
                    aria-label="复制 SQL"
                  >
                    {sqlCopied ? "已复制" : "复制"}
                  </button>
                  <pre className="max-h-[280px] overflow-auto px-4 py-3 pr-16 font-mono text-[12px] leading-6 text-[#d8e0c8]">
                    {message.sql}
                  </pre>
                </div>
              )}
            </section>
          )}

          <div
            className={cn(
              "mt-3 text-xs",
              isUser ? "text-parchment/55" : "text-ink/45",
            )}
          >
            {formatTime(message.createdAt)}
          </div>
        </div>
      </div>

      {isUser && (
        <div className="mt-1 grid h-9 w-9 shrink-0 place-items-center rounded-full bg-moss text-white">
          <UserRound className="h-4 w-4" aria-hidden="true" />
        </div>
      )}
    </article>
  );
}
