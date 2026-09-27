import pythonDeployerSource from "../../../deploy_demo/deployer.py?raw";
import ansiblePlaybookSource from "../../../ansible/deploy.yml?raw";
import trustedWorkflowSource from "../../../.github/workflows/fleet-deploy.yml?raw";

const RAW_BUILD_SHA = import.meta.env.VITE_BUILD_SHA;

export const BUILD_SHA = typeof RAW_BUILD_SHA === "string" && /^[0-9a-f]{40}$/i.test(RAW_BUILD_SHA)
  ? RAW_BUILD_SHA.toLowerCase()
  : null;
export const SOURCE_REF = BUILD_SHA ?? "master";
export const SOURCE_REPOSITORY_URL = "https://github.com/dhleach/homeops";

export const IMPLEMENTATION_SOURCES = Object.freeze({
  python: Object.freeze({
    id: "python",
    label: "Python deployer",
    path: "deploy_demo/deployer.py",
    language: "python",
    content: pythonDeployerSource,
  }),
  ansible: Object.freeze({
    id: "ansible",
    label: "Ansible playbook",
    path: "ansible/deploy.yml",
    language: "yaml",
    content: ansiblePlaybookSource,
  }),
});

export const TRUSTED_WORKFLOW_SOURCE = Object.freeze({
  label: "Trusted implementation workflow",
  path: ".github/workflows/fleet-deploy.yml",
  content: trustedWorkflowSource,
});

export function sourceUrl(path) {
  return `${SOURCE_REPOSITORY_URL}/blob/${SOURCE_REF}/${path}`;
}
