import { useCallback, useEffect, useState } from "react";
import { createDeploymentId, DeploymentSpecForm } from "./DeploymentSpecForm.jsx";
import { IMPLEMENTATION_SOURCES, sourceUrl } from "../fleetDeploymentSources.js";

const REFRESH_INTERVAL_MS = 30_000;
const DEPLOYMENT_REFRESH_INTERVAL_MS = 2_000;
const HISTORY_REFRESH_INTERVAL_MS = 30_000;
const HISTORY_PREVIEW_COUNT = 4;
const ACTIVE_DEPLOYMENT_STORAGE_KEY = "homeops.activeFleetDeploymentId";
const ENVIRONMENTS = ["test", "stage", "prod"];
const RESET_TARGET_IDS = ENVIRONMENTS.flatMap((environment) => (
  [1, 2, 3, 4].map((index) => `${environment}-vehicle-${String(index).padStart(2, "0")}`)
));
const SOURCE_REPOSITORY_URL = "https://github.com/dhleach/homeops";
const FRONTEND_SOURCE_PATH = "dashboard/frontend/src/components/FleetDeployView.jsx";
const FRONTEND_README_PATH = "dashboard/frontend/README.md";
const RAW_BUILD_SHA = import.meta.env.VITE_BUILD_SHA;
const BUILD_SHA = typeof RAW_BUILD_SHA === "string" && /^[0-9a-f]{40}$/i.test(RAW_BUILD_SHA)
  ? RAW_BUILD_SHA.toLowerCase()
  : null;
const SOURCE_REF = BUILD_SHA ?? "master";
const FRONTEND_SOURCE_URL = `${SOURCE_REPOSITORY_URL}/blob/${SOURCE_REF}/${FRONTEND_SOURCE_PATH}`;
const FRONTEND_README_URL = `${SOURCE_REPOSITORY_URL}/blob/${SOURCE_REF}/${FRONTEND_README_PATH}`;
const BUILD_COMMIT_URL = BUILD_SHA ? `${SOURCE_REPOSITORY_URL}/commit/${BUILD_SHA}` : null;

const PROFILE_COLORS = {
  blue: "#60a5fa",
  green: "#4ade80",
  orange: "#fb923c",
  purple: "#c084fc",
};

const PROFILE_SHAPES = {
  circle: "fleet-shape--circle",
  hexagon: "fleet-shape--hexagon",
  square: "fleet-shape--square",
  triangle: "fleet-shape--triangle",
};

const STATUS_LABELS = {
  applying: "Applying",
  failed: "Failed",
  pending: "Pending",
  queued: "Queued",
  ready: "Ready",
  started: "Started",
  succeeded: "Succeeded",
  waiting: "Waiting for workflow",
  workflow_unavailable: "Workflow state unavailable",
};

const WORKFLOW_STATUS_LABELS = {
  completed: "Completed",
  in_progress: "Running",
  pending: "Pending",
  queued: "Queued",
  requested: "Requested",
  waiting: "Waiting",
};

const ERROR_CLASS_LABELS = {
  capacity_full: "Fleet busy",
  invalid_deployment_spec: "Request rejected",
  manifest_commit_failed: "Manifest commit failed",
  service_unavailable: "Service unavailable",
  submission_cooldown: "Cooldown active",
  submission_failed: "Submission failed",
  verification_failed: "Verification failed",
  workflow_dispatch_failed: "Workflow dispatch failed",
  workflow_failed: "Workflow failed",
  workflow_unavailable: "Workflow state unavailable",
};

const ROLLBACK_STATUS_LABELS = {
  not_started: "Not verified",
  in_progress: "Restoring",
  succeeded: "Verified",
  failed: "Failed",
};

const TERMINAL_JOB_STATUSES = new Set(["completed"]);

function errorClassLabel(code) {
  return ERROR_CLASS_LABELS[code] ?? "Deployment error";
}

function workflowIsSettled(deployment) {
  if (deployment?.dispatch_status !== "dispatched") return deployment?.dispatch_status === "failed";
  const workflow = deployment?.workflow;
  return Boolean(
    workflow
    && workflow.status === "completed"
    && Array.isArray(workflow.jobs)
    && workflow.jobs.every((job) => TERMINAL_JOB_STATUSES.has(job.status)),
  );
}

function deploymentNeedsPolling(deployment) {
  if (!deployment) return false;
  if (deployment.dispatch_status === "failed") return false;
  if (deployment.status !== "succeeded" && deployment.status !== "failed") return true;
  return !workflowIsSettled(deployment);
}

function deploymentDisplayStatus(deployment) {
  if (deployment?.error_code === "workflow_unavailable") return "workflow_unavailable";
  if (deployment?.dispatch_status === "dispatched" && !workflowIsSettled(deployment)) return "waiting";
  if (deployment?.dispatch_status === "failed") return "failed";
  return deployment?.status;
}

function fleetApiUrl(apiUrl) {
  return `${apiUrl.replace(/\/$/, "")}/deploy/api/fleet`;
}

function deploymentApiUrl(apiUrl, deploymentId) {
  return `${apiUrl.replace(/\/$/, "")}/deploy/api/deployments/${encodeURIComponent(deploymentId)}`;
}

function deploymentHistoryApiUrl(apiUrl) {
  return `${apiUrl.replace(/\/$/, "")}/deploy/api/deployments/history?limit=20`;
}

function displayStatus(status) {
  return STATUS_LABELS[status] ?? "Unknown";
}

function formatDigest(digest) {
  if (!digest) return "Unavailable";
  return `sha256:${digest.slice(0, 12)}`;
}

function profileText(profile) {
  if (!profile) return "Unavailable";
  return `${profile.color} ${profile.shape}`;
}

function targetDisplayLabel(target) {
  const match = /^(test|stage|prod)-vehicle-(\d{2})$/.exec(target);
  return match ? `${match[1].toUpperCase()}-${match[2]}` : target;
}

function evidenceStatusLabel(conclusion, status) {
  if (conclusion === "success") return "Passed";
  if (conclusion === "failure" || conclusion === "timed_out" || conclusion === "cancelled") return "Failed";
  if (conclusion === "skipped") return "Skipped";
  return WORKFLOW_STATUS_LABELS[status] ?? displayStatus(status);
}

function evidenceStatusClass(conclusion, status) {
  if (conclusion === "success" || status === "succeeded") return "text-emerald-300";
  if (conclusion === "failure" || conclusion === "timed_out" || conclusion === "cancelled" || status === "failed") {
    return "text-red-300";
  }
  if (conclusion === "skipped") return "text-slate-400";
  return "text-amber-200";
}

function deploymentEventLabel(eventType) {
  return eventType
    .replace(/^deployment_/, "")
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

function deploymentEventStatusLabel(event) {
  if (event.event_type.endsWith("verified")) return "Verified";
  return evidenceStatusLabel(null, event.status);
}

function deploymentEventStatusClass(event) {
  if (event.event_type.endsWith("verified")) return "text-emerald-300";
  return evidenceStatusClass(null, event.status);
}

function groupTargets(targets) {
  return ENVIRONMENTS.map((environment) => ({
    environment,
    targets: targets.filter((target) => target.environment === environment),
  }));
}

export function useFleet(apiUrl) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [lastUpdated, setLastUpdated] = useState(null);

  const refresh = useCallback(async (signal) => {
    try {
      const response = await fetch(fleetApiUrl(apiUrl), {
        cache: "no-store",
        headers: { Accept: "application/json" },
        signal,
      });
      if (!response.ok) {
        throw new Error(`Fleet API returned HTTP ${response.status}`);
      }

      const payload = await response.json();
      if (
        payload?.simulated !== true ||
        payload?.target_kind !== "simulated" ||
        !Array.isArray(payload?.targets) ||
        payload.targets.length !== 12
      ) {
        throw new Error("Fleet API returned an incomplete simulated-fleet snapshot");
      }

      setData(payload);
      setError(null);
      setLastUpdated(new Date());
    } catch (requestError) {
      if (requestError?.name !== "AbortError") {
        setError(requestError instanceof Error ? requestError.message : "Unable to read fleet state");
      }
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [apiUrl]);

  useEffect(() => {
    const controller = new AbortController();
    refresh(controller.signal);
    const interval = window.setInterval(() => {
      refresh(controller.signal);
    }, REFRESH_INTERVAL_MS);

    return () => {
      controller.abort();
      window.clearInterval(interval);
    };
  }, [refresh]);

  const refreshFleet = useCallback(() => refresh(), [refresh]);

  return { data, loading, error, lastUpdated, refresh: refreshFleet };
}

export function useDeploymentHistory(apiUrl) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const refresh = useCallback(async (signal) => {
    try {
      const response = await fetch(deploymentHistoryApiUrl(apiUrl), {
        cache: "no-store",
        headers: { Accept: "application/json" },
        signal,
      });
      if (!response.ok) {
        throw new Error(`Deployment history API returned HTTP ${response.status}`);
      }
      const payload = await response.json();
      if (
        payload?.simulated !== true
        || payload?.target_kind !== "simulated"
        || !Array.isArray(payload?.deployments)
      ) {
        throw new Error("Deployment history API returned an incomplete snapshot");
      }
      setData(payload);
      setError(null);
    } catch (requestError) {
      if (requestError?.name !== "AbortError") {
        setError(requestError instanceof Error ? requestError.message : "Unable to read deployment history");
      }
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [apiUrl]);

  useEffect(() => {
    const controller = new AbortController();
    refresh(controller.signal);
    const interval = window.setInterval(() => refresh(controller.signal), HISTORY_REFRESH_INTERVAL_MS);
    return () => {
      controller.abort();
      window.clearInterval(interval);
    };
  }, [refresh]);

  return { data, loading, error, refresh };
}

function readStoredDeploymentId() {
  try {
    return window.sessionStorage.getItem(ACTIVE_DEPLOYMENT_STORAGE_KEY);
  } catch {
    return null;
  }
}

function storeDeploymentId(deploymentId) {
  try {
    if (deploymentId) {
      window.sessionStorage.setItem(ACTIVE_DEPLOYMENT_STORAGE_KEY, deploymentId);
    } else {
      window.sessionStorage.removeItem(ACTIVE_DEPLOYMENT_STORAGE_KEY);
    }
  } catch {
    // Private browsing and disabled storage should not block the demo.
  }
}

export function useDeployment(apiUrl) {
  const [deploymentId, setDeploymentId] = useState(readStoredDeploymentId);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    if (!deploymentId) return null;
    try {
      const response = await fetch(deploymentApiUrl(apiUrl, deploymentId), {
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        throw new Error(`Deployment API returned HTTP ${response.status}`);
      }
      const payload = await response.json();
      if (
        payload?.simulated !== true
        || payload?.deployment_id !== deploymentId
        || !Array.isArray(payload?.targets)
      ) {
        throw new Error("Deployment API returned an incomplete run snapshot");
      }
      setData(payload);
      setError(null);
      return payload;
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to read deployment state");
      return null;
    }
  }, [apiUrl, deploymentId]);

  useEffect(() => {
    if (!deploymentId) {
      setData(null);
      setError(null);
      return undefined;
    }

    let disposed = false;
    let timer;
    const poll = async () => {
      const payload = await refresh();
      if (disposed) return;
      if (!payload) return;
      if (deploymentNeedsPolling(payload)) {
        timer = window.setTimeout(poll, DEPLOYMENT_REFRESH_INTERVAL_MS);
      }
    };
    poll();

    return () => {
      disposed = true;
      window.clearTimeout(timer);
    };
  }, [deploymentId, refresh]);

  const trackDeployment = useCallback((nextDeploymentId, initialData = null) => {
    storeDeploymentId(nextDeploymentId);
    setDeploymentId(nextDeploymentId || null);
    setData(
      initialData?.deployment_id === nextDeploymentId
        && Array.isArray(initialData?.targets)
        && Array.isArray(initialData?.target_ids)
        ? initialData
        : null,
    );
    setError(null);
  }, []);

  return {
    data,
    deploymentId,
    error,
    refresh,
    trackDeployment,
  };
}

function ProfileGlyph({ profile, label }) {
  const color = PROFILE_COLORS[profile?.color] ?? "#94a3b8";
  const shape = PROFILE_SHAPES[profile?.shape] ?? "fleet-shape--square";

  return (
    <span
      aria-label={label}
      className={`fleet-profile-glyph ${shape}`}
      role="img"
      style={{ backgroundColor: color }}
    />
  );
}

function VehicleGlyph({ environment }) {
  return (
    <span
      aria-hidden="true"
      className={`fleet-vehicle-glyph fleet-vehicle-glyph--${environment}`}
      data-testid="fleet-vehicle-glyph"
    />
  );
}

function StatusPill({ status }) {
  const statusClass = status === "failed"
    ? "border-red-400/30 bg-red-400/10 text-red-300"
    : status === "queued" || status === "applying" || status === "pending"
      || status === "waiting" || status === "workflow_unavailable"
      ? "border-amber-400/30 bg-amber-400/10 text-amber-200"
      : "border-emerald-400/30 bg-emerald-400/10 text-emerald-300";

  return (
    <span className={`rounded-full border px-2.5 py-1 text-xs font-medium ${statusClass}`}>
      {displayStatus(status)}
    </span>
  );
}

function FleetTargetCard({ target, selected }) {
  const synchronized = Boolean(
    target.desired_digest
    && target.observed_digest
    && target.desired_digest === target.observed_digest,
  );
  const cardTitleId = `fleet-target-${target.target_id}`;
  const displayLabel = target.label ?? targetDisplayLabel(target.target_id);
  const summaryClass = synchronized
    ? "border-emerald-400/30 bg-emerald-400/5"
    : "border-amber-400/40 bg-amber-400/10";
  const syncTextClass = synchronized ? "text-emerald-200" : "text-amber-100";

  return (
    <article
      aria-labelledby={cardTitleId}
      className={`flex min-w-0 flex-col rounded-2xl border p-3 shadow-lg shadow-slate-950/10 transition-colors ${selected
        ? "border-blue-400/80 bg-blue-400/10 ring-1 ring-blue-400/40"
        : "border-border bg-card"}`}
      data-selected={selected ? "true" : "false"}
      data-target-id={target.target_id}
      data-testid="fleet-target-card"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <VehicleGlyph environment={target.environment} />
          <div className="min-w-0">
            <p className="text-[10px] font-semibold uppercase tracking-[0.2em] text-blue-300">
              Fleet target
            </p>
            <h3 id={cardTitleId} className="mt-1 font-mono text-base font-semibold text-white">
              {displayLabel}
            </h3>
            <p className="mt-0.5 text-xs text-slate-400">
              {target.environment.toUpperCase()} · simulated vehicle
            </p>
          </div>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-2">
          {selected && (
            <span className="rounded-full border border-blue-300/40 bg-blue-300/10 px-2 py-1 text-[10px] font-semibold uppercase tracking-wider text-blue-100">
              Selected
            </span>
          )}
        </div>
      </div>

      <div
        aria-label={synchronized ? "Desired matches observed" : "Desired differs from observed"}
        className={`mt-3 flex items-center gap-3 rounded-xl border p-3 ${summaryClass}`}
        data-synchronized={synchronized ? "true" : "false"}
        data-testid="fleet-target-sync"
      >
        <ProfileGlyph
          profile={target.observed}
          label={`Observed ${profileText(target.observed)}`}
        />
        <div className="min-w-0 flex-1">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Observed profile</p>
          <p className="mt-0.5 truncate text-sm font-medium text-slate-200">{profileText(target.observed)}</p>
          <p className={`mt-1 text-[11px] font-medium ${syncTextClass}`}>
            {synchronized ? "Healthy · synchronized" : "Needs attention · drift detected"}
          </p>
        </div>
        <div className="ml-auto flex shrink-0 flex-col items-end gap-1">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">Health</span>
          <StatusPill status={target.status} />
        </div>
      </div>

      {!synchronized && (
        <div className="mt-3 rounded-xl border border-amber-400/40 bg-amber-400/10 p-3" data-testid="fleet-target-drift-details">
          <div className="flex items-start justify-between gap-3">
            <div>
              <p className="text-xs font-semibold text-amber-100">Desired / observed drift</p>
              <p className="mt-1 text-[11px] text-slate-400">
                Desired profile differs from observed state.
              </p>
            </div>
            <span aria-hidden="true" className="text-lg leading-none text-amber-100">!</span>
          </div>
          <div className="mt-3 grid grid-cols-2 gap-3 border-t border-current/10 pt-3 text-xs">
            <div>
              <p className="text-slate-400">Desired</p>
              <div className="mt-1 flex items-center gap-2 text-slate-300">
                <ProfileGlyph profile={target.desired} label={`Desired ${profileText(target.desired)}`} />
                <span>{profileText(target.desired)}</span>
              </div>
            </div>
            <div>
              <p className="text-slate-400">Observed</p>
              <div className="mt-1 flex items-center gap-2 text-slate-300">
                <ProfileGlyph profile={target.observed} label={`Observed ${profileText(target.observed)}`} />
                <span>{profileText(target.observed)}</span>
              </div>
            </div>
          </div>
        </div>
      )}

      {target.last_error && (
        <p className="mt-3 truncate text-xs text-red-300" title={target.last_error}>
          Health detail: {target.last_error}
        </p>
      )}

      <details className="mt-3 rounded-lg border border-border/70 bg-slate-950/20 px-3 py-2 text-xs" data-testid="fleet-target-details">
        <summary className="cursor-pointer rounded font-medium text-slate-400 hover:text-slate-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-300 focus-visible:ring-offset-2 focus-visible:ring-offset-surface">
          Full target details
        </summary>
        <dl className="mt-3 space-y-2 border-t border-border/70 pt-3 text-slate-300">
          <div className="flex items-start justify-between gap-3">
            <dt className="text-slate-400">Target ID</dt>
            <dd className="max-w-[14rem] break-all text-right font-mono">{target.target_id}</dd>
          </div>
          <div className="flex items-start justify-between gap-3">
            <dt className="text-slate-400">Desired digest</dt>
            <dd className="max-w-[14rem] break-all text-right font-mono" title={target.desired_digest}>
              {formatDigest(target.desired_digest)}
            </dd>
          </div>
          <div className="flex items-start justify-between gap-3">
            <dt className="text-slate-400">Observed digest</dt>
            <dd className="max-w-[14rem] break-all text-right font-mono" title={target.observed_digest}>
              {formatDigest(target.observed_digest)}
            </dd>
          </div>
        </dl>
      </details>
    </article>
  );
}

function FleetLoadingState() {
  return (
    <div
      aria-label="Loading simulated fleet"
      className="grid min-w-0 grid-cols-1 gap-4 sm:grid-cols-2"
      data-testid="fleet-loading"
    >
      {[...Array(12)].map((_, index) => (
        <div key={index} className="h-28 animate-pulse rounded-2xl border border-border bg-card" />
      ))}
    </div>
  );
}

function formatTimestamp(value) {
  if (!value) return "Unavailable";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function FleetBuildContext() {
  return (
    <section
      aria-labelledby="fleet-build-context-heading"
      className="mt-10 border-t border-border/70 pt-6"
      data-testid="fleet-build-context"
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p
            id="fleet-build-context-heading"
            className="text-xs font-semibold uppercase tracking-[0.22em] text-slate-400"
          >
            Fleet Deploy Lab context
          </p>
          <p className="mt-1 text-xs text-slate-500">
            Public source and documentation for this bounded simulated control plane.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs">
          <a
            href={FRONTEND_SOURCE_URL}
            target="_blank"
            rel="noreferrer"
            className="text-slate-400 underline decoration-slate-600 underline-offset-2 hover:text-slate-200"
          >
            View frontend source
          </a>
          <a
            href={FRONTEND_README_URL}
            target="_blank"
            rel="noreferrer"
            className="text-slate-400 underline decoration-slate-600 underline-offset-2 hover:text-slate-200"
          >
            Frontend README
          </a>
          {BUILD_COMMIT_URL && (
            <span data-testid="fleet-build-revision" className="text-slate-500">
              Build{" "}
              <a
                href={BUILD_COMMIT_URL}
                target="_blank"
                rel="noreferrer"
                className="font-mono text-slate-400 underline decoration-slate-600 underline-offset-2 hover:text-slate-200"
              >
                {BUILD_SHA.slice(0, 12)}
              </a>
            </span>
          )}
        </div>
      </div>
    </section>
  );
}

function targetSummaryLabel(targetIds) {
  return targetIds.map((targetId) => {
    const match = /^(test|stage|prod)-vehicle-(\d{2})$/.exec(targetId);
    return match ? `${match[1].toUpperCase()}-${match[2]}` : targetId;
  }).join(", ");
}

function requestSummaryText(deployment) {
  const summary = deployment.request_summary;
  if (!summary) return "Request details were not persisted by this older run.";
  const targets = summary.environment
    ? `${summary.environment.toUpperCase()} environment (four vehicles)`
    : targetSummaryLabel(summary.target_ids ?? deployment.target_ids);
  const failure = summary.failure_target_id
    ? ` · injected verification failure at ${targetDisplayLabel(summary.failure_target_id)}`
    : "";
  if (summary.operation === "reset") {
    return `Full simulated-fleet reset · ${summary.implementation} / ${summary.strategy} / ${summary.failure_mode}`;
  }
  return `${targets} · ${summary.implementation} / ${summary.strategy} / ${summary.failure_mode}${failure}`;
}

function historySelectorText(entry) {
  if (entry.operation === "reset") return "Complete simulated fleet";
  if (entry.selector?.environment) return `${entry.selector.environment.toUpperCase()} environment`;
  return (entry.selector?.target_ids ?? []).map(targetDisplayLabel).join(", ") || "Targets unavailable";
}

function historyOutcomeLabel(entry) {
  if (entry.outcome === "verified") return "Verified";
  if (entry.outcome === "failed" || entry.outcome.endsWith("failed")) return "Failed";
  if (entry.outcome === "queued") return "Queued";
  if (entry.outcome === "applying") return "Applying";
  return entry.outcome;
}

function historyOutcomeClass(entry) {
  if (entry.outcome === "verified") return "text-emerald-300";
  if (entry.outcome === "failed" || entry.outcome.endsWith("failed")) return "text-red-300";
  return "text-amber-200";
}

function DeploymentHistory({ data, loading, error, activeDeploymentId, onSelect }) {
  const deployments = data?.deployments ?? [];
  const [showAll, setShowAll] = useState(false);
  const visibleDeployments = showAll ? deployments : deployments.slice(0, HISTORY_PREVIEW_COUNT);
  return (
    <section
      aria-labelledby="deployment-history-heading"
      className="mb-8 min-w-0 rounded-2xl border border-border bg-card/70 p-4 shadow-lg shadow-slate-950/10 sm:p-6"
      data-testid="deployment-history"
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.22em] text-blue-300">
            Durable run history
          </p>
          <h2 id="deployment-history-heading" className="mt-1 text-xl font-semibold text-white">
            Recent deployment attempts
          </h2>
          <p className="mt-2 max-w-3xl text-sm text-slate-400">
            Stored in the simulator database, so these attempts survive a backend restart. Select a row
            to reopen its exact run evidence. Fleet reset uses this same validated path and is limited to
            one request per UTC day.
          </p>
        </div>
        {deployments.length > HISTORY_PREVIEW_COUNT && (
          <button
            type="button"
            onClick={() => setShowAll((current) => !current)}
            aria-expanded={showAll}
            className="w-fit shrink-0 rounded-lg border border-border px-3 py-2 text-xs font-medium text-slate-300 hover:border-blue-500/50 hover:text-blue-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-300 focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
            data-testid="deployment-history-view-all"
          >
            {showAll ? "Show recent only" : `View all ${deployments.length} attempts`}
          </button>
        )}
      </div>
      {error && (
        <p className="mt-4 rounded-lg border border-amber-400/30 bg-amber-400/10 p-3 text-xs text-amber-100" role="alert">
          Deployment history refresh failed: {error}
        </p>
      )}
      {loading && !data && (
        <p className="mt-4 text-sm text-slate-400">Loading recent attempts…</p>
      )}
      {!loading && deployments.length === 0 && (
        <p className="mt-4 rounded-lg border border-border bg-slate-950/30 p-3 text-sm text-slate-400">
          No deployment attempts have been recorded yet.
        </p>
      )}
      {deployments.length > 0 && (
        <ol className="mt-4 grid gap-3 lg:grid-cols-2" data-testid="deployment-history-list">
          {visibleDeployments.map((entry) => (
            <li
              key={entry.deployment_id}
              className={`rounded-xl border p-3 ${entry.deployment_id === activeDeploymentId
                ? "border-blue-400/70 bg-blue-400/10"
                : "border-border bg-slate-950/30"}`}
              data-testid="deployment-history-entry"
            >
              <button
                type="button"
                onClick={() => onSelect(entry.deployment_id)}
                className="w-full rounded-lg text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-300 focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                data-testid="deployment-history-row"
                aria-label={`Open deployment run ${entry.deployment_id}`}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-mono text-sm font-semibold text-white">{entry.deployment_id}</span>
                  <span className={historyOutcomeClass(entry)}>{historyOutcomeLabel(entry)}</span>
                </div>
                <p className="mt-2 text-xs text-slate-300">
                  {entry.operation === "reset" ? "Fleet reset" : entry.implementation}
                  {entry.operation !== "reset" && ` · ${entry.selector?.environment
                    ? `${entry.selector.environment.toUpperCase()} environment`
                    : historySelectorText(entry)}`}
                  {` · ${formatTimestamp(entry.created_at)}`}
                </p>
                <p className="mt-1 text-xs text-slate-400">
                  Artifact {formatDigest(entry.artifact_sha256)} · {historySelectorText(entry)}
                </p>
              </button>
              <div className="mt-2 flex flex-wrap gap-3 text-xs">
                <span className="text-slate-500">{entry.verification}</span>
                {entry.workflow_url && (
                  <a
                    href={entry.workflow_url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-blue-300 underline"
                  >
                    Actions run
                  </a>
                )}
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function deploymentStatusLabel(deployment) {
  if (deployment.workflow?.status) {
    return WORKFLOW_STATUS_LABELS[deployment.workflow.status] ?? displayStatus(deployment.status);
  }
  if (deployment.error_code === "workflow_unavailable") return "Workflow state unavailable";
  return displayStatus(deployment.status);
}

function DeploymentRunPanel({ deployment, error, onRefresh }) {
  const verifiedTargets = deployment.targets.filter(
    (target) => deployment.target_ids.includes(target.target_id)
      && target.desired_digest === target.observed_digest,
  );
  const jobs = deployment.workflow?.jobs ?? [];
  const events = deployment.events ?? [];
  const selectedImplementation = deployment.request_summary?.implementation;
  const implementationSource = IMPLEMENTATION_SOURCES[selectedImplementation] ?? null;

  return (
    <section
      aria-labelledby="deployment-run-heading"
      className="mb-10 min-w-0 rounded-2xl border border-border bg-card/70 p-4 shadow-lg shadow-slate-950/10 sm:p-6"
      data-testid="deployment-run-state"
    >
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.22em] text-blue-300">
            Deployment run
          </p>
          <h2 id="deployment-run-heading" className="mt-1 font-mono text-xl font-semibold text-white">
            {deployment.deployment_id}
          </h2>
          <p className="mt-2 text-sm text-slate-400">
            State is reconstructed from GitHub Actions and the observed simulated fleet. Fleet reads are
            separate from submission-pipeline availability and deployment completion.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <StatusPill status={deploymentDisplayStatus(deployment)} />
          <button
            type="button"
            onClick={onRefresh}
            className="rounded-lg border border-border px-3 py-2 text-xs font-medium text-slate-300 hover:border-blue-500/50 hover:text-blue-300"
          >
            Refresh run
          </button>
        </div>
      </div>

      <div
        className="mt-5 rounded-lg border border-blue-400/20 bg-blue-400/5 p-3 text-sm text-slate-200"
        data-testid="deployment-request-summary"
      >
        <p className="text-xs font-semibold uppercase tracking-wider text-blue-200">Frozen request</p>
        <p className="mt-1">{requestSummaryText(deployment)}</p>
        <p className="mt-1 text-xs text-slate-400">
          Attempt ID and request details stay fixed while this run is reconciled.
        </p>
      </div>

      {deployment.request_summary?.failure_mode === "rollback" && (
        <div
          className={`mt-5 rounded-lg border p-3 text-sm ${deployment.rollback_status === "succeeded"
            ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-100"
            : "border-amber-400/30 bg-amber-400/10 text-amber-100"}`}
          data-testid="deployment-rollback-state"
          role="status"
        >
          <p className="font-semibold">
            Rollback: {ROLLBACK_STATUS_LABELS[deployment.rollback_status] ?? "Unknown"}
          </p>
          {deployment.rollback_status === "succeeded" ? (
            <p className="mt-1">
              Deployment remains failed; {deployment.rollback_target_ids?.length ?? 0} changed
              target{deployment.rollback_target_ids?.length === 1 ? "" : "s"} returned to the
              pre-deployment profile.
            </p>
          ) : (
            <p className="mt-1">
              Rollback is not verified. Inspect desired versus observed state; the simulated
              fleet may be partially changed.
            </p>
          )}
          {deployment.rollback_error && (
            <p className="mt-1 text-red-200">Rollback detail: {deployment.rollback_error}</p>
          )}
        </div>
      )}

      {implementationSource && (
        <div
          className="mt-5 rounded-lg border border-slate-600/70 bg-slate-950/30 p-3 text-sm text-slate-200"
          data-testid="deployment-implementation-proof"
        >
          <p className="text-xs font-semibold uppercase tracking-wider text-slate-400">
            Implementation proof
          </p>
          <p className="mt-1 font-semibold text-white">{implementationSource.label}</p>
          <p className="mt-1 text-xs text-slate-400">
            The trusted workflow selected this path from the validated DeploymentSpec:
          </p>
          <a
            href={sourceUrl(implementationSource.path)}
            target="_blank"
            rel="noreferrer"
            className="mt-1 inline-block break-all font-mono text-xs text-blue-300 underline"
          >
            {implementationSource.path}
          </a>
        </div>
      )}

      {deployment.dispatch_status === "dispatched" && !workflowIsSettled(deployment) && (
        <div
          className="mt-5 rounded-lg border border-amber-400/30 bg-amber-400/10 p-3 text-sm text-amber-100"
          data-testid="workflow-convergence"
          role="status"
        >
          Deployment state is recorded. Waiting for the final workflow conclusion and current job rows before closing this attempt.
        </div>
      )}

      <div className="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div className="rounded-lg border border-border bg-slate-950/30 p-3">
          <p className="text-xs uppercase tracking-wider text-slate-400">Workflow state</p>
          <p
            className="mt-1 text-sm font-semibold text-slate-200"
            aria-live="polite"
            aria-atomic="true"
            data-testid="deployment-workflow-status"
          >
            {deploymentStatusLabel(deployment)}
          </p>
          {deployment.workflow?.conclusion && (
            <p className="mt-1 text-xs text-slate-400">Conclusion: {deployment.workflow.conclusion}</p>
          )}
        </div>
        <div className="rounded-lg border border-border bg-slate-950/30 p-3">
          <p className="text-xs uppercase tracking-wider text-slate-400">Verified targets</p>
          <p className="mt-1 text-sm font-semibold text-slate-200">
            {verifiedTargets.length} / {deployment.target_ids.length}
          </p>
        </div>
        <div className="rounded-lg border border-border bg-slate-950/30 p-3">
          <p className="text-xs uppercase tracking-wider text-slate-400">Deployment created</p>
          <p className="mt-1 text-sm text-slate-300">{formatTimestamp(deployment.workflow_created_at ?? deployment.created_at)}</p>
        </div>
        <div className="rounded-lg border border-border bg-slate-950/30 p-3">
          <p className="text-xs uppercase tracking-wider text-slate-400">Run state updated</p>
          <p className="mt-1 text-sm text-slate-300">{formatTimestamp(deployment.workflow_updated_at ?? deployment.updated_at)}</p>
        </div>
      </div>

      <div className="mt-5 flex flex-wrap gap-3 text-sm">
        {deployment.manifest_commit_url && (
          <a
            href={deployment.manifest_commit_url}
            target="_blank"
            rel="noreferrer"
            className="text-blue-300 underline"
          >
            Manifest commit
          </a>
        )}
        {deployment.workflow_url && (
          <a
            href={deployment.workflow_url}
            target="_blank"
            rel="noreferrer"
            className="text-blue-300 underline"
            aria-label={implementationSource
              ? `Run proof: ${implementationSource.label}`
              : "GitHub Actions run"}
          >
            {implementationSource ? `Run proof: ${implementationSource.label}` : "GitHub Actions run"}
          </a>
        )}
      </div>

      {(deployment.error || deployment.workflow_error || error) && (
        <div role="alert" className="mt-5 rounded-lg border border-red-400/30 bg-red-400/10 p-3 text-sm text-red-200">
          {deployment.error_code && (
            <p className="font-semibold">{errorClassLabel(deployment.error_code)}</p>
          )}
          <p className={deployment.error_code ? "mt-1" : undefined}>
            {deployment.error || deployment.workflow_error || error}
          </p>
          {deployment.error_recovery && (
            <p className="mt-1 text-red-200/80">{deployment.error_recovery}</p>
          )}
          {error && !deployment.error_recovery && (
            <p className="mt-1 text-red-200/80">
              Refresh this attempt; its request identity remains fixed while the status is reconciled.
            </p>
          )}
        </div>
      )}

      {(jobs.length > 0 || events.length > 0) && (
        <section className="mt-5 border-t border-border/70 pt-4" data-testid="deployment-timeline">
          <h3 className="text-sm font-semibold text-slate-200">Deployment timeline</h3>
          {jobs.length > 0 && (
            <div className="mt-3" data-testid="github-actions-timeline">
              <h4 className="text-xs font-semibold uppercase tracking-wider text-blue-200">
                GitHub Actions jobs and steps
              </h4>
              <ol className="mt-2 space-y-2 text-xs text-slate-300">
                {jobs.map((job) => (
                  <li key={job.id} className="rounded-lg border border-border bg-slate-950/30 px-3 py-2" data-testid="workflow-job">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="font-medium">{job.name}</span>
                      <span className={evidenceStatusClass(job.conclusion, job.status)}>
                        {evidenceStatusLabel(job.conclusion, job.status)}
                      </span>
                    </div>
                    {job.steps?.length > 0 && (
                      <ol className="mt-2 space-y-1 border-l border-border/70 pl-3">
                        {job.steps.map((step) => (
                          <li key={`${job.id}-${step.number}`} className="flex flex-wrap items-center justify-between gap-2">
                            <span>{step.name}</span>
                            <span className={evidenceStatusClass(step.conclusion, step.status)}>
                              {evidenceStatusLabel(step.conclusion, step.status)}
                            </span>
                          </li>
                        ))}
                      </ol>
                    )}
                    {job.conclusion === "skipped" && (
                      <p className="mt-2 text-slate-400" data-testid="workflow-job-skipped">
                        Skipped because an earlier GitHub Actions job did not complete successfully.
                      </p>
                    )}
                    {job.steps?.length === 0 && job.failed_step && (
                      <p className="mt-2 text-red-300">Failed step: {job.failed_step}</p>
                    )}
                  </li>
                ))}
              </ol>
            </div>
          )}
          {events.length > 0 && (
            <div className="mt-4" data-testid="deployment-events-timeline">
              <h4 className="text-xs font-semibold uppercase tracking-wider text-blue-200">
                Simulator deployment events
              </h4>
              <ol className="mt-2 space-y-2 text-xs text-slate-300">
                {events.map((event) => (
                  <li key={event.sequence} className="rounded-lg border border-border bg-slate-950/30 px-3 py-2" data-testid="deployment-event">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="font-medium">{deploymentEventLabel(event.event_type)}</span>
                      <span className={deploymentEventStatusClass(event)}>
                        {deploymentEventStatusLabel(event)}
                      </span>
                    </div>
                    <p className="mt-1 text-slate-400">
                      Targets: {event.target_ids.map(targetDisplayLabel).join(", ")}
                    </p>
                    {event.detail && <p className="mt-1 text-slate-400">{event.detail}</p>}
                    <p className="mt-1 text-slate-500">Recorded {formatTimestamp(event.recorded_at)}</p>
                  </li>
                ))}
              </ol>
            </div>
          )}
        </section>
      )}
    </section>
  );
}

export function FleetDeployView({ apiUrl }) {
  const { data, loading, error, lastUpdated, refresh } = useFleet(apiUrl);
  const deployment = useDeployment(apiUrl);
  const {
    data: historyData,
    loading: historyLoading,
    error: historyError,
    refresh: refreshHistory,
  } = useDeploymentHistory(apiUrl);
  const [selectedTargetIds, setSelectedTargetIds] = useState([]);
  const [resetting, setResetting] = useState(false);
  const [resetError, setResetError] = useState(null);
  const groups = groupTargets(data?.targets ?? []);

  const handleTargetSelectionChange = useCallback((targetIds) => {
    setSelectedTargetIds(targetIds);
  }, []);

  const handleSubmitted = useCallback((submission) => {
    deployment.trackDeployment(submission?.deployment_id, submission);
    refresh();
    refreshHistory();
  }, [deployment, refresh, refreshHistory]);

  const handleNewAttempt = useCallback(() => {
    deployment.trackDeployment(null);
    refresh();
  }, [deployment, refresh]);

  const handleHistorySelect = useCallback((deploymentId) => {
    deployment.trackDeployment(deploymentId);
  }, [deployment]);

  const handleReset = useCallback(async () => {
    if (resetting) return;
    setResetting(true);
    setResetError(null);
    const payload = {
      operation: "reset",
      deployment_id: createDeploymentId(),
      target_ids: RESET_TARGET_IDS,
      profile: { color: "blue", shape: "circle" },
      schema_version: 1,
      implementation: "python",
      strategy: "all_at_once",
      failure_mode: "abort",
    };
    try {
      const response = await fetch(`${apiUrl.replace(/\/$/, "")}/deploy/api/deployments/submit`, {
        method: "POST",
        cache: "no-store",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
      });
      const body = await response.json().catch(() => null);
      if (!response.ok) {
        const detail = body?.detail;
        const message = detail && typeof detail === "object"
          ? detail.message
          : typeof detail === "string" ? detail : `Fleet reset returned HTTP ${response.status}`;
        throw new Error(message);
      }
      if (body?.deployment_id !== payload.deployment_id) {
        throw new Error("Fleet reset response did not preserve the request identity.");
      }
      deployment.trackDeployment(body.deployment_id, body);
      refresh();
      refreshHistory();
    } catch (requestError) {
      setResetError(requestError instanceof Error ? requestError.message : "Unable to request fleet reset");
    } finally {
      setResetting(false);
    }
  }, [apiUrl, deployment, refresh, refreshHistory, resetting]);

  useEffect(() => {
    if (deployment.data?.status === "succeeded" || deployment.data?.status === "failed") {
      refresh();
      refreshHistory();
    }
  }, [deployment.data?.status, refresh, refreshHistory]);

  return (
    <div className="min-h-screen w-full min-w-0 overflow-x-clip bg-surface text-slate-100" data-testid="fleet-deploy-page">
      <header className="border-b border-border px-4 py-5 sm:px-6">
        <div className="mx-auto flex min-w-0 max-w-6xl flex-col gap-5 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex min-w-0 items-start gap-3">
            <span aria-hidden="true" className="mt-1 text-2xl">🚚</span>
            <div className="min-w-0">
              <p className="text-xs font-semibold uppercase tracking-[0.22em] text-blue-300">
                HomeOps / Deploy
              </p>
              <h1 className="mt-1 text-2xl font-bold tracking-tight text-white sm:text-3xl">
                Fleet Deploy Lab
              </h1>
              <p className="mt-2 max-w-2xl text-sm text-slate-400">
                Configure and launch bounded deployments against twelve simulated vehicles, then follow the
                trusted workflow and observed fleet state.
              </p>
            </div>
          </div>
          <div className="flex w-full flex-wrap items-center gap-3 sm:w-auto">
            <a
              href="/"
              className="flex-1 rounded-xl border border-border px-4 py-2 text-center text-sm font-medium text-slate-300 transition-colors hover:border-blue-500/50 hover:text-blue-300 sm:flex-none"
            >
              ← HomeOps dashboard
            </a>
            <button
              type="button"
              onClick={refresh}
              className="flex-1 rounded-xl border border-blue-500/40 bg-blue-500/10 px-4 py-2 text-sm font-medium text-blue-300 transition-colors hover:bg-blue-500/20 sm:flex-none"
            >
              Refresh fleet
            </button>
            <button
              type="button"
              onClick={handleReset}
              disabled={resetting}
              className="flex-1 rounded-xl border border-amber-400/40 bg-amber-400/10 px-4 py-2 text-sm font-medium text-amber-200 transition-colors hover:bg-amber-400/20 disabled:cursor-not-allowed disabled:opacity-50 sm:flex-none"
              data-testid="fleet-reset"
            >
              {resetting ? "Requesting reset…" : "Reset simulated fleet"}
            </button>
          </div>
        </div>
        {resetError && (
          <p
            className="mx-auto mt-3 max-w-6xl rounded-lg border border-red-400/30 bg-red-400/10 px-4 py-3 text-sm text-red-200"
            data-testid="fleet-reset-error"
            role="alert"
          >
            Fleet reset was not accepted: {resetError}
          </p>
        )}
      </header>

      <main className="mx-auto min-w-0 w-full max-w-7xl px-4 py-6 sm:px-6 sm:py-10" data-testid="fleet-deploy-main">
        <div
          className="grid min-w-0 items-start gap-8 lg:grid-cols-[minmax(22rem,0.82fr)_minmax(0,1.18fr)]"
          data-testid="fleet-deploy-layout"
        >
          <div className="min-w-0" data-testid="deployment-config-column">
            <DeploymentSpecForm
              apiUrl={apiUrl}
              capabilities={data?.capabilities}
              activeDeployment={deployment.data}
              onNewAttempt={handleNewAttempt}
              onSubmitted={handleSubmitted}
              onTargetSelectionChange={handleTargetSelectionChange}
            />
          </div>

          <div className="min-w-0 space-y-8" data-testid="fleet-state-column">
            {deployment.data && (
              <DeploymentRunPanel
                deployment={deployment.data}
                error={deployment.error}
                onRefresh={deployment.refresh}
              />
            )}

            {error && (
              <div
                role="alert"
                className="rounded-xl border border-amber-400/30 bg-amber-400/10 px-4 py-3 text-sm text-amber-100"
              >
                {data ? `Fleet refresh failed: ${error}. Showing the last successful snapshot.` : error}
              </div>
            )}

            {loading && <FleetLoadingState />}

            {!loading && data && (
              <div className="space-y-8">
                {groups.map(({ environment, targets }) => (
                  <section
                    key={environment}
                    aria-labelledby={`fleet-environment-${environment}`}
                    className="min-w-0"
                    data-testid={`fleet-environment-${environment}`}
                  >
                    <div className="mb-4 flex items-end justify-between gap-4">
                      <div>
                        <p className="text-xs font-semibold uppercase tracking-[0.22em] text-blue-300">
                          Environment
                        </p>
                        <h2 id={`fleet-environment-${environment}`} className="mt-1 text-xl font-semibold uppercase text-white">
                          {environment}
                        </h2>
                      </div>
                      <p className="text-sm text-slate-400">{targets.length} simulated targets</p>
                    </div>
                    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                      {targets.map((target) => (
                        <FleetTargetCard
                          key={target.target_id}
                          target={target}
                          selected={selectedTargetIds.includes(target.target_id)}
                        />
                      ))}
                    </div>
                  </section>
                ))}
              </div>
            )}

            {!loading && !data && !error && (
              <p className="rounded-xl border border-border bg-card p-6 text-center text-sm text-slate-400">
                No fleet snapshot is available yet.
              </p>
            )}
          </div>
        </div>

        <div className="mt-8 grid min-w-0 grid-cols-1 gap-3 sm:mt-10 sm:grid-cols-3">
          <div className="rounded-xl border border-border bg-card/70 p-4">
            <p className="text-xs uppercase tracking-wider text-slate-400">Targets</p>
            <p className="mt-1 text-xl font-semibold text-white">12 simulated vehicles</p>
          </div>
          <div className="rounded-xl border border-border bg-card/70 p-4">
            <p className="text-xs uppercase tracking-wider text-slate-400">Control boundary</p>
            <p className="mt-1 text-xl font-semibold text-white">Public simulated control plane</p>
            <p className="mt-2 text-xs leading-relaxed text-slate-400" data-testid="fleet-read-boundary">
              A successful fleet read does not prove that the protected submission pipeline is healthy.
            </p>
          </div>
          <div className="rounded-xl border border-border bg-card/70 p-4">
            <p className="text-xs uppercase tracking-wider text-slate-400">Fleet snapshot read</p>
            <p className="mt-1 text-xl font-semibold text-white" data-testid="fleet-refresh-time">
              {lastUpdated ? lastUpdated.toLocaleTimeString() : "Waiting…"}
            </p>
            <p className="mt-2 text-xs leading-relaxed text-slate-400">
              Browser read time; run completion and workflow state appear in the deployment panel.
            </p>
          </div>
        </div>

        <DeploymentHistory
          data={historyData}
          loading={historyLoading}
          error={historyError}
          activeDeploymentId={deployment.deploymentId}
          onSelect={handleHistorySelect}
        />
        <FleetBuildContext />
      </main>
    </div>
  );
}
