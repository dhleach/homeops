import { useCallback, useEffect, useState } from "react";
import { DeploymentSpecForm } from "./DeploymentSpecForm.jsx";

const REFRESH_INTERVAL_MS = 30_000;
const DEPLOYMENT_REFRESH_INTERVAL_MS = 2_000;
const ACTIVE_DEPLOYMENT_STORAGE_KEY = "homeops.activeFleetDeploymentId";
const ENVIRONMENTS = ["test", "stage", "prod"];

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
  succeeded: "Succeeded",
};

const WORKFLOW_STATUS_LABELS = {
  completed: "Completed",
  in_progress: "Running",
  pending: "Pending",
  queued: "Queued",
  requested: "Requested",
  waiting: "Waiting",
};

function fleetApiUrl(apiUrl) {
  return `${apiUrl.replace(/\/$/, "")}/deploy/api/fleet`;
}

function deploymentApiUrl(apiUrl, deploymentId) {
  return `${apiUrl.replace(/\/$/, "")}/deploy/api/deployments/${encodeURIComponent(deploymentId)}`;
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

  return { data, loading, error, lastUpdated, refresh: () => refresh() };
}

function readStoredDeploymentId() {
  try {
    return window.localStorage.getItem(ACTIVE_DEPLOYMENT_STORAGE_KEY);
  } catch {
    return null;
  }
}

function storeDeploymentId(deploymentId) {
  try {
    if (deploymentId) {
      window.localStorage.setItem(ACTIVE_DEPLOYMENT_STORAGE_KEY, deploymentId);
    } else {
      window.localStorage.removeItem(ACTIVE_DEPLOYMENT_STORAGE_KEY);
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
      const terminal = payload?.status === "succeeded" || payload?.status === "failed";
      if (!terminal) {
        timer = window.setTimeout(poll, DEPLOYMENT_REFRESH_INTERVAL_MS);
      }
    };
    poll();

    return () => {
      disposed = true;
      window.clearTimeout(timer);
    };
  }, [deploymentId, refresh]);

  const trackDeployment = useCallback((nextDeploymentId) => {
    storeDeploymentId(nextDeploymentId);
    setDeploymentId(nextDeploymentId || null);
    setData(null);
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

function StatusPill({ status }) {
  const statusClass = status === "failed"
    ? "border-red-400/30 bg-red-400/10 text-red-300"
    : status === "queued" || status === "applying" || status === "pending"
      ? "border-amber-400/30 bg-amber-400/10 text-amber-200"
      : "border-emerald-400/30 bg-emerald-400/10 text-emerald-300";

  return (
    <span className={`rounded-full border px-2.5 py-1 text-xs font-medium ${statusClass}`}>
      {displayStatus(status)}
    </span>
  );
}

function FleetTargetCard({ target }) {
  const synchronized = target.desired_digest === target.observed_digest;
  const cardTitleId = `fleet-target-${target.target_id}`;

  return (
    <article
      aria-labelledby={cardTitleId}
      className="flex min-h-[19rem] flex-col rounded-2xl border border-border bg-card p-5 shadow-lg shadow-slate-950/10"
      data-testid="fleet-target-card"
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.2em] text-blue-300">
            {target.environment}
          </p>
          <h3 id={cardTitleId} className="mt-1 font-mono text-sm font-semibold text-white">
            {target.target_id}
          </h3>
        </div>
        <StatusPill status={target.status} />
      </div>

      <div className="mt-5 flex items-center gap-4 rounded-xl border border-border/70 bg-slate-950/20 p-3">
        <ProfileGlyph
          profile={target.observed}
          label={`Observed ${profileText(target.observed)}`}
        />
        <div>
          <p className="text-sm font-medium text-slate-200">Simulated target</p>
          <p className="text-xs text-slate-500">{target.label}</p>
        </div>
        <span className="ml-auto rounded-full border border-slate-600/80 px-2 py-1 text-[10px] font-semibold uppercase tracking-wider text-slate-400">
          SIM
        </span>
      </div>

      <dl className="mt-5 space-y-3 text-xs">
        <div className="flex items-start justify-between gap-3">
          <dt className="text-slate-500">Desired</dt>
          <dd className="flex items-center gap-2 text-right text-slate-300">
            <ProfileGlyph profile={target.desired} label={`Desired ${profileText(target.desired)}`} />
            <span>
              <span className="block">{profileText(target.desired)}</span>
              <code className="text-slate-500" title={target.desired_digest}>
                {formatDigest(target.desired_digest)}
              </code>
            </span>
          </dd>
        </div>
        <div className="flex items-start justify-between gap-3">
          <dt className="text-slate-500">Observed</dt>
          <dd className="flex items-center gap-2 text-right text-slate-300">
            <ProfileGlyph profile={target.observed} label={`Observed ${profileText(target.observed)}`} />
            <span>
              <span className="block">{profileText(target.observed)}</span>
              <code className="text-slate-500" title={target.observed_digest}>
                {formatDigest(target.observed_digest)}
              </code>
            </span>
          </dd>
        </div>
      </dl>

      <div className="mt-auto flex items-center justify-between gap-3 border-t border-border/70 pt-4 text-xs">
        <span className={synchronized ? "text-emerald-300" : "text-amber-200"}>
          {synchronized ? "Desired = observed" : "Desired / observed drift"}
        </span>
        {target.last_error && (
          <span className="max-w-[10rem] truncate text-red-300" title={target.last_error}>
            {target.last_error}
          </span>
        )}
      </div>
    </article>
  );
}

function FleetLoadingState() {
  return (
    <div
      aria-label="Loading simulated fleet"
      className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3"
      data-testid="fleet-loading"
    >
      {[...Array(12)].map((_, index) => (
        <div key={index} className="h-[19rem] animate-pulse rounded-2xl border border-border bg-card" />
      ))}
    </div>
  );
}

function formatTimestamp(value) {
  if (!value) return "Unavailable";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function deploymentStatusLabel(deployment) {
  if (deployment.workflow?.status) {
    return WORKFLOW_STATUS_LABELS[deployment.workflow.status] ?? displayStatus(deployment.status);
  }
  return displayStatus(deployment.status);
}

function DeploymentRunPanel({ deployment, error, onRefresh }) {
  const verifiedTargets = deployment.targets.filter(
    (target) => deployment.target_ids.includes(target.target_id)
      && target.desired_digest === target.observed_digest,
  );
  const jobs = deployment.workflow?.jobs ?? [];

  return (
    <section
      aria-labelledby="deployment-run-heading"
      className="mb-10 rounded-2xl border border-border bg-card/70 p-5 shadow-lg shadow-slate-950/10 sm:p-6"
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
            State is reconstructed from GitHub Actions and the observed simulated fleet.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <StatusPill status={deployment.status} />
          <button
            type="button"
            onClick={onRefresh}
            className="rounded-lg border border-border px-3 py-2 text-xs font-medium text-slate-300 hover:border-blue-500/50 hover:text-blue-300"
          >
            Refresh run
          </button>
        </div>
      </div>

      <div className="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div className="rounded-lg border border-border bg-slate-950/30 p-3">
          <p className="text-xs uppercase tracking-wider text-slate-500">Workflow state</p>
          <p className="mt-1 text-sm font-semibold text-slate-200">{deploymentStatusLabel(deployment)}</p>
          {deployment.workflow?.conclusion && (
            <p className="mt-1 text-xs text-slate-500">Conclusion: {deployment.workflow.conclusion}</p>
          )}
        </div>
        <div className="rounded-lg border border-border bg-slate-950/30 p-3">
          <p className="text-xs uppercase tracking-wider text-slate-500">Verified targets</p>
          <p className="mt-1 text-sm font-semibold text-slate-200">
            {verifiedTargets.length} / {deployment.target_ids.length}
          </p>
        </div>
        <div className="rounded-lg border border-border bg-slate-950/30 p-3">
          <p className="text-xs uppercase tracking-wider text-slate-500">Created</p>
          <p className="mt-1 text-sm text-slate-300">{formatTimestamp(deployment.workflow_created_at ?? deployment.created_at)}</p>
        </div>
        <div className="rounded-lg border border-border bg-slate-950/30 p-3">
          <p className="text-xs uppercase tracking-wider text-slate-500">Last updated</p>
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
          >
            GitHub Actions run
          </a>
        )}
      </div>

      {(deployment.error || deployment.workflow_error || error) && (
        <div role="alert" className="mt-5 rounded-lg border border-red-400/30 bg-red-400/10 p-3 text-sm text-red-200">
          {deployment.error || deployment.workflow_error || error}
        </div>
      )}

      {jobs.length > 0 && (
        <div className="mt-5 border-t border-border/70 pt-4">
          <h3 className="text-sm font-semibold text-slate-200">Workflow jobs</h3>
          <ul className="mt-3 space-y-2 text-xs text-slate-300">
            {jobs.map((job) => (
              <li key={job.id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border bg-slate-950/30 px-3 py-2">
                <span>{job.name}</span>
                <span className={job.conclusion === "success" ? "text-emerald-300" : job.conclusion ? "text-red-300" : "text-amber-200"}>
                  {job.conclusion ?? job.status}
                  {job.failed_step ? ` — ${job.failed_step}` : ""}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

export function FleetDeployView({ apiUrl }) {
  const { data, loading, error, lastUpdated, refresh } = useFleet(apiUrl);
  const deployment = useDeployment(apiUrl);
  const groups = groupTargets(data?.targets ?? []);

  const handleSubmitted = useCallback((submission) => {
    deployment.trackDeployment(submission?.deployment_id);
    refresh();
  }, [deployment, refresh]);

  useEffect(() => {
    if (deployment.data?.status === "succeeded" || deployment.data?.status === "failed") {
      refresh();
    }
  }, [deployment.data?.status, refresh]);

  return (
    <div className="min-h-screen bg-surface text-slate-100">
      <header className="border-b border-border px-6 py-5">
        <div className="mx-auto flex max-w-6xl flex-col gap-5 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-start gap-3">
            <span aria-hidden="true" className="mt-1 text-2xl">🚚</span>
            <div>
              <p className="text-xs font-semibold uppercase tracking-[0.22em] text-blue-300">
                HomeOps / Deploy
              </p>
              <h1 className="mt-1 text-2xl font-bold tracking-tight text-white sm:text-3xl">
                Fleet Deploy Lab
              </h1>
              <p className="mt-2 max-w-2xl text-sm text-slate-400">
                A read-only view of the twelve simulated vehicles used to demonstrate safe deployment state.
              </p>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <a
              href="/"
              className="rounded-xl border border-border px-4 py-2 text-sm font-medium text-slate-300 transition-colors hover:border-blue-500/50 hover:text-blue-300"
            >
              ← HomeOps dashboard
            </a>
            <button
              type="button"
              onClick={refresh}
              className="rounded-xl border border-blue-500/40 bg-blue-500/10 px-4 py-2 text-sm font-medium text-blue-300 transition-colors hover:bg-blue-500/20"
            >
              Refresh fleet
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto w-full max-w-6xl px-6 py-10">
        <div className="mb-10 grid grid-cols-1 gap-3 sm:grid-cols-3">
          <div className="rounded-xl border border-border bg-card/70 p-4">
            <p className="text-xs uppercase tracking-wider text-slate-500">Targets</p>
            <p className="mt-1 text-xl font-semibold text-white">12 simulated vehicles</p>
          </div>
          <div className="rounded-xl border border-border bg-card/70 p-4">
            <p className="text-xs uppercase tracking-wider text-slate-500">Control boundary</p>
            <p className="mt-1 text-xl font-semibold text-white">Public read-only</p>
          </div>
          <div className="rounded-xl border border-border bg-card/70 p-4">
            <p className="text-xs uppercase tracking-wider text-slate-500">Last refresh</p>
            <p className="mt-1 text-xl font-semibold text-white">
              {lastUpdated ? lastUpdated.toLocaleTimeString() : "Waiting…"}
            </p>
          </div>
        </div>

        <DeploymentSpecForm apiUrl={apiUrl} onSubmitted={handleSubmitted} />

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
            className="mb-8 rounded-xl border border-amber-400/30 bg-amber-400/10 px-4 py-3 text-sm text-amber-100"
          >
            {data ? `Fleet refresh failed: ${error}. Showing the last successful snapshot.` : error}
          </div>
        )}

        {loading && <FleetLoadingState />}

        {!loading && data && (
          <div className="space-y-12">
            {groups.map(({ environment, targets }) => (
              <section key={environment} aria-labelledby={`fleet-environment-${environment}`}>
                <div className="mb-4 flex items-end justify-between gap-4">
                  <div>
                    <p className="text-xs font-semibold uppercase tracking-[0.22em] text-blue-300">
                      Environment
                    </p>
                    <h2 id={`fleet-environment-${environment}`} className="mt-1 text-xl font-semibold uppercase text-white">
                      {environment}
                    </h2>
                  </div>
                  <p className="text-sm text-slate-500">{targets.length} simulated targets</p>
                </div>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
                  {targets.map((target) => (
                    <FleetTargetCard key={target.target_id} target={target} />
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
      </main>
    </div>
  );
}
