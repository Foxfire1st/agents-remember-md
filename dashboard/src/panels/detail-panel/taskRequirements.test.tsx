import { fireEvent, render } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { DetailPanel } from './DetailPanel';
import { seedTaskDocuments, stubNotes, taskDoc } from './test-utils';

const REPO = 'agents-remember';
const MASTER = '260831_closeout-certification-reform';
const DOC_PATH = `/tasks/${REPO}/${MASTER}/23_registered-task-local-requirement-root-navigation.json`;
const DOCUMENT = `${MASTER}/23_registered-task-local-requirement-root-navigation.json`;
const PACKET = 'CCR-R23-v1-registered-task-local-requirement-root-navigation.md';

const requirement = {
  name: PACKET,
  path: PACKET,
  address: `requirements/${PACKET}`,
  size: 100,
  sha256: 'abc',
};

function seedRequirementTask() {
  const doc = taskDoc({
    id: '260831-CCR-L23',
    lifecycleId: undefined,
    kind: 'subTask',
    title: 'Register the requirements artifact root',
    repository: REPO,
    docPath: DOC_PATH,
    objective: [
      `[R23 packet](requirements/${PACKET})`,
      `[missing packet](requirements/missing.md)`,
      '[external](https://example.com/spec)',
      '[section](#proof)',
    ].join(' · '),
    references: [
      `requirements/${PACKET} (canonical packet)`,
      `notes/requirements/${PACKET} (explicit Series note collision)`,
    ],
  });
  seedTaskDocuments([doc]);
  stubNotes(
    [{ name: PACKET, path: `requirements/${PACKET}`, size: 10, language: 'markdown' }],
    'note body',
    [requirement],
  );
}

describe('task requirement navigation', () => {
  it('opens task-prose and References requirement addresses through the internal reader', async () => {
    seedRequirementTask();
    const onOpenNotes = vi.fn();
    const view = render(
      <DetailPanel selectedId={`taskdoc:${DOC_PATH}`} onOpenNotes={onOpenNotes} />,
    );

    const proseLink = await view.findByTestId('requirement-link');
    expect(proseLink.tagName.toLowerCase()).toBe('button');
    expect(proseLink.getAttribute('href')).toBeNull();
    fireEvent.click(proseLink);
    expect(onOpenNotes).toHaveBeenLastCalledWith({
      kind: 'requirements',
      repo: REPO,
      master: MASTER,
      document: DOCUMENT,
      path: PACKET,
    });

    const reference = await view.findByTestId('requirement-ref-1');
    expect(reference.getAttribute('href')).toBeNull();
    fireEvent.click(reference);
    expect(onOpenNotes).toHaveBeenLastCalledWith({
      kind: 'requirements',
      repo: REPO,
      master: MASTER,
      document: DOCUMENT,
      path: PACKET,
    });
  });

  it('fails closed for missing requirement links and preserves external links and anchors', async () => {
    seedRequirementTask();
    const view = render(<DetailPanel selectedId={`taskdoc:${DOC_PATH}`} onOpenNotes={vi.fn()} />);
    const refused = await view.findByTestId('requirement-link-refused');
    expect(refused.textContent).toBe('missing packet');
    expect(refused.closest('a')).toBeNull();
    expect(view.getByText('external').closest('a')?.getAttribute('href')).toBe(
      'https://example.com/spec',
    );
    expect(view.getByText('section').closest('a')?.getAttribute('href')).toBe('#proof');
  });

  it('keeps explicit notes/requirements collisions on the Series notes surface', async () => {
    seedRequirementTask();
    const onOpenNotes = vi.fn();
    const view = render(
      <DetailPanel selectedId={`taskdoc:${DOC_PATH}`} onOpenNotes={onOpenNotes} />,
    );
    fireEvent.click(await view.findByTestId('note-ref-2'));
    expect(onOpenNotes).toHaveBeenLastCalledWith({
      kind: 'notes',
      repo: REPO,
      master: MASTER,
      path: `requirements/${PACKET}`,
    });
  });
});
