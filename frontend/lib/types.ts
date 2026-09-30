export type Observation = {
  key: string;
  value: string | boolean | number;
  src_line?: number | null;
  scope_type?: string;
  scope_id?: string;
  source?: string;
};

export type Review = {
  id: number;
  display: string;
  examples: { raw: string; line: number }[];
  frequency: number;
  status: string;
  vars: { name: string; type: string; sample: string }[];
  vendor_family: string;
  suggestion: {
    key: string;
    score: number;
    value_expr: string;
    model_version: string | null;
    decision: string;
  } | null;
};

export type Finding = {
  rule: string;
  title: string | null;
  key: string | null;
  status: string;
  severity: string | null;
  actual: unknown;
  expected: unknown;
  scope: string;
  fix: string | null;
};

export type AuditSummary = {
  PASS: number;
  FAIL: number;
  UNKNOWN: number;
  ERROR: number;
  total: number;
  score: number | null;
};

export type AuditRun = {
  id: number;
  benchmarks: string[];
  registry_version: number;
  summary: AuditSummary;
  results: Finding[];
};

export type Benchmark = {
  name: string;
  rule_count: number;
};

export type CanonicalKey = {
  key: string;
  type: string;
  category: string;
  description: string;
};

export type Device = {
  id: string;
  name: string;
  vendor: string;
  dialect: string;
  status: string;
  unknown_count: number;
  has_audit: boolean;
  created_at: string | null;
  summary: AuditSummary | null;
  // Loaded via getDevice()
  observations?: Observation[];
  // Loaded via getReviewQueue()
  reviews?: Review[];
  // Loaded via getAudit()
  audit?: AuditRun | null;
};
