export const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export type JobStatus = "queued" | "running" | "succeeded" | "failed";

export interface JobSummary {
  job_id: string;
  filename: string;
  status: JobStatus;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  error: string | null;
  has_result: boolean;
}

export interface AccuracyReport {
  overall_accuracy_score: number | null;
  summary: string;
  mismatches: {
    location: string;
    original: string;
    ocr_output: string;
    reason: string;
  }[];
  raw_response?: string;
}

export async function submitJob(file: File): Promise<{ job_id: string }> {
  const formData = new FormData();
  formData.append("file", file);
  const res = await fetch(`${API_BASE_URL}/process-pdf`, {
    method: "POST",
    body: formData,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || "Failed to submit job");
  }
  return res.json();
}

export async function fetchJobStatus(jobId: string): Promise<JobSummary> {
  const res = await fetch(`${API_BASE_URL}/jobs/${jobId}`);
  if (!res.ok) {
    throw new Error("Failed to fetch job");
  }
  return res.json();
}

export async function fetchJobResultZip(jobId: string): Promise<Blob> {
  const res = await fetch(`${API_BASE_URL}/jobs/${jobId}/result`);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || "Failed to fetch result zip");
  }
  return res.blob();
}

export async function fetchAccuracy(jobId: string): Promise<AccuracyReport> {
  const res = await fetch(`${API_BASE_URL}/jobs/${jobId}/accuracy`);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || "Failed to fetch accuracy report");
  }
  return res.json();
}

