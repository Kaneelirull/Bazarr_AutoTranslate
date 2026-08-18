export type DataRow = Record<string, any>;

export type StatusSnapshot = {
  generatedAt?: string;
  service?: DataRow;
  currentCycle?: DataRow;
  completedCycle?: number;
  activeJobs?: DataRow[];
  upNext?: DataRow[];
  recentOutcomes?: DataRow[];
  retryPlans?: DataRow[];
  retryMaxAttempts?: number;
  timing?: DataRow;
  circuits?: DataRow[];
  validationObservations?: DataRow[];
  history?: Record<string, DataRow>;
  maintenance?: DataRow;
};

export type WorkView = "auto" | "active" | "up-next" | "retry" | "recent";
export type RetrySort = "media" | "language" | "status" | "attempts" | "nextAction";
