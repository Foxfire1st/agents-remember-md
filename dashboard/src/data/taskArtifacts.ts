export type TaskArtifactReaderTarget =
  | {
      kind: 'notes';
      repo: string;
      master: string;
      path: string;
    }
  | {
      kind: 'requirements';
      repo: string;
      master: string;
      document: string;
      path: string;
    };
