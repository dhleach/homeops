import { useState } from "react";
import { useAsk } from "../hooks/useAsk.js";

const SUGGESTED_QUESTIONS = [
  "Is my HVAC behaving normally?",
  "Which floor ran the most today?",
  "Is floor 2 running too long?",
];

/**
 * AI diagnostic chat widget for HomeOps.
 * Lets visitors ask natural-language questions about live HVAC data.
 *
 * @param {{ apiUrl: string }} props
 */
export function AskHvac({ apiUrl }) {
  const [input, setInput] = useState("");
  const { ask, answer, loading, error, reset } = useAsk(apiUrl);

  const handleSubmit = (question) => {
    const q = question.trim();
    if (!q || loading) return;
    ask(q);
  };

  const handleChipClick = (question) => {
    setInput(question);
    handleSubmit(question);
  };

  const handleFormSubmit = (e) => {
    e.preventDefault();
    handleSubmit(input);
  };

  const handleInputChange = (e) => {
    setInput(e.target.value);
    if (answer || error) reset();
  };

  const showPanel = loading || answer !== null || error !== null;

  return (
    <div className="rounded-2xl border border-border bg-card overflow-hidden">
      {/* Header */}
      <div className="px-6 py-5 border-b border-border">
        <div className="flex items-center gap-3">
          <span className="text-2xl">🤖</span>
          <div>
            <h2 className="text-lg font-semibold text-white">Ask HomeOps</h2>
            <p className="text-xs text-slate-400">Powered by Gemini · live HVAC data</p>
          </div>
        </div>
      </div>

      {/* Suggested question chips */}
      <div className="px-6 py-4 border-b border-border flex flex-wrap gap-2">
        {SUGGESTED_QUESTIONS.map((q) => (
          <button
            key={q}
            type="button"
            onClick={() => handleChipClick(q)}
            disabled={loading}
            className="rounded-full border border-border bg-surface px-4 py-1.5 text-sm text-slate-300 transition-colors hover:border-blue-500/50 hover:bg-blue-500/10 hover:text-blue-300 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {q}
          </button>
        ))}
      </div>

      {/* Input row */}
      <form onSubmit={handleFormSubmit} className="px-6 py-4 flex gap-3 items-center">
        <input
          type="text"
          value={input}
          onChange={handleInputChange}
          placeholder="Ask about your HVAC system…"
          disabled={loading}
          className="flex-1 rounded-xl border border-border bg-surface px-4 py-2.5 text-sm text-white placeholder-slate-500 outline-none transition-colors focus:border-blue-500/50 focus:ring-1 focus:ring-blue-500/30 disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={loading || !input.trim()}
          className="shrink-0 rounded-xl bg-blue-600 px-5 py-2.5 text-sm font-medium text-white transition-colors hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {loading ? "…" : "Ask →"}
        </button>
      </form>

      {/* Answer / loading / error panel */}
      {showPanel && (
        <div className="px-6 pb-6">
          <div className="rounded-xl border border-border bg-surface p-5">
            {loading && (
              <div className="flex items-center gap-3 text-slate-400">
                <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-slate-500 border-t-blue-400" />
                <span className="text-sm">Analyzing your HVAC data…</span>
              </div>
            )}
            {!loading && error && (
              <p className="text-sm text-orange-400">{error}</p>
            )}
            {!loading && answer && (
              <p className="text-sm leading-relaxed text-slate-200 whitespace-pre-wrap">{answer}</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
