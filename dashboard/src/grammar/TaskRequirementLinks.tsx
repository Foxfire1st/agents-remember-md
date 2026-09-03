import { createContext, type ReactNode, useContext, useEffect, useMemo, useState } from 'react';

import { listRequirements, type RequirementEntry } from '../data/requirements';
import type { TaskArtifactReaderTarget } from '../data/taskArtifacts';

interface TaskRequirementLinkContextValue {
  requirements: RequirementEntry[];
  open: (path: string) => void;
}

const TaskRequirementLinkContext = createContext<TaskRequirementLinkContextValue | null>(null);

export function TaskRequirementLinksProvider({
  repo,
  master,
  document,
  onOpenArtifact,
  children,
}: {
  repo: string;
  master: string;
  document: string | undefined;
  onOpenArtifact?: (target: TaskArtifactReaderTarget) => void;
  children: ReactNode;
}) {
  const [requirements, setRequirements] = useState<RequirementEntry[]>([]);

  useEffect(() => {
    let live = true;
    setRequirements([]);
    if (document) {
      void listRequirements(repo, master, document).then(
        (listing) => {
          if (live) setRequirements(listing.requirements);
        },
        () => {},
      );
    }
    return () => {
      live = false;
    };
  }, [repo, master, document]);

  const value = useMemo<TaskRequirementLinkContextValue>(
    () => ({
      requirements,
      open: (path) => {
        if (document) {
          onOpenArtifact?.({ kind: 'requirements', repo, master, document, path });
        }
      },
    }),
    [document, master, onOpenArtifact, repo, requirements],
  );

  return (
    <TaskRequirementLinkContext.Provider value={value}>
      {children}
    </TaskRequirementLinkContext.Provider>
  );
}

export function useTaskRequirementLinks(): TaskRequirementLinkContextValue | null {
  return useContext(TaskRequirementLinkContext);
}
