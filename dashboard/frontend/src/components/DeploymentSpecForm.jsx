import { useEffect, useMemo, useState } from "react";
import { ImplementationSourcePanel } from "./ImplementationSourcePanel.jsx";

// These finite choices mirror deploy_demo/deployment_spec.py. The browser may
// submit only this closed data structure; the backend validator remains the
// authority at the trusted workflow boundary.
const DEPLOYMENT_SPEC_VERSION = 1;
const ENVIRONMENTS = ["test", "stage", "prod"];
const COLORS = ["blue", "green", "orange", "purple"];
const SHAPES = ["circle", "hexagon", "square", "triangle"];
const DEFAULT_CAPABILITIES = [
  { implementation: "python", strategy: "all_at_once", failure_mode: "abort" },
  { implementation: "python", strategy: "canary", failure_mode: "abort" },
  { implementation: "python", strategy: "canary", failure_mode: "rollback" },
  { implementation: "ansible", strategy: "all_at_once", failure_mode: "abort" },
];
const TARGETS_BY_ENVIRONMENT = Object.fromEntries(
  ENVIRONMENTS.map((environment) => [
    environment,
    [1, 2, 3, 4].map((index) => `${environment}-vehicle-${String(index).padStart(2, "0")}`),
  ]),
);
const TARGET_ORDER = ENVIRONMENTS.flatMap((environment) => TARGETS_BY_ENVIRONMENT[environment]);
const KNOWN_TARGET_IDS = new Set(TARGET_ORDER);
const DEPLOYMENT_ID_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/;
const DEPLOYMENT_ATTEMPT_STORAGE_KEY = "homeops.fleetDeployAttemptId";
const PREVIOUS_ATTEMPT_STORAGE_KEY = "homeops.previousFleetDeployment";
const PREVIOUS_ATTEMPT_FIELDS = [
  "deployment_id",
  "target_ids",
  "desired",
  "desired_digest",
  "status",
  "error",
  "created_at",
  "updated_at",
  "verification",
  "verified",
  "targets",
  "request_summary",
  "idempotent",
  "manifest_path",
  "manifest_sha256",
  "manifest_commit_sha",
  "manifest_commit_url",
  "workflow_run_id",
  "workflow_url",
  "workflow_status",
  "workflow_conclusion",
  "workflow_created_at",
  "workflow_updated_at",
  "workflow_error",
  "workflow",
  "dispatch_status",
  "dispatch_error",
  "rollback_status",
  "rollback_target_ids",
  "rollback_error",
  "error_code",
  "error_recovery",
  "retry_after_seconds",
];

const ERROR_CLASS_LABELS = {
  capacity_full: "Fleet busy",
  invalid_deployment_spec: "Request rejected",
  manifest_commit_failed: "Manifest commit failed",
  response_mismatch: "Response identity mismatch",
  service_unavailable: "Service unavailable",
  submission_cooldown: "Cooldown active",
  submission_failed: "Submission failed",
  verification_failed: "Verification failed",
  workflow_dispatch_failed: "Workflow dispatch failed",
  workflow_failed: "Workflow failed",
  workflow_unavailable: "Workflow state unavailable",
};

function submitUrl(apiUrl) {
  return `${apiUrl.replace(/\/$/, "")}/deploy/api/deployments/submit`;
}

const DEFAULT_FORM = {
  deployment_id: "",
  target_selection: "environment",
  environment: "test",
  target_ids: [],
  profile_color: "blue",
  profile_shape: "circle",
  implementation: "python",
  strategy: "all_at_once",
  failure_mode: "abort",
  failure_target_id: "none",
};

function normalizeCapabilities(capabilities) {
  if (!Array.isArray(capabilities) || capabilities.length === 0) return DEFAULT_CAPABILITIES;
  const normalized = capabilities.filter((capability) => (
    capability
    && typeof capability.implementation === "string"
    && typeof capability.strategy === "string"
    && typeof capability.failure_mode === "string"
  ));
  return normalized.length > 0 ? normalized : DEFAULT_CAPABILITIES;
}

function capabilitiesFor(capabilities, form) {
  return capabilities.filter((capability) => (
    capability.implementation === form.implementation
    && capability.strategy === form.strategy
  ));
}

function supportsCapability(form, capabilities) {
  return capabilities.some((capability) => (
    capability.implementation === form.implementation
    && capability.strategy === form.strategy
    && capability.failure_mode === form.failure_mode
  ));
}

function uniqueCapabilityValues(capabilities, field) {
  return [...new Set(capabilities.map((capability) => capability[field]))];
}

function readStoredAttemptId() {
  try {
    const stored = window.sessionStorage.getItem(DEPLOYMENT_ATTEMPT_STORAGE_KEY);
    return stored && DEPLOYMENT_ID_PATTERN.test(stored) ? stored : null;
  } catch {
    return null;
  }
}

function writeStoredAttemptId(deploymentId) {
  try {
    window.sessionStorage.setItem(DEPLOYMENT_ATTEMPT_STORAGE_KEY, deploymentId);
  } catch {
    // Private browsing and disabled storage should not block the demo.
  }
}

function readStoredPreviousAttempt() {
  try {
    const stored = window.sessionStorage.getItem(PREVIOUS_ATTEMPT_STORAGE_KEY);
    return stored ? JSON.parse(stored) : null;
  } catch {
    return null;
  }
}

function storePreviousAttempt(attempt) {
  try {
    window.sessionStorage.setItem(PREVIOUS_ATTEMPT_STORAGE_KEY, JSON.stringify(attempt));
  } catch {
    // Private browsing and disabled storage should not block the demo.
  }
}

function errorClassLabel(code) {
  return ERROR_CLASS_LABELS[code] ?? "Deployment submission error";
}

function normalizeSubmissionError(response, body) {
  const detail = body?.detail;
  const retryAfterHeader = Number.parseInt(response.headers.get("Retry-After") ?? "", 10);
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    return {
      code: detail.code ?? "submission_failed",
      message: detail.message ?? `Fleet API returned HTTP ${response.status}`,
      recovery: detail.recovery ?? "This attempt ID is retained. Retry to reconcile the same server record.",
      retry_after_seconds: detail.retry_after_seconds ?? (
        Number.isFinite(retryAfterHeader) ? retryAfterHeader : null
      ),
    };
  }

  const code = response.status === 429
    ? "capacity_full"
    : response.status === 422
      ? "invalid_deployment_spec"
      : response.status >= 500
        ? "service_unavailable"
        : "submission_failed";
  return {
    code,
    message: typeof detail === "string" ? detail : `Fleet API returned HTTP ${response.status}`,
    recovery: "This attempt ID is retained. Retry to reconcile the same server record.",
    retry_after_seconds: Number.isFinite(retryAfterHeader) ? retryAfterHeader : null,
  };
}

function compactAttempt(attempt) {
  if (!attempt) return null;
  const normalizedAttempt = {
    ...attempt,
    error: attempt.error ?? attempt.message ?? null,
    error_code: attempt.error_code ?? attempt.code ?? null,
    error_recovery: attempt.error_recovery ?? attempt.recovery ?? null,
  };
  return Object.fromEntries(PREVIOUS_ATTEMPT_FIELDS.map((field) => [
    field,
    normalizedAttempt[field] ?? null,
  ]));
}

function requestSummaryLabel(summary) {
  if (!summary) return "Request details unavailable";
  const targets = summary.environment
    ? `${summary.environment.toUpperCase()} environment`
    : `${summary.target_ids?.length ?? 0} selected vehicles`;
  const failure = summary.failure_target_id
    ? ` · injected verification failure at ${targetDisplayLabel(summary.failure_target_id)}`
    : "";
  return `${targets} · ${summary.implementation ?? "unknown"} / ${summary.strategy ?? "unknown"} / ${summary.failure_mode ?? "unknown"}${failure}`;
}

function previousAttemptTargetLabel(attempt) {
  const targetIds = Array.isArray(attempt?.target_ids) ? attempt.target_ids : [];
  if (targetIds.length === 0) return "Target set unavailable";
  return targetIds.map(targetDisplayLabel).join(", ");
}

function previousAttemptWorkflowLabel(attempt) {
  const status = attempt?.workflow_status ?? attempt?.workflow?.status;
  const conclusion = attempt?.workflow_conclusion ?? attempt?.workflow?.conclusion;
  if (!status && !conclusion) return null;
  return [status, conclusion].filter(Boolean).join(" · ");
}

function previousAttemptVerificationLabel(attempt) {
  if (!attempt) return null;
  const targetIds = Array.isArray(attempt.target_ids) ? attempt.target_ids : [];
  const targets = Array.isArray(attempt.targets) ? attempt.targets : [];
  const verifiedCount = targets.filter((target) => (
    targetIds.includes(target.target_id)
    && target.desired_digest === target.observed_digest
  )).length;
  if (targetIds.length === 0 && !attempt.verification) return null;
  const count = targets.length > 0 ? ` (${verifiedCount} / ${targetIds.length} targets)` : "";
  return `${attempt.verification ?? "pending"}${count}`;
}

function formatAttemptTimestamp(value) {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function createDeploymentId() {
  const uuid = globalThis.crypto?.randomUUID?.();
  if (uuid) return `demo-${uuid.replaceAll("-", "")}`;

  const values = new Uint32Array(3);
  if (globalThis.crypto?.getRandomValues) {
    globalThis.crypto.getRandomValues(values);
  } else {
    values[0] = Date.now();
    values[1] = Math.floor(Math.random() * 2 ** 32);
    values[2] = Math.floor(Math.random() * 2 ** 32);
  }
  return `demo-${Date.now().toString(36)}-${Array.from(values)
    .map((value) => value.toString(36))
    .join("")}`;
}

function getOrCreateDeploymentId() {
  const stored = readStoredAttemptId();
  if (stored) return stored;
  const generated = createDeploymentId();
  writeStoredAttemptId(generated);
  return generated;
}

function createDefaultForm() {
  return { ...DEFAULT_FORM, deployment_id: getOrCreateDeploymentId() };
}

function labelize(value) {
  return value.replaceAll("_", " ");
}

function targetDisplayLabel(targetId) {
  const match = /^(test|stage|prod)-vehicle-(\d{2})$/.exec(targetId);
  return match ? `${match[1].toUpperCase()}-${match[2]}` : targetId;
}

function resolvedTargetIds(form) {
  if (form.target_selection === "environment") {
    return TARGETS_BY_ENVIRONMENT[form.environment] ?? [];
  }
  return sortedTargetIds(form.target_ids);
}

function deploymentTargetCount(form) {
  if (form.target_selection === "environment") {
    return TARGETS_BY_ENVIRONMENT[form.environment]?.length ?? 0;
  }
  return form.target_ids.length;
}

function deploymentActionLabel(form, submitting, retryable) {
  if (submitting) return "Submitting…";
  if (retryable) return "Retry same attempt";

  const targetCount = deploymentTargetCount(form);
  if (!targetCount) return "Select targets to deploy";
  if (form.target_selection === "environment") {
    return `Deploy to ${targetCount} ${form.environment} vehicles`;
  }
  return `Deploy to ${targetCount} selected ${targetCount === 1 ? "vehicle" : "vehicles"}`;
}

function sortedTargetIds(targetIds) {
  return [...targetIds].sort((left, right) => TARGET_ORDER.indexOf(left) - TARGET_ORDER.indexOf(right));
}

function buildDeploymentSpec(form) {
  const selector = form.target_selection === "environment"
    ? { environment: form.environment }
    : { target_ids: sortedTargetIds(form.target_ids) };

  const spec = {
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
  if (form.failure_target_id !== "none") {
    spec.failure_target_id = form.failure_target_id;
  }
  return spec;
}

function validateForm(form, capabilities) {
  const errors = {};

  if (!form.deployment_id) {
    errors.deployment_id = "A generated attempt ID is required.";
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
  if (!supportsCapability(form, capabilities)) {
    errors.capability = "This implementation, strategy, and failure mode combination is not supported.";
  }
  if (
    form.failure_target_id !== "none"
    && !resolvedTargetIds(form).includes(form.failure_target_id)
  ) {
    errors.failure_target_id = "Choose a failure target from the resolved simulated targets.";
  }

  return errors;
}

function FieldError({ id, message }) {
  if (!message) return null;
  return <p id={id} className="mt-1 text-xs text-red-300">{message}</p>;
}

function OptionSelect({
  id,
  label,
  value,
  options,
  onChange,
  error,
  disabled = false,
  disabledReason,
  optionLabels = {},
}) {
  const errorId = `${id}-error`;
  const disabledReasonId = `${id}-disabled-help`;
  const describedBy = [error ? errorId : null, disabled && disabledReason ? disabledReasonId : null]
    .filter(Boolean)
    .join(" ") || undefined;
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
        aria-describedby={describedBy}
        className="mt-2 w-full rounded-lg border border-border bg-slate-950/70 px-3 py-2.5 text-sm text-slate-100 transition-colors focus:border-blue-400 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {options.map((option) => (
          <option key={option} value={option}>{optionLabels[option] ?? labelize(option)}</option>
        ))}
      </select>
      <FieldError id={errorId} message={error} />
      {disabled && disabledReason && (
        <p id={disabledReasonId} className="mt-1 text-xs text-slate-400">{disabledReason}</p>
      )}
    </div>
  );
}

export function DeploymentSpecForm({
  apiUrl,
  capabilities,
  activeDeployment,
  onSubmitted,
  onNewAttempt,
  onTargetSelectionChange,
}) {
  const availableCapabilities = useMemo(() => normalizeCapabilities(capabilities), [capabilities]);
  const [form, setForm] = useState(createDefaultForm);
  const [submitting, setSubmitting] = useState(false);
  const [submission, setSubmission] = useState(null);
  const [submitError, setSubmitError] = useState(null);
  const [previousAttempt, setPreviousAttempt] = useState(readStoredPreviousAttempt);
  const errors = useMemo(
    () => validateForm(form, availableCapabilities),
    [form, availableCapabilities],
  );
  const preview = useMemo(() => buildDeploymentSpec(form), [form]);
  const selectedTargetIds = useMemo(() => resolvedTargetIds(form), [form]);
  const implementationOptions = uniqueCapabilityValues(availableCapabilities, "implementation");
  const strategyOptions = uniqueCapabilityValues(
    availableCapabilities.filter((capability) => capability.implementation === form.implementation),
    "strategy",
  );
  const failureModeOptions = uniqueCapabilityValues(capabilitiesFor(availableCapabilities, form), "failure_mode");
  const failureTargetOptions = ["none", ...selectedTargetIds];

  useEffect(() => {
    onTargetSelectionChange?.(selectedTargetIds);
  }, [onTargetSelectionChange, selectedTargetIds]);

  function rememberPreviousAttempt(attempt) {
    const compact = compactAttempt(attempt);
    if (!compact || (!compact.deployment_id && !compact.error)) return;
    setPreviousAttempt(compact);
    storePreviousAttempt(compact);
  }

  function currentAttemptSnapshot() {
    if (activeDeployment?.deployment_id === form.deployment_id) return activeDeployment;
    if (submission?.deployment_id === form.deployment_id) return submission;
    if (submitError) {
      return {
        deployment_id: form.deployment_id,
        request_summary: preview,
        ...submitError,
      };
    }
    return null;
  }

  function beginNewAttempt() {
    rememberPreviousAttempt(currentAttemptSnapshot());
    const deploymentId = createDeploymentId();
    writeStoredAttemptId(deploymentId);
    setForm((current) => ({ ...current, deployment_id: deploymentId }));
    setSubmission(null);
    setSubmitError(null);
    onNewAttempt?.();
  }

  function prepareForConfigurationEdit() {
    const previous = currentAttemptSnapshot();
    if (!previous) return null;
    rememberPreviousAttempt(previous);
    const deploymentId = createDeploymentId();
    writeStoredAttemptId(deploymentId);
    setSubmission(null);
    setSubmitError(null);
    return deploymentId;
  }

  function updateImplementation(value) {
    if (value === form.implementation) return;
    const deploymentId = prepareForConfigurationEdit();
    setForm((current) => {
      const nextCapability = availableCapabilities.find(
        (capability) => capability.implementation === value,
      );
      return {
        ...current,
        implementation: value,
        strategy: nextCapability?.strategy ?? current.strategy,
        failure_mode: nextCapability?.failure_mode ?? current.failure_mode,
        ...(deploymentId ? { deployment_id: deploymentId } : {}),
      };
    });
  }

  function updateField(field) {
    return (event) => {
      const value = event.target.value;
      if (field === "implementation") {
        updateImplementation(value);
        return;
      }
      const deploymentId = prepareForConfigurationEdit();
      setForm((current) => {
        const next = { ...current, [field]: value };
        if (field === "strategy") {
          const nextCapability = availableCapabilities.find(
            (capability) => (
              capability.implementation === current.implementation
              && capability.strategy === value
            ),
          );
          if (nextCapability) next.failure_mode = nextCapability.failure_mode;
        }
        if (
          field === "environment"
          && next.failure_target_id !== "none"
          && !TARGETS_BY_ENVIRONMENT[value]?.includes(next.failure_target_id)
        ) {
          next.failure_target_id = "none";
        }
        if (deploymentId) next.deployment_id = deploymentId;
        return next;
      });
    };
  }

  function selectTargetMode(event) {
    const deploymentId = prepareForConfigurationEdit();
    setForm((current) => ({
      ...current,
      target_selection: event.target.value,
      target_ids: [],
      failure_target_id: "none",
      ...(deploymentId ? { deployment_id: deploymentId } : {}),
    }));
  }

  function toggleTarget(targetId) {
    const deploymentId = prepareForConfigurationEdit();
    setForm((current) => {
      const target_ids = current.target_ids.includes(targetId)
        ? current.target_ids.filter((selected) => selected !== targetId)
        : [...current.target_ids, targetId];
      return {
        ...current,
        target_ids,
        failure_target_id: target_ids.includes(current.failure_target_id)
          ? current.failure_target_id
          : "none",
        ...(deploymentId ? { deployment_id: deploymentId } : {}),
      };
    });
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
        throw normalizeSubmissionError(response, body);
      }
      if (body?.deployment_id !== form.deployment_id) {
        throw {
          code: "response_mismatch",
          message: "Fleet API returned a different attempt ID.",
          recovery: "Do not retry automatically. Start a new attempt after checking the server response.",
        };
      }
      setSubmission(body);
      onSubmitted?.(body);
    } catch (requestError) {
      setSubmitError(requestError?.code
        ? requestError
        : {
          code: "submission_failed",
          message: requestError instanceof Error ? requestError.message : "Unable to submit deployment",
          recovery: "This attempt ID is retained. Retry to reconcile the same server record.",
        });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section
      aria-labelledby="deployment-spec-heading"
      className="min-w-0 rounded-2xl border border-blue-400/30 bg-blue-400/5 p-4 shadow-lg shadow-slate-950/10 sm:p-6"
      data-testid="deployment-config-panel"
    >
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.22em] text-blue-300">
            Constrained control plane
          </p>
          <h2 id="deployment-spec-heading" className="mt-1 text-xl font-semibold text-white">
            Configure a deployment
          </h2>
          <p className="mt-2 max-w-2xl text-sm text-slate-400">
            Choose from the fixed simulated fleet and finite profile options. Deploying creates one
            validated manifest and dispatches the trusted workflow; credentials remain server-side.
          </p>
        </div>
        <span className="w-fit rounded-full border border-emerald-400/30 bg-emerald-400/10 px-3 py-1 text-xs font-medium text-emerald-200">
          Simulated fleet
        </span>
      </div>

      <form
        className="mt-6 min-w-0 space-y-6"
        onSubmit={submitDeployment}
      >
        <div className="space-y-6">
          <div className="rounded-lg border border-border bg-slate-950/40 p-3">
            <div className="flex items-baseline justify-between gap-3">
              <p className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                Attempt ID
              </p>
              <code
                className="max-w-[70%] truncate text-right text-xs text-slate-300"
                data-testid="deployment-attempt-id"
                title={form.deployment_id}
              >
                {form.deployment_id}
              </code>
            </div>
            <p className="mt-1 text-xs text-slate-400">
              Generated automatically and retained in this browser tab so retries address the same operation.
            </p>
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
                    className="mt-0.5 accent-blue-400 focus-visible:ring-2 focus-visible:ring-blue-300 focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                  />
                  <span>
                    <span className="block font-medium">{label}</span>
                    <span className="mt-1 block text-xs text-slate-400">{description}</span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>

          <div
            className="rounded-lg border border-blue-400/20 bg-blue-400/5 p-3"
            data-testid="deployment-resolved-targets"
            aria-live="polite"
          >
            <div className="flex items-baseline justify-between gap-3">
              <p className="text-xs font-semibold uppercase tracking-wider text-blue-200">
                Resolved targets
              </p>
              <p className="text-xs font-medium text-blue-100" data-testid="deployment-resolved-target-count">
                {selectedTargetIds.length} {selectedTargetIds.length === 1 ? "vehicle" : "vehicles"}
              </p>
            </div>
            <p className="mt-1 text-sm font-medium text-slate-200">
              {selectedTargetIds.length > 0
                ? selectedTargetIds.map(targetDisplayLabel).join(", ")
                : "No targets selected"}
            </p>
            <p className="mt-1 text-xs text-slate-400">
              {form.target_selection === "environment"
                ? `${form.environment.toUpperCase()} environment deploys its four vehicles.`
                : "Only the vehicles listed above will be included in the request."}
            </p>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <OptionSelect
              id="deployment-environment"
              label="Environment"
              value={form.environment}
              options={ENVIRONMENTS}
              onChange={updateField("environment")}
              error={errors.environment}
              disabled={form.target_selection !== "environment"}
              disabledReason="Switch to Environment target selection to choose a complete environment."
            />
            <OptionSelect
              id="deployment-implementation"
              label="Implementation"
              value={form.implementation}
              options={implementationOptions}
              onChange={updateField("implementation")}
              error={errors.implementation}
            />
            <OptionSelect
              id="deployment-strategy"
              label="Strategy"
              value={form.strategy}
              options={strategyOptions}
              onChange={updateField("strategy")}
              error={errors.strategy}
            />
            <OptionSelect
              id="deployment-failure-mode"
              label="Failure mode"
              value={form.failure_mode}
              options={failureModeOptions}
              onChange={updateField("failure_mode")}
              error={errors.failure_mode}
            />
            <OptionSelect
              id="deployment-failure-target"
              label="Inject verification failure"
              value={form.failure_target_id}
              options={failureTargetOptions}
              optionLabels={{ none: "No injected failure" }}
              onChange={updateField("failure_target_id")}
              error={errors.failure_target_id}
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
          <p className="rounded-lg border border-amber-400/20 bg-amber-400/5 p-3 text-xs leading-5 text-amber-100/80" data-testid="deployment-capability-note">
            Only verified execution paths are enabled. Python canary verifies the first target
            before continuing, and Python rollback restores targets changed before a later
            verification failure; Ansible exposes only its verified all-at-once path until
            equivalent serial behavior is proven. An injected verification failure is limited to
            the selected simulated target and never reaches Home Assistant or normal HomeOps infrastructure.
          </p>

          <ImplementationSourcePanel
            implementation={form.implementation}
            availableImplementations={implementationOptions}
            onImplementationChange={updateImplementation}
          />

          {form.target_selection === "target_ids" && (
            <fieldset>
              <legend className="block text-xs font-semibold uppercase tracking-wider text-slate-400">
                Individual target IDs
              </legend>
              <p className="mt-1 text-xs text-slate-400">
                Choose the short vehicle labels below; stable machine IDs remain in the JSON details.
              </p>
              <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                {TARGET_ORDER.map((targetId) => (
                  <label
                    key={targetId}
                    className="flex items-center gap-2 rounded-lg border border-border bg-slate-950/40 px-3 py-2 text-xs text-slate-300 transition-colors has-[:checked]:border-blue-400/60 has-[:checked]:bg-blue-400/10"
                    title={targetId}
                  >
                    <input
                      type="checkbox"
                      checked={form.target_ids.includes(targetId)}
                      onChange={() => toggleTarget(targetId)}
                      aria-label={targetDisplayLabel(targetId)}
                      className="accent-blue-400 focus-visible:ring-2 focus-visible:ring-blue-300 focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                    />
                    <span className="font-mono">{targetDisplayLabel(targetId)}</span>
                  </label>
                ))}
              </div>
              <FieldError id="target-ids-error" message={errors.target_ids} />
            </fieldset>
          )}
        </div>

        {activeDeployment && activeDeployment.deployment_id !== form.deployment_id && (
          <div
            className="rounded-lg border border-amber-400/30 bg-amber-400/10 p-3 text-xs text-amber-100"
            data-testid="active-run-preserved"
            role="status"
          >
            <p className="font-semibold">Previous run remains visible</p>
            <p className="mt-1">
              Run <code>{activeDeployment.deployment_id}</code> is still being reconciled while you
              configure this new attempt. Editing has not submitted another deployment.
            </p>
          </div>
        )}

        <aside className="space-y-4">
          <details
            className="rounded-xl border border-border bg-slate-950/50 p-4"
            data-testid="deployment-spec-disclosure"
          >
            <summary className="cursor-pointer list-none rounded text-xs font-semibold uppercase tracking-wider text-slate-400 marker:hidden focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-300 focus-visible:ring-offset-2 focus-visible:ring-offset-surface">
              <span className="inline-flex items-center gap-2">
                <span aria-hidden="true" className="text-slate-400">▸</span>
                DeploymentSpec JSON
              </span>
              <span className="mt-1 block normal-case tracking-normal text-slate-400">
                Expand to inspect the exact validated payload submitted when you deploy.
              </span>
            </summary>
            <pre
              aria-label="DeploymentSpec preview"
              data-testid="deployment-spec-preview"
              className="mt-4 max-h-80 max-w-full overflow-auto rounded-lg border border-border bg-slate-950 p-4 text-xs leading-6 text-emerald-200"
            >
              {JSON.stringify(preview, null, 2)}
            </pre>
          </details>

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
            className="mt-5 w-full rounded-lg border border-blue-400/40 bg-blue-400/10 px-4 py-2.5 text-sm font-semibold text-blue-200 transition-colors hover:bg-blue-400/20 disabled:cursor-not-allowed disabled:border-slate-600 disabled:bg-slate-800 disabled:text-slate-400"
          >
            {deploymentActionLabel(form, submitting, Boolean(submitError) || submission?.dispatch_status === "failed")}
          </button>
          {submitError && (
            <div role="alert" className="mt-3 rounded-lg border border-red-400/30 bg-red-400/10 p-3 text-xs text-red-200">
              <p className="font-semibold" data-testid="submission-error-class">
                {errorClassLabel(submitError.code)}
              </p>
              <p className="mt-1">{submitError.message}</p>
              <p className="mt-1 text-red-200/80" data-testid="retry-guidance">
                {submitError.recovery}
                {submitError.retry_after_seconds ? ` Retry after ${submitError.retry_after_seconds} seconds.` : ""}
              </p>
            </div>
          )}
          {submission && (
            <div
              role={submission.dispatch_status === "failed" ? "alert" : "status"}
              aria-atomic="true"
              className={`mt-3 rounded-lg border p-3 text-xs ${submission.dispatch_status === "failed"
                ? "border-red-400/30 bg-red-400/10 text-red-100"
                : "border-emerald-400/30 bg-emerald-400/10 text-emerald-100"}`}
            >
              <p className="font-semibold">
                {submission.dispatch_status === "failed"
                  ? `Deployment ${submission.deployment_id} needs recovery`
                  : `Deployment ${submission.deployment_id} accepted`}
              </p>
              <p className="mt-1">Workflow: {submission.dispatch_status}</p>
              {submission.error_code && (
                <p className="mt-1" data-testid="submission-error-class">
                  {errorClassLabel(submission.error_code)}
                </p>
              )}
              {submission.error && <p className="mt-1">{submission.error}</p>}
              {submission.error_recovery && (
                <p className="mt-1" data-testid="retry-guidance">{submission.error_recovery}</p>
              )}
              {submission.dispatch_status === "failed" && (
                <p className="mt-1" data-testid="retry-guidance">The manifest is retained; retrying uses the same attempt ID.</p>
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
              <button
                type="button"
                onClick={beginNewAttempt}
                className="mt-3 rounded-lg border border-emerald-300/30 px-3 py-2 font-medium text-emerald-100 hover:bg-emerald-300/10"
                data-testid="new-deployment"
              >
                Start another deployment
              </button>
            </div>
          )}
          {previousAttempt && (
            <details
              className="mt-3 rounded-lg border border-slate-600/70 bg-slate-950/30 p-3 text-xs text-slate-300"
              data-testid="previous-attempt"
            >
              <summary className="cursor-pointer rounded font-semibold text-slate-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-300 focus-visible:ring-offset-2 focus-visible:ring-offset-surface">Previous attempt</summary>
              <div className="mt-3 space-y-2">
                <p>
                  <span className="text-slate-400">Attempt ID: </span>
                  <code>{previousAttempt.deployment_id ?? "Unavailable"}</code>
                </p>
                <p>
                  <span className="text-slate-400">Frozen request: </span>
                  {requestSummaryLabel(previousAttempt.request_summary)}
                </p>
                {(previousAttempt.request_summary?.implementation || previousAttempt.request_summary?.strategy) && (
                  <p data-testid="previous-attempt-execution">
                    <span className="text-slate-400">Execution: </span>
                    {previousAttempt.request_summary.implementation ?? "unknown"}
                    {" / "}
                    {previousAttempt.request_summary.strategy ?? "unknown"}
                  </p>
                )}
                <p data-testid="previous-attempt-targets">
                  <span className="text-slate-400">Target set: </span>
                  {previousAttemptTargetLabel(previousAttempt)}
                </p>
                {(previousAttempt.status || previousAttempt.dispatch_status) && (
                  <p data-testid="previous-attempt-status">
                    <span className="text-slate-400">Status: </span>
                    {previousAttempt.status ?? "unknown"}
                    {previousAttempt.dispatch_status && ` · Dispatch: ${previousAttempt.dispatch_status}`}
                  </p>
                )}
                {previousAttemptWorkflowLabel(previousAttempt) && (
                  <p data-testid="previous-attempt-workflow">
                    <span className="text-slate-400">Workflow: </span>
                    {previousAttemptWorkflowLabel(previousAttempt)}
                  </p>
                )}
                {previousAttemptVerificationLabel(previousAttempt) && (
                  <p data-testid="previous-attempt-verification">
                    <span className="text-slate-400">Target verification: </span>
                    {previousAttemptVerificationLabel(previousAttempt)}
                  </p>
                )}
                {(previousAttempt.error_code
                  || previousAttempt.error
                  || previousAttempt.workflow_error
                  || previousAttempt.dispatch_error) && (
                  <p data-testid="previous-attempt-error">
                    <span className="text-slate-400">Result: </span>
                    {previousAttempt.error_code ? errorClassLabel(previousAttempt.error_code) : "Recorded error"}
                    {(previousAttempt.error
                      ?? previousAttempt.workflow_error
                      ?? previousAttempt.dispatch_error)
                      ? ` — ${previousAttempt.error ?? previousAttempt.workflow_error ?? previousAttempt.dispatch_error}`
                      : ""}
                  </p>
                )}
                {previousAttempt.error_recovery && (
                  <p className="text-slate-400">{previousAttempt.error_recovery}</p>
                )}
                {previousAttempt.rollback_status && previousAttempt.rollback_status !== "not_started" && (
                  <p>
                    <span className="text-slate-400">Rollback: </span>
                    {previousAttempt.rollback_status}
                  </p>
                )}
                {previousAttempt.created_at && (
                  <p>
                    <span className="text-slate-400">Created: </span>
                    {formatAttemptTimestamp(previousAttempt.created_at)}
                  </p>
                )}
                {previousAttempt.updated_at && (
                  <p>
                    <span className="text-slate-400">Updated: </span>
                    {formatAttemptTimestamp(previousAttempt.updated_at)}
                  </p>
                )}
                {previousAttempt.manifest_commit_url && (
                  <a
                    href={previousAttempt.manifest_commit_url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-block text-blue-300 underline"
                  >
                    View previous manifest commit
                  </a>
                )}
                {previousAttempt.workflow_url && (
                  <a
                    href={previousAttempt.workflow_url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-block text-blue-300 underline"
                  >
                    View previous workflow run
                  </a>
                )}
              </div>
            </details>
          )}
        </aside>
      </form>
    </section>
  );
}
