import { useMemo, useState } from "react";

// These finite choices mirror deploy_demo/deployment_spec.py. The browser may
// submit only this closed data structure; the backend validator remains the
// authority at the trusted workflow boundary.
const DEPLOYMENT_SPEC_VERSION = 1;
const ENVIRONMENTS = ["test", "stage", "prod"];
const COLORS = ["blue", "green", "orange", "purple"];
const SHAPES = ["circle", "hexagon", "square", "triangle"];
const IMPLEMENTATIONS = ["python", "ansible"];
const STRATEGIES = ["all_at_once", "rolling", "canary"];
const FAILURE_MODES = ["abort", "rollback"];
const FAILURE_MODES_BY_STRATEGY = {
  all_at_once: ["abort"],
  rolling: FAILURE_MODES,
  canary: FAILURE_MODES,
};
const TARGETS_BY_ENVIRONMENT = Object.fromEntries(
  ENVIRONMENTS.map((environment) => [
    environment,
    [1, 2, 3, 4].map((index) => `${environment}-vehicle-${String(index).padStart(2, "0")}`),
  ]),
);
const TARGET_ORDER = ENVIRONMENTS.flatMap((environment) => TARGETS_BY_ENVIRONMENT[environment]);
const KNOWN_TARGET_IDS = new Set(TARGET_ORDER);
const DEPLOYMENT_ID_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/;

function submitUrl(apiUrl) {
  return `${apiUrl.replace(/\/$/, "")}/deploy/api/deployments/submit`;
}

const DEFAULT_FORM = {
  deployment_id: "demo-preview-001",
  target_selection: "environment",
  environment: "test",
  target_ids: [],
  profile_color: "blue",
  profile_shape: "circle",
  implementation: "python",
  strategy: "rolling",
  failure_mode: "rollback",
};

function labelize(value) {
  return value.replaceAll("_", " ");
}

function sortedTargetIds(targetIds) {
  return [...targetIds].sort((left, right) => TARGET_ORDER.indexOf(left) - TARGET_ORDER.indexOf(right));
}

function buildDeploymentSpec(form) {
  const selector = form.target_selection === "environment"
    ? { environment: form.environment }
    : { target_ids: sortedTargetIds(form.target_ids) };

  return {
    deployment_id: form.deployment_id,
    ...selector,
    failure_mode: form.failure_mode,
    implementation: form.implementation,
    profile: {
      color: form.profile_color,
      shape: form.profile_shape,
    },
    schema_version: DEPLOYMENT_SPEC_VERSION,
    strategy: form.strategy,
  };
}

function validateForm(form) {
  const errors = {};

  if (!form.deployment_id) {
    errors.deployment_id = "Enter a deployment ID.";
  } else if (!DEPLOYMENT_ID_PATTERN.test(form.deployment_id)) {
    errors.deployment_id = "Use lowercase letters, digits, and internal hyphens only.";
  }

  if (!ENVIRONMENTS.includes(form.environment)) {
    errors.environment = "Choose a known environment.";
  }

  if (form.target_selection === "target_ids") {
    if (form.target_ids.length === 0) {
      errors.target_ids = "Select at least one simulated target.";
    } else if (
      form.target_ids.length > 12
      || form.target_ids.some((targetId) => !KNOWN_TARGET_IDS.has(targetId))
    ) {
      errors.target_ids = "Choose up to twelve known simulated targets.";
    }
  }

  if (!COLORS.includes(form.profile_color) || !SHAPES.includes(form.profile_shape)) {
    errors.profile = "Choose a supported color and shape.";
  }
  if (!IMPLEMENTATIONS.includes(form.implementation)) {
    errors.implementation = "Choose a supported implementation.";
  }
  if (!STRATEGIES.includes(form.strategy)) {
    errors.strategy = "Choose a supported strategy.";
  }
  if (!FAILURE_MODES_BY_STRATEGY[form.strategy]?.includes(form.failure_mode)) {
    errors.failure_mode = `${labelize(form.strategy)} supports: ${FAILURE_MODES_BY_STRATEGY[form.strategy]?.join(", ") ?? "no failure modes"}.`;
  }

  return errors;
}

function FieldError({ id, message }) {
  if (!message) return null;
  return <p id={id} className="mt-1 text-xs text-red-300">{message}</p>;
}

function OptionSelect({ id, label, value, options, onChange, error, disabled = false }) {
  const errorId = `${id}-error`;
  return (
    <div>
      <label htmlFor={id} className="block text-xs font-semibold uppercase tracking-wider text-slate-400">
        {label}
      </label>
      <select
        id={id}
        value={value}
        onChange={onChange}
        disabled={disabled}
        aria-invalid={Boolean(error)}
        aria-describedby={error ? errorId : undefined}
        className="mt-2 w-full rounded-lg border border-border bg-slate-950/70 px-3 py-2.5 text-sm text-slate-100 outline-none transition-colors focus:border-blue-400 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {options.map((option) => (
          <option key={option} value={option}>{labelize(option)}</option>
        ))}
      </select>
      <FieldError id={errorId} message={error} />
    </div>
  );
}

export function DeploymentSpecForm({ apiUrl, onSubmitted }) {
  const [form, setForm] = useState(DEFAULT_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [submission, setSubmission] = useState(null);
  const [submitError, setSubmitError] = useState(null);
  const errors = useMemo(() => validateForm(form), [form]);
  const preview = useMemo(() => buildDeploymentSpec(form), [form]);

  function updateField(field) {
    return (event) => setForm((current) => ({ ...current, [field]: event.target.value }));
  }

  function selectTargetMode(event) {
    setForm((current) => ({ ...current, target_selection: event.target.value }));
  }

  function toggleTarget(targetId) {
    setForm((current) => ({
      ...current,
      target_ids: current.target_ids.includes(targetId)
        ? current.target_ids.filter((selected) => selected !== targetId)
        : [...current.target_ids, targetId],
    }));
  }

  async function submitDeployment(event) {
    event.preventDefault();
    if (Object.keys(errors).length > 0 || submitting) return;

    setSubmitting(true);
    setSubmitError(null);
    try {
      const response = await fetch(submitUrl(apiUrl), {
        method: "POST",
        cache: "no-store",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: JSON.stringify(preview),
      });
      const body = await response.json().catch(() => null);
      if (!response.ok) {
        const detail = typeof body?.detail === "string"
          ? body.detail
          : `Fleet API returned HTTP ${response.status}`;
        throw new Error(detail);
      }
      setSubmission(body);
      onSubmitted?.(body);
    } catch (requestError) {
      setSubmitError(requestError instanceof Error ? requestError.message : "Unable to submit deployment");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section
      aria-labelledby="deployment-spec-heading"
      className="mb-10 rounded-2xl border border-blue-400/30 bg-blue-400/5 p-5 shadow-lg shadow-slate-950/10 sm:p-6"
    >
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.22em] text-blue-300">
            Constrained control plane
          </p>
          <h2 id="deployment-spec-heading" className="mt-1 text-xl font-semibold text-white">
            Build a deployment preview
          </h2>
          <p className="mt-2 max-w-2xl text-sm text-slate-400">
            Choose only from the fixed simulated fleet and finite profile options. Submission creates one
            validated manifest and dispatches the trusted workflow; no credential enters the browser.
          </p>
        </div>
        <span className="w-fit rounded-full border border-emerald-400/30 bg-emerald-400/10 px-3 py-1 text-xs font-medium text-emerald-200">
          Simulated fleet
        </span>
      </div>

      <form
        className="mt-6 grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(18rem,0.8fr)]"
        onSubmit={submitDeployment}
      >
        <div className="space-y-6">
          <div>
            <label htmlFor="deployment-id" className="block text-xs font-semibold uppercase tracking-wider text-slate-400">
              Deployment ID
            </label>
            <input
              id="deployment-id"
              type="text"
              value={form.deployment_id}
              onChange={updateField("deployment_id")}
              autoComplete="off"
              spellCheck="false"
              aria-invalid={Boolean(errors.deployment_id)}
              aria-describedby={errors.deployment_id ? "deployment-id-error" : undefined}
              className="mt-2 w-full rounded-lg border border-border bg-slate-950/70 px-3 py-2.5 font-mono text-sm text-slate-100 outline-none transition-colors focus:border-blue-400"
            />
            <FieldError id="deployment-id-error" message={errors.deployment_id} />
          </div>

          <fieldset>
            <legend className="block text-xs font-semibold uppercase tracking-wider text-slate-400">
              Target selection
            </legend>
            <div className="mt-2 grid gap-2 sm:grid-cols-2">
              {[
                ["environment", "Environment", "Expand one complete environment"],
                ["target_ids", "Individual targets", "Choose specific simulated vehicles"],
              ].map(([value, label, description]) => (
                <label
                  key={value}
                  className="flex cursor-pointer items-start gap-3 rounded-lg border border-border bg-slate-950/40 p-3 text-sm text-slate-200 transition-colors has-[:checked]:border-blue-400/60 has-[:checked]:bg-blue-400/10"
                >
                  <input
                    type="radio"
                    name="target-selection"
                    value={value}
                    checked={form.target_selection === value}
                    onChange={selectTargetMode}
                    className="mt-0.5 accent-blue-400"
                  />
                  <span>
                    <span className="block font-medium">{label}</span>
                    <span className="mt-1 block text-xs text-slate-500">{description}</span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>

          <div className="grid gap-4 sm:grid-cols-2">
            <OptionSelect
              id="deployment-environment"
              label="Environment"
              value={form.environment}
              options={ENVIRONMENTS}
              onChange={updateField("environment")}
              error={errors.environment}
              disabled={form.target_selection !== "environment"}
            />
            <OptionSelect
              id="deployment-implementation"
              label="Implementation"
              value={form.implementation}
              options={IMPLEMENTATIONS}
              onChange={updateField("implementation")}
              error={errors.implementation}
            />
            <OptionSelect
              id="deployment-strategy"
              label="Strategy"
              value={form.strategy}
              options={STRATEGIES}
              onChange={updateField("strategy")}
              error={errors.strategy}
            />
            <OptionSelect
              id="deployment-failure-mode"
              label="Failure mode"
              value={form.failure_mode}
              options={FAILURE_MODES}
              onChange={updateField("failure_mode")}
              error={errors.failure_mode}
            />
            <OptionSelect
              id="deployment-color"
              label="Profile color"
              value={form.profile_color}
              options={COLORS}
              onChange={updateField("profile_color")}
              error={errors.profile}
            />
            <OptionSelect
              id="deployment-shape"
              label="Profile shape"
              value={form.profile_shape}
              options={SHAPES}
              onChange={updateField("profile_shape")}
              error={errors.profile}
            />
          </div>

          <fieldset disabled={form.target_selection !== "target_ids"}>
            <legend className="block text-xs font-semibold uppercase tracking-wider text-slate-400">
              Individual target IDs
            </legend>
            <p className="mt-1 text-xs text-slate-500">
              Enabled when Individual targets is selected; the contract allows at most twelve.
            </p>
            <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {TARGET_ORDER.map((targetId) => (
                <label
                  key={targetId}
                  className="flex items-center gap-2 rounded-lg border border-border bg-slate-950/40 px-3 py-2 text-xs text-slate-300"
                >
                  <input
                    type="checkbox"
                    checked={form.target_ids.includes(targetId)}
                    onChange={() => toggleTarget(targetId)}
                    aria-label={targetId}
                    className="accent-blue-400"
                  />
                  <span className="font-mono">{targetId}</span>
                </label>
              ))}
            </div>
            <FieldError id="target-ids-error" message={errors.target_ids} />
          </fieldset>
        </div>

        <aside className="flex flex-col rounded-xl border border-border bg-slate-950/50 p-4">
          <div>
            <p className="text-xs font-semibold uppercase tracking-wider text-slate-400">
              Canonical preview
            </p>
            <p className="mt-1 text-xs text-slate-500">
              The field names and values below are the DeploymentSpec payload.
            </p>
          </div>
          <pre
            aria-label="DeploymentSpec preview"
            data-testid="deployment-spec-preview"
            className="mt-4 min-h-[19rem] overflow-x-auto rounded-lg border border-border bg-slate-950 p-4 text-xs leading-6 text-emerald-200"
          >
            {JSON.stringify(preview, null, 2)}
          </pre>

          {Object.keys(errors).length > 0 && (
            <div
              role="alert"
              data-testid="deployment-validation-errors"
              className="mt-4 rounded-lg border border-red-400/30 bg-red-400/10 p-3 text-xs text-red-200"
            >
              <p className="font-semibold">Preview needs attention</p>
              <ul className="mt-2 list-disc space-y-1 pl-4">
                {Object.values(errors).map((error) => <li key={error}>{error}</li>)}
              </ul>
            </div>
          )}

          <button
            type="submit"
            disabled={submitting || Object.keys(errors).length > 0}
            data-testid="deploy-submit"
            className="mt-5 w-full rounded-lg border border-blue-400/40 bg-blue-400/10 px-4 py-2.5 text-sm font-semibold text-blue-200 transition-colors hover:bg-blue-400/20 disabled:cursor-not-allowed disabled:border-slate-600 disabled:bg-slate-800 disabled:text-slate-500"
          >
            {submitting ? "Submitting…" : "Submit deployment"}
          </button>
          {submitError && (
            <p role="alert" className="mt-3 rounded-lg border border-red-400/30 bg-red-400/10 p-3 text-xs text-red-200">
              {submitError}
            </p>
          )}
          {submission && (
            <div role="status" className="mt-3 rounded-lg border border-emerald-400/30 bg-emerald-400/10 p-3 text-xs text-emerald-100">
              <p className="font-semibold">Deployment {submission.deployment_id} accepted</p>
              <p className="mt-1">Workflow: {submission.dispatch_status}</p>
              {submission.dispatch_status === "failed" && (
                <p className="mt-1">The manifest is retained; submit the same ID again to retry dispatch.</p>
              )}
              {submission.workflow_url && (
                <a
                  href={submission.workflow_url}
                  target="_blank"
                  rel="noreferrer"
                  className="mt-2 inline-block text-emerald-200 underline"
                >
                  View workflow run
                </a>
              )}
            </div>
          )}
        </aside>
      </form>
    </section>
  );
}
