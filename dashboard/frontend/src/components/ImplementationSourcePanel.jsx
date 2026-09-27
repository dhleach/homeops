import {
  BUILD_SHA,
  IMPLEMENTATION_SOURCES,
  sourceUrl,
  TRUSTED_WORKFLOW_SOURCE,
} from "../fleetDeploymentSources.js";

export function ImplementationSourcePanel({
  implementation,
  availableImplementations,
  onImplementationChange,
}) {
  const availableSources = Object.values(IMPLEMENTATION_SOURCES).filter((candidate) => (
    availableImplementations.includes(candidate.id)
  ));
  const source = availableSources.find((candidate) => candidate.id === implementation)
    ?? availableSources[0]
    ?? IMPLEMENTATION_SOURCES.python;
  const revisionLabel = BUILD_SHA ? BUILD_SHA.slice(0, 12) : "master";

  return (
    <section
      aria-labelledby="implementation-source-heading"
      className="rounded-xl border border-border bg-slate-950/40 p-4"
      data-testid="implementation-source-view"
    >
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wider text-blue-200">
            Read-only source view
          </p>
          <h3 id="implementation-source-heading" className="mt-1 text-sm font-semibold text-white">
            Implementation source
          </h3>
          <p className="mt-1 text-xs leading-5 text-slate-400">
            These tabs are the checked-in deployer paths used by the trusted workflow. The
            DeploymentSpec parameters above remain separate and are never editable as source code.
          </p>
        </div>
        <span className="shrink-0 rounded-full border border-slate-600/70 px-2.5 py-1 text-[10px] font-medium uppercase tracking-wider text-slate-400">
          Revision {revisionLabel}
        </span>
      </div>

      <div
        className="mt-4 flex flex-wrap gap-2"
        role="tablist"
        aria-label="Implementation source tabs"
      >
        {availableSources.map((candidate) => {
          const selected = candidate.id === source.id;
          return (
            <button
              key={candidate.id}
              type="button"
              role="tab"
              aria-selected={selected}
              aria-controls="implementation-source-panel"
              data-testid={`implementation-source-tab-${candidate.id}`}
              onClick={() => onImplementationChange?.(candidate.id)}
              className={`rounded-lg border px-3 py-2 text-xs font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-300 focus-visible:ring-offset-2 focus-visible:ring-offset-surface ${selected
                ? "border-blue-400/60 bg-blue-400/10 text-blue-100"
                : "border-border text-slate-400 hover:border-blue-500/50 hover:text-slate-200"}`}
            >
              {candidate.label}
            </button>
          );
        })}
      </div>

      <div
        id="implementation-source-panel"
        className="mt-4 min-w-0"
        role="tabpanel"
        aria-label={`${source.label} source`}
        data-testid="implementation-source-panel"
      >
        <div className="flex min-w-0 flex-col gap-2 text-xs sm:flex-row sm:items-center sm:justify-between">
          <code className="min-w-0 break-all text-slate-400">{source.path}</code>
          <a
            href={sourceUrl(source.path)}
            target="_blank"
            rel="noreferrer"
            className="shrink-0 text-blue-300 underline underline-offset-2 hover:text-blue-200"
          >
            Open {source.label} on GitHub
          </a>
        </div>
        <pre
          aria-label={`${source.label} source code`}
          className="mt-3 max-h-96 max-w-full overflow-auto rounded-lg border border-border bg-slate-950 p-4 text-[11px] leading-5 text-emerald-200"
          data-testid="implementation-source-code"
        >
          {source.content}
        </pre>
      </div>

      <p className="mt-3 text-xs leading-5 text-slate-500">
        The trusted selector and execution boundary are also checked in at{" "}
        <a
          href={sourceUrl(TRUSTED_WORKFLOW_SOURCE.path)}
          target="_blank"
          rel="noreferrer"
          className="text-slate-400 underline underline-offset-2 hover:text-slate-200"
        >
          {TRUSTED_WORKFLOW_SOURCE.path}
        </a>
        .
      </p>
    </section>
  );
}
