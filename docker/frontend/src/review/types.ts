export type ReviewActionName = "recheck" | "queue_retry" | "dismiss" | "approve_name" | "revoke_name" | "reopen";

export type ReviewCue = {
  cueNumber: number; timestamp: string; sourceText: string; targetText: string;
  sourceCueHash: string; targetCueHash: string; reason: string; rules: string[]; canApproveName: boolean; canApproveCue: boolean;
  decision?: "approve" | "retry" | null; rememberPhrase?: boolean;
  context: Array<{ cueNumber: number; sourceText: string; targetText: string }>;
};
export type CueReviewPayload = {
  planId: number; expectedUpdatedAt: number; sourceHash: string; candidateHash: string;
  approvalRevision: number; scope: string; sourceLanguage: string; targetLanguage: string;
  decisionRevision: number; decisionCounts: { approved: number; retry: number; undecided: number };
  fileFindings?: Array<{ code: string; reason: string; action: string }>;
  items: ReviewCue[]; pagination: { page: number; pageSize: number; total: number };
  approvals: Array<{ id: number; sourceText: string; targetText: string }>;
  actionsEnabled: boolean;
  candidateAvailable?: boolean; unavailableReason?: string | null;
};

export type ReviewFilters = {
  page: string;
  pageSize: string;
  q: string;
  status: string;
  itemType: string;
  language: string;
  sort: string;
  direction: string;
};

export type ReviewHistory = {
  action?: string;
  outcome?: string;
  reasonCode?: string;
  createdAt?: number;
  details?: Record<string, unknown>;
};

export type ReviewItem = {
  id: number;
  itemId?: number;
  itemType?: string;
  sourceLanguage?: string;
  targetLanguage?: string;
  status?: string;
  updatedAt: number;
  failureClass?: string;
  failureRules?: string[];
  attemptCount?: number;
  lastReason?: string;
  scanPending?: boolean;
  scanState?: string;
  sourceRelativePath?: string;
  targetRelativePath?: string;
  artifactRelativePath?: string;
  mediaRelativePath?: string;
  sourceAvailable?: boolean;
  targetAvailable?: boolean;
  artifactAvailable?: boolean;
  mediaAvailable?: boolean;
  sourceAvailabilityReason?: string;
  targetAvailabilityReason?: string;
  artifactAvailabilityReason?: string;
  mediaAvailabilityReason?: string;
  allowedActions?: ReviewActionName[];
  media?: { title?: string; episodeCode?: string; episodeTitle?: string; seasonNumber?: number; episodeNumber?: number };
  recovery?: { validRecoveredCueCount?: number; unresolvedCueCount?: number; latestRecoveryStage?: string };
  validationFeedback?: Record<string, unknown> & { completeness?: Record<string, unknown>; validationResult?: string; outcome?: string; reasonCode?: string };
  actions?: ReviewHistory[];
  actionCount?: number;
  actionsTruncated?: boolean;
};

export type ReviewPayload = {
  counts?: { needsAttention?: number; manuallyQueued?: number; resolved?: number; dismissed?: number };
  items?: ReviewItem[];
  pagination?: { page?: number; pageSize?: number; total?: number };
  actionsEnabled?: boolean;
};

export type ActionPayload = { outcome?: string; scanPending?: boolean };

export const DEFAULT_FILTERS: ReviewFilters = {
  page: "1",
  pageSize: "20",
  q: "",
  status: "",
  itemType: "",
  language: "",
  sort: "updatedAt",
  direction: "desc",
};
