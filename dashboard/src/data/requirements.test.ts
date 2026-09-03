import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  isRequirementAddress,
  listRequirements,
  readRequirement,
  requirementAddressFromReference,
  resolveRequirementAddress,
  resolveRequirementReference,
  type RequirementEntry,
} from './requirements';

function stubFetch(payload: unknown = {}) {
  const fn = vi.fn(
    async () => ({ ok: true, status: 200, json: async () => payload }) as unknown as Response,
  );
  vi.stubGlobal('fetch', fn);
  return fn;
}

const requirements: RequirementEntry[] = [
  {
    name: 'CCR-R23-v1.md',
    path: 'CCR-R23-v1.md',
    address: 'requirements/CCR-R23-v1.md',
    size: 42,
    sha256: 'abc',
  },
];

afterEach(() => vi.unstubAllGlobals());

describe('data/requirements client', () => {
  it('binds both reads to the canonical repository, master, and task document', async () => {
    const fetch = stubFetch();
    await listRequirements('agents-remember', '260831_master', '260831_master/23_leaf.json');
    await readRequirement(
      'agents-remember',
      '260831_master',
      '260831_master/23_leaf.json',
      'CCR-R23-v1.md',
    );
    const urls = (fetch.mock.calls as unknown as string[][]).map((call) => call[0]);
    expect(urls).toEqual([
      '/api/requirements/list?repo=agents-remember&master=260831_master&document=260831_master%2F23_leaf.json',
      '/api/requirements/read?repo=agents-remember&master=260831_master&document=260831_master%2F23_leaf.json&path=CCR-R23-v1.md',
    ]);
  });
});

describe('requirement address resolution', () => {
  it('resolves only an exact registered requirements/ address', () => {
    expect(resolveRequirementAddress('requirements/CCR-R23-v1.md', requirements)).toBe(
      'CCR-R23-v1.md',
    );
    expect(resolveRequirementAddress('requirements/missing.md', requirements)).toBeUndefined();
    expect(resolveRequirementAddress('notes/CCR-R23-v1.md', requirements)).toBeUndefined();
  });

  it('extracts a requirement address from References prose without guessing', () => {
    const reference = 'requirements/CCR-R23-v1.md (approved packet)';
    expect(requirementAddressFromReference(reference)).toBe('requirements/CCR-R23-v1.md');
    expect(resolveRequirementReference(reference, requirements)).toBe('CCR-R23-v1.md');
    expect(requirementAddressFromReference('CCR-R23-v1.md')).toBeUndefined();
  });

  it('classifies only the reserved root prefix', () => {
    expect(isRequirementAddress('requirements/CCR-R23-v1.md')).toBe(true);
    expect(isRequirementAddress('notes/requirements/CCR-R23-v1.md')).toBe(false);
    expect(isRequirementAddress('https://localhost/requirements/CCR-R23-v1.md')).toBe(false);
  });
});
