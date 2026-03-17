import React, { useEffect, useMemo, useState } from "react";
import JSZip from "jszip";
import {
  AccuracyReport,
  JobStatus,
  JobSummary,
  fetchAccuracy,
  fetchJobResultZip,
  fetchJobStatus,
  submitJob,
} from "./api";

interface TrackedJob extends JobSummary {
  // client-side metadata
  localFile?: File;
}

type View = "upload" | "jobs";

const POLL_INTERVAL_MS = 3000;

function classNames(...parts: (string | false | null | undefined)[]) {
  return parts.filter(Boolean).join(" ");
}

const App: React.FC = () => {
  const [view, setView] = useState<View>("upload");
  const [jobs, setJobs] = useState<Record<string, TrackedJob>>({});
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [loadingJobId, setLoadingJobId] = useState<string | null>(null);
  const [ocrTextByJob, setOcrTextByJob] = useState<Record<string, string>>({});
  const [accuracyByJob, setAccuracyByJob] = useState<
    Record<string, AccuracyReport | null>
  >({});
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [isUploading, setIsUploading] = useState(false);

  const selectedJob = selectedJobId ? jobs[selectedJobId] : null;

  // Poll statuses for all jobs
  useEffect(() => {
    if (Object.keys(jobs).length === 0) return;
    let cancelled = false;

    const poll = async () => {
      for (const jobId of Object.keys(jobs)) {
        try {
          const updated = await fetchJobStatus(jobId);
          if (cancelled) return;
          setJobs((prev) => ({
            ...prev,
            [jobId]: {
              ...prev[jobId],
              ...updated,
            },
          }));
        } catch {
          // ignore transient errors
        }
      }
    };

    const interval = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [jobs]);

  // When a job finishes, fetch its OCR text and accuracy if not already loaded
  useEffect(() => {
    const completed = Object.values(jobs).filter(
      (j) => j.status === "succeeded" && j.has_result,
    );
    for (const job of completed) {
      const jobId = job.job_id;
      if (!ocrTextByJob[jobId]) {
        void loadOcrText(jobId);
      }
      if (!accuracyByJob[jobId]) {
        void loadAccuracy(jobId);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobs]);

  const loadOcrText = async (jobId: string) => {
    try {
      setLoadingJobId((id) => id ?? jobId);
      const blob = await fetchJobResultZip(jobId);
      const zip = await JSZip.loadAsync(blob);
      // Heuristic: find the first .md file inside the zip
      const mdEntry = Object.keys(zip.files).find((name) => name.endsWith(".md"));
      if (!mdEntry) {
        throw new Error("No markdown file found in OCR zip");
      }
      const file = zip.files[mdEntry];
      const text = await file.async("text");
      setOcrTextByJob((prev) => ({ ...prev, [jobId]: text }));
    } catch (e) {
      console.error(e);
    } finally {
      setLoadingJobId(null);
    }
  };

  const loadAccuracy = async (jobId: string) => {
    try {
      const report = await fetchAccuracy(jobId);
      setAccuracyByJob((prev) => ({ ...prev, [jobId]: report }));
    } catch (e) {
      console.error(e);
      setAccuracyByJob((prev) => ({
        ...prev,
        [jobId]: {
          overall_accuracy_score: null,
          summary:
            "Accuracy service unavailable or misconfigured (check TOGETHER_API_KEY).",
          mismatches: [],
        },
      }));
    }
  };

  const onUpload = async (file: File | null) => {
    if (!file) return;
    setUploadError(null);
    setIsUploading(true);
    try {
      const { job_id } = await submitJob(file);
      const now = Date.now() / 1000;
      const newJob: TrackedJob = {
        job_id,
        filename: file.name,
        status: "queued",
        created_at: now,
        started_at: null,
        finished_at: null,
        error: null,
        has_result: false,
        localFile: file,
      };
      setJobs((prev) => ({ ...prev, [job_id]: newJob }));
      setSelectedJobId(job_id);
      setView("jobs");
    } catch (e: any) {
      setUploadError(e?.message ?? "Failed to submit job");
    } finally {
      setIsUploading(false);
    }
  };

  const jobList = useMemo(
    () =>
      Object.values(jobs).sort((a, b) => b.created_at - a.created_at),
    [jobs],
  );

  return (
    <div className="min-h-screen flex flex-col">
      <header className="border-b border-slate-800 bg-slate-950/70 backdrop-blur">
        <div className="mx-auto max-w-6xl px-4 py-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="h-8 w-8 rounded-lg bg-sky-500/20 border border-sky-400/40 flex items-center justify-center text-sky-300 font-semibold">
              C
            </div>
            <div>
              <div className="font-semibold tracking-tight">Chandra OCR Dashboard</div>
              <div className="text-xs text-slate-400">
                Upload, track, and validate OCR jobs
              </div>
            </div>
          </div>
          <nav className="flex gap-2 text-sm">
            <button
              className={classNames(
                "px-3 py-1.5 rounded-full border transition-colors",
                view === "upload"
                  ? "border-sky-400 bg-sky-500/10 text-sky-100"
                  : "border-slate-700 text-slate-300 hover:border-slate-500",
              )}
              onClick={() => setView("upload")}
            >
              New Job
            </button>
            <button
              className={classNames(
                "px-3 py-1.5 rounded-full border transition-colors",
                view === "jobs"
                  ? "border-sky-400 bg-sky-500/10 text-sky-100"
                  : "border-slate-700 text-slate-300 hover:border-slate-500",
              )}
              onClick={() => setView("jobs")}
            >
              Jobs
            </button>
          </nav>
        </div>
      </header>

      <main className="flex-1 mx-auto max-w-6xl w-full px-4 py-6 flex gap-6">
        <section className="w-80 shrink-0 space-y-4">
          <UploadCard
            onUpload={onUpload}
            uploadError={uploadError}
            isUploading={isUploading}
          />
          <JobsCard
            jobs={jobList}
            selectedJobId={selectedJobId}
            onSelectJob={(id) => {
              setSelectedJobId(id);
              setView("jobs");
            }}
          />
        </section>

        <section className="flex-1">
          {view === "upload" && (
            <div className="h-full flex items-center justify-center text-slate-400">
              <p className="text-sm">
                Upload a PDF on the left to start a new OCR job, then switch to the
                Jobs view to see progress and results.
              </p>
            </div>
          )}
          {view === "jobs" && (
            <JobDetail
              job={selectedJob}
              ocrText={selectedJob ? ocrTextByJob[selectedJob.job_id] : undefined}
              accuracy={
                selectedJob ? accuracyByJob[selectedJob.job_id] ?? undefined : undefined
              }
              loadingOcr={loadingJobId === selectedJob?.job_id}
            />
          )}
        </section>
      </main>
    </div>
  );
};

const UploadCard: React.FC<{
  onUpload: (file: File | null) => void;
  uploadError: string | null;
  isUploading: boolean;
}> = ({ onUpload, uploadError, isUploading }) => {
  const [file, setFile] = useState<File | null>(null);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!file) return;
    void onUpload(file);
  };

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-4 shadow-lg shadow-slate-900/60">
      <h2 className="text-sm font-semibold mb-2">Start a new job</h2>
      <p className="text-xs text-slate-400 mb-3">
        Upload a PDF to run layout-aware OCR with Chandra. Jobs are queued and processed
        one-by-one on the server.
      </p>
      <form onSubmit={handleSubmit} className="space-y-3">
        <label className="block">
          <span className="text-xs text-slate-300">PDF document</span>
          <input
            type="file"
            accept="application/pdf"
            className="mt-1 block w-full text-xs text-slate-200 file:mr-2 file:py-1.5 file:px-3 file:rounded-full file:border-0 file:text-xs file:font-medium file:bg-sky-500/20 file:text-sky-100 hover:file:bg-sky-500/30"
            onChange={(e) => {
              const f = e.target.files?.[0] ?? null;
              setFile(f);
            }}
          />
        </label>
        {uploadError && (
          <div className="text-xs text-rose-400 bg-rose-950/40 border border-rose-800/60 rounded-md px-2 py-1">
            {uploadError}
          </div>
        )}
        <button
          type="submit"
          disabled={!file || isUploading}
          className={classNames(
            "w-full inline-flex items-center justify-center rounded-full px-3 py-1.5 text-xs font-medium transition-colors",
            !file || isUploading
              ? "bg-slate-800 text-slate-500 cursor-not-allowed"
              : "bg-sky-500 text-slate-950 hover:bg-sky-400",
          )}
        >
          {isUploading ? "Starting job..." : "Start OCR job"}
        </button>
      </form>
      <p className="mt-3 text-[11px] text-slate-500">
        Note: accuracy metrics use Together AI&apos;s GPT-OSS 120B model. Ensure{" "}
        <span className="font-mono text-sky-300">TOGETHER_API_KEY</span> is set on the
        API.
      </p>
    </div>
  );
};

const statusColors: Record<JobStatus, string> = {
  queued: "bg-slate-800 text-slate-200 border-slate-700",
  running: "bg-amber-500/15 text-amber-200 border-amber-500/60",
  succeeded: "bg-emerald-500/15 text-emerald-200 border-emerald-500/60",
  failed: "bg-rose-500/15 text-rose-200 border-rose-500/60",
};

const JobsCard: React.FC<{
  jobs: TrackedJob[];
  selectedJobId: string | null;
  onSelectJob: (id: string) => void;
}> = ({ jobs, selectedJobId, onSelectJob }) => {
  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-4 shadow-lg shadow-slate-900/60 h-[420px] flex flex-col">
      <div className="flex items-center justify-between mb-2">
        <h2 className="text-sm font-semibold">Jobs</h2>
        <span className="text-[11px] text-slate-500">
          {jobs.length === 0 ? "No jobs yet" : `${jobs.length} job(s)`}
        </span>
      </div>
      <div className="flex-1 overflow-y-auto space-y-2 pr-1">
        {jobs.map((job) => (
          <button
            key={job.job_id}
            onClick={() => onSelectJob(job.job_id)}
            className={classNames(
              "w-full text-left rounded-xl border px-3 py-2 text-xs transition-colors",
              selectedJobId === job.job_id
                ? "border-sky-500/70 bg-sky-500/10"
                : "border-slate-800 bg-slate-900/40 hover:border-slate-600",
            )}
          >
            <div className="flex items-center justify-between gap-2">
              <div className="truncate font-medium text-slate-100">
                {job.filename || "Untitled.pdf"}
              </div>
            </div>
            <div className="mt-1 flex items-center justify-between gap-2">
              <div
                className={classNames(
                  "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px]",
                  statusColors[job.status],
                )}
              >
                <span
                  className={classNames(
                    "h-1.5 w-1.5 rounded-full",
                    job.status === "queued"
                      ? "bg-slate-400"
                      : job.status === "running"
                        ? "bg-amber-400 animate-pulse"
                        : job.status === "succeeded"
                          ? "bg-emerald-400"
                          : "bg-rose-400",
                  )}
                />
                <span className="capitalize">{job.status}</span>
              </div>
              <span className="text-[10px] text-slate-500">
                {job.status === "succeeded" && job.finished_at
                  ? "Done"
                  : job.status === "failed"
                    ? "Failed"
                    : "In queue / running"}
              </span>
            </div>
          </button>
        ))}
      </div>
    </div>
  );
};

const JobDetail: React.FC<{
  job: TrackedJob | null;
  ocrText?: string;
  accuracy?: AccuracyReport;
  loadingOcr: boolean;
}> = ({ job, ocrText, accuracy, loadingOcr }) => {
  if (!job) {
    return (
      <div className="h-full flex items-center justify-center text-slate-500 text-sm border border-dashed border-slate-700 rounded-2xl">
        Select a job on the left to see its progress and results.
      </div>
    );
  }

  const created = new Date(job.created_at * 1000).toLocaleString();

  return (
    <div className="h-full rounded-2xl border border-slate-800 bg-slate-900/60 shadow-lg shadow-slate-900/60 flex flex-col">
      <div className="border-b border-slate-800 px-5 py-3 flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold text-slate-50">
            {job.filename || "Untitled.pdf"}
          </h2>
          <p className="text-[11px] text-slate-500">
            Job ID:{" "}
            <span className="font-mono text-slate-300 text-[10px]">{job.job_id}</span>
          </p>
        </div>
        <div
          className={classNames(
            "inline-flex items-center gap-1 rounded-full border px-3 py-1 text-[11px]",
            statusColors[job.status],
          )}
        >
          <span
            className={classNames(
              "h-1.5 w-1.5 rounded-full",
              job.status === "queued"
                ? "bg-slate-400"
                : job.status === "running"
                  ? "bg-amber-400 animate-pulse"
                  : job.status === "succeeded"
                    ? "bg-emerald-400"
                    : "bg-rose-400",
            )}
          />
          <span className="capitalize">{job.status}</span>
        </div>
      </div>

      <div className="flex-1 grid grid-cols-3 gap-4 px-5 py-4 overflow-hidden">
        {/* Original document */}
        <div className="col-span-1 flex flex-col min-w-0">
          <h3 className="text-xs font-semibold text-slate-200 mb-2">Original document</h3>
          <div className="flex-1 rounded-xl border border-slate-800 bg-slate-950/50 p-2 text-xs text-slate-400 overflow-auto">
            {job.localFile ? (
              <iframe
                title="Original PDF"
                src={URL.createObjectURL(job.localFile)}
                className="w-full h-full rounded-lg border border-slate-800 bg-slate-900"
              />
            ) : (
              <p className="text-[11px]">
                Original file is only available in this browser session. Re-upload to
                preview again.
              </p>
            )}
            <p className="mt-2 text-[10px] text-slate-500">
              Created: {created}
            </p>
          </div>
        </div>

        {/* Extracted text */}
        <div className="col-span-1 flex flex-col min-w-0">
          <h3 className="text-xs font-semibold text-slate-200 mb-2">Extracted text</h3>
          <div className="flex-1 rounded-xl border border-slate-800 bg-slate-950/50 p-2 text-xs text-slate-200 overflow-auto whitespace-pre-wrap">
            {job.status !== "succeeded" ? (
              <p className="text-[11px] text-slate-500">
                Waiting for job to complete to show extracted text.
              </p>
            ) : loadingOcr ? (
              <p className="text-[11px] text-slate-500">Loading OCR text from zip...</p>
            ) : ocrText ? (
              ocrText
            ) : (
              <p className="text-[11px] text-slate-500">
                No OCR text available yet. It may have failed to load.
              </p>
            )}
          </div>
        </div>

        {/* Comparison and accuracy */}
        <div className="col-span-1 flex flex-col min-w-0">
          <h3 className="text-xs font-semibold text-slate-200 mb-2">
            Comparison &amp; accuracy
          </h3>
          <div className="flex-1 rounded-xl border border-slate-800 bg-slate-950/50 p-2 text-xs text-slate-200 overflow-auto space-y-3">
            {accuracy ? (
              <>
                <div className="flex items-baseline justify-between gap-2">
                  <div>
                    <p className="text-[11px] text-slate-400 mb-1">Overall accuracy</p>
                    <p className="text-2xl font-semibold text-emerald-300">
                      {accuracy.overall_accuracy_score != null
                        ? `${accuracy.overall_accuracy_score.toFixed(1)}%`
                        : "—"}
                    </p>
                  </div>
                  <div className="flex-1 text-right text-[11px] text-slate-400">
                    {accuracy.summary}
                  </div>
                </div>
                <div>
                  <p className="text-[11px] font-semibold text-slate-300 mb-1">
                    What did not match and why
                  </p>
                  {accuracy.mismatches.length === 0 ? (
                    <p className="text-[11px] text-slate-500">
                      No mismatches reported by the model. OCR output closely matches the
                      original.
                    </p>
                  ) : (
                    <div className="space-y-2">
                      {accuracy.mismatches.map((m, idx) => (
                        <div
                          key={idx}
                          className="rounded-lg border border-slate-800 bg-slate-900/80 p-2"
                        >
                          <p className="text-[11px] text-sky-300 mb-1">
                            Location: {m.location}
                          </p>
                          <p className="text-[11px] text-slate-400">
                            <span className="font-semibold text-slate-200">
                              Original:
                            </span>{" "}
                            {m.original}
                          </p>
                          <p className="text-[11px] text-slate-400">
                            <span className="font-semibold text-slate-200">
                              OCR output:
                            </span>{" "}
                            {m.ocr_output}
                          </p>
                          <p className="mt-1 text-[11px] text-amber-300">
                            Reason: {m.reason}
                          </p>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </>
            ) : job.status === "succeeded" ? (
              <p className="text-[11px] text-slate-500">
                Waiting for accuracy metrics from Together AI...
              </p>
            ) : (
              <p className="text-[11px] text-slate-500">
                Accuracy metrics will appear here once the job completes and the comparison
                API finishes.
              </p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

export default App;

