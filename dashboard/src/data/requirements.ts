import { getJson, qs } from './files';

export interface RequirementEntry {
  name: string;
  path: string;
  address: string;
  size: number;
  sha256: string;
}

export interface RequirementsListing {
  repo: string;
  master: string;
  document: string;
  registered: boolean;
  requirements: RequirementEntry[];
}

export interface RequirementContent extends RequirementEntry {
  repo: string;
  master: string;
  document: string;
  content: string;
}

export const listRequirements = (
  repo: string,
  master: string,
  document: string,
  base = '',
): Promise<RequirementsListing> =>
  getJson<RequirementsListing>(`${base}/api/requirements/list?${qs({ repo, master, document })}`);

export const readRequirement = (
  repo: string,
  master: string,
  document: string,
  path: string,
  base = '',
): Promise<RequirementContent> =>
  getJson<RequirementContent>(
    `${base}/api/requirements/read?${qs({ repo, master, document, path })}`,
  );

const REQUIREMENTS_PREFIX = 'requirements/';
const PATH_TOKEN = /[\w./%-]+\.md/g;

export function isRequirementAddress(value: string): boolean {
  return value.startsWith(REQUIREMENTS_PREFIX);
}

export function resolveRequirementAddress(
  address: string,
  requirements: readonly RequirementEntry[],
): string | undefined {
  if (!isRequirementAddress(address)) return undefined;
  return requirements.find((entry) => entry.address === address)?.path;
}

export function requirementAddressFromReference(reference: string): string | undefined {
  return (reference.match(PATH_TOKEN) ?? []).find(isRequirementAddress);
}

export function resolveRequirementReference(
  reference: string,
  requirements: readonly RequirementEntry[],
): string | undefined {
  const address = requirementAddressFromReference(reference);
  return address ? resolveRequirementAddress(address, requirements) : undefined;
}
