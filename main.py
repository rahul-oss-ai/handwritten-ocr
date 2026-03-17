"""
OCR API (Chandra) with a single-worker job queue:
- Submit a PDF -> returns job_id immediately
- Poll job status
- Download result zip when done
- Accuracy: compare original vs OCR output via Together AI (GPT-OSS 120B)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from queue import Queue
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None  # type: ignore

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore

app = FastAPI(title="PDF OCR API (Chandra)", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

JOB_TTL_SECONDS = 24 * 60 * 60  # keep completed jobs/results for 24h
ENABLE_TTL_CLEANUP = os.environ.get("OCR_ENABLE_TTL_CLEANUP", "").strip() in {
    "1",
    "true",
    "True",
    "yes",
    "YES",
}

# Persist jobs/results locally so server restarts don't lose state.
DATA_DIR = Path(os.environ.get("OCR_DATA_DIR", str(Path.cwd() / "ocr-data"))).resolve()
UPLOADS_DIR = DATA_DIR / "uploads"
RESULTS_DIR = DATA_DIR / "results"
JOBS_JSON_PATH = DATA_DIR / "jobs.json"
for _p in (DATA_DIR, UPLOADS_DIR, RESULTS_DIR):
    _p.mkdir(parents=True, exist_ok=True)

TOGETHER_MODEL = "openai/gpt-oss-120b"
TOGETHER_API_KEY_ENV = "TOGETHER_API_KEY"


@dataclass
class Job:
    id: str
    filename: str
    status: str  # queued | running | succeeded | failed
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    result_zip_path: Optional[str] = None
    input_pdf_path: Optional[str] = None


jobs: Dict[str, Job] = {}
jobs_lock = threading.Lock()
job_queue: "Queue[str]" = Queue()
worker_started = False
worker_lock = threading.Lock()


def _job_to_dict(job: Job) -> Dict[str, Any]:
    return {
        "id": job.id,
        "filename": job.filename,
        "status": job.status,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "error": job.error,
        "result_zip_path": job.result_zip_path,
        "input_pdf_path": job.input_pdf_path,
    }


def _job_from_dict(d: Dict[str, Any]) -> Job:
    return Job(
        id=str(d.get("id") or d.get("job_id") or ""),
        filename=str(d.get("filename") or "document.pdf"),
        status=str(d.get("status") or "queued"),
        created_at=float(d.get("created_at") or time.time()),
        started_at=d.get("started_at"),
        finished_at=d.get("finished_at"),
        error=d.get("error"),
        result_zip_path=d.get("result_zip_path"),
        input_pdf_path=d.get("input_pdf_path"),
    )


def _persist_jobs_locked() -> None:
    tmp = JOBS_JSON_PATH.with_suffix(".json.tmp")
    payload = {"jobs": [_job_to_dict(j) for j in jobs.values()]}
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(JOBS_JSON_PATH)


def _load_jobs_into_memory() -> None:
    if not JOBS_JSON_PATH.exists():
        return
    try:
        data = json.loads(JOBS_JSON_PATH.read_text(encoding="utf-8"))
        loaded = {}
        for item in data.get("jobs", []):
            j = _job_from_dict(item)
            if not j.id:
                continue
            loaded[j.id] = j
        with jobs_lock:
            jobs.clear()
            jobs.update(loaded)
    except Exception as e:
        _log(f"Warning: failed to load persisted jobs: {e}")


_load_jobs_into_memory()


def _cleanup_old_jobs() -> None:
    if not ENABLE_TTL_CLEANUP:
        return
    now = time.time()
    to_delete: list[str] = []
    with jobs_lock:
        for job_id, job in jobs.items():
            if job.finished_at is None:
                continue
            if (now - job.finished_at) > JOB_TTL_SECONDS:
                to_delete.append(job_id)
        for job_id in to_delete:
            job = jobs.pop(job_id, None)
            if job:
                if job.result_zip_path:
                    try:
                        Path(job.result_zip_path).unlink(missing_ok=True)
                    except Exception:
                        pass
                uploads_dir = Path(tempfile.gettempdir()) / "ocr-api-uploads"
                try:
                    (uploads_dir / f"{job_id}.pdf").unlink(missing_ok=True)
                except Exception:
                    pass
        if to_delete:
            _persist_jobs_locked()


def _ensure_worker_started() -> None:
    global worker_started
    with worker_lock:
        if worker_started:
            return
        t = threading.Thread(target=_worker_loop, name="ocr-worker", daemon=True)
        t.start()
        worker_started = True


def _worker_loop() -> None:
    while True:
        job_id = job_queue.get()
        try:
            _process_job(job_id)
        finally:
            job_queue.task_done()


def _log(msg: str) -> None:
    """Print job progress to terminal (stderr so it doesn't break JSON responses)."""
    print(f"[OCR] {msg}", file=sys.stderr, flush=True)


def _process_job(job_id: str) -> None:
    _cleanup_old_jobs()

    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        job.status = "running"
        job.started_at = time.time()
        job.error = None
        filename = job.filename
        _persist_jobs_locked()

    _log(f"Job {job_id[:8]}... ({filename}) started")

    tmp_dir = Path(tempfile.mkdtemp(prefix=f"ocr_{job_id}_"))
    output_dir = tmp_dir / "output"
    pdf_path = Path(jobs[job_id].input_pdf_path or (UPLOADS_DIR / f"{job_id}.pdf"))
    result_zip_path = tmp_dir / "result.zip"

    try:
        output_dir.mkdir(parents=True, exist_ok=True)

        # Run OCR
        _log(f"Job {job_id[:8]}... running Chandra OCR...")
        run_chandra_ocr(pdf_path, output_dir)

        if not output_dir.exists() or not any(output_dir.iterdir()):
            raise RuntimeError("Chandra produced no output.")

        zip_bytes = zip_directory(output_dir)
        result_zip_path.write_bytes(zip_bytes)

        # Move zip to a stable path so it persists across restarts.
        stable_zip = RESULTS_DIR / f"{job_id}.zip"
        shutil.move(str(result_zip_path), str(stable_zip))

        with jobs_lock:
            job = jobs.get(job_id)
            if job:
                job.status = "succeeded"
                job.finished_at = time.time()
                job.result_zip_path = str(stable_zip)
                _persist_jobs_locked()
        _log(f"Job {job_id[:8]}... succeeded")
    except subprocess.TimeoutExpired:
        with jobs_lock:
            job = jobs.get(job_id)
            if job:
                job.status = "failed"
                job.finished_at = time.time()
                job.error = "OCR timed out."
                _persist_jobs_locked()
        _log(f"Job {job_id[:8]}... failed: OCR timed out")
    except Exception as e:
        with jobs_lock:
            job = jobs.get(job_id)
            if job:
                job.status = "failed"
                job.finished_at = time.time()
                job.error = str(e)
                _persist_jobs_locked()
        _log(f"Job {job_id[:8]}... failed: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        # Keep uploaded PDF for accuracy comparison; optional TTL cleanup can remove it.


def _safe_job_dict(job: Job) -> Dict[str, Any]:
    return {
        "job_id": job.id,
        "filename": job.filename,
        "status": job.status,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "error": job.error,
        "has_result": bool(job.result_zip_path) and job.status == "succeeded",
    }


def _stream_subprocess_output(proc: subprocess.Popen, out_lines: List[str]) -> None:
    """Read subprocess stdout line by line; log each line and append to out_lines."""
    if proc.stdout is None:
        return
    for line in proc.stdout:
        line = line.rstrip("\n\r")
        if line:
            _log(line)
        out_lines.append(line)


def run_chandra_ocr(pdf_path: Path, output_dir: Path) -> None:
    """Run chandra CLI: chandra input.pdf ./output --method hf. Streams OCR progress to stderr."""
    cmd = [
        sys.executable,
        "-m",
        "chandra.scripts.cli",
        str(pdf_path),
        str(output_dir),
        "--method",
        "hf",
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines: List[str] = []
    reader = threading.Thread(target=_stream_subprocess_output, args=(proc, lines))
    reader.daemon = True
    reader.start()
    try:
        proc.wait(timeout=3600)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise RuntimeError("OCR timed out.")
    finally:
        if proc.stdout:
            proc.stdout.close()
        reader.join(timeout=5.0)
    if proc.returncode != 0:
        output = "\n".join(lines).strip() or "Unknown error"
        raise RuntimeError(f"Chandra OCR failed: {output}")


def zip_directory(dir_path: Path) -> bytes:
    """Zip all contents of a directory (relative paths preserved) and return zip bytes."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in dir_path.rglob("*"):
            if f.is_file():
                arcname = f.relative_to(dir_path)
                zf.write(f, arcname)
    buf.seek(0)
    return buf.getvalue()


def _extract_text_from_pdf(pdf_path: Path) -> str:
    if PdfReader is None:
        raise RuntimeError("pypdf is required for accuracy comparison. pip install pypdf")
    reader = PdfReader(str(pdf_path))
    parts = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            parts.append(text)
    return "\n\n".join(parts) if parts else ""


def _get_ocr_markdown_from_zip(zip_path: Path, stem: str) -> str:
    with zipfile.ZipFile(zip_path, "r") as zf:
        md_name = f"{stem}/{stem}.md"
        if md_name not in zf.namelist():
            raise RuntimeError(f"OCR output zip has no {md_name}")
        return zf.read(md_name).decode("utf-8", errors="replace")


def _truncate_for_model(text: str, max_chars: int = 120_000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[... truncated for length ...]"


def _get_accuracy_via_together(original_text: str, ocr_markdown: str) -> Dict[str, Any]:
    if OpenAI is None:
        raise RuntimeError("openai package is required for accuracy. pip install openai")
    api_key = os.environ.get(TOGETHER_API_KEY_ENV)
    if not api_key:
        raise RuntimeError(
            f"Environment variable {TOGETHER_API_KEY_ENV} must be set for accuracy comparison."
        )

    original_text = _truncate_for_model(original_text)
    ocr_markdown = _truncate_for_model(ocr_markdown)

    system_prompt = """You are an expert at comparing a source document with an OCR-generated version.
Your task is to produce a JSON report with:
1. "overall_accuracy_score": a number from 0 to 100 (100 = perfect match).
2. "summary": one short paragraph describing how well the OCR output matches the original.
3. "mismatches": an array of objects, each with:
   - "location": where in the document (e.g. page/section or excerpt).
   - "original": the exact or representative text from the original that was affected.
   - "ocr_output": the corresponding text in the OCR output (or "missing" if omitted).
   - "reason": brief explanation of what went wrong (e.g. misread character, missing line, wrong layout).
If there are no mismatches, set "mismatches" to [] and overall_accuracy_score to 100.
Output only valid JSON, no markdown or extra text."""

    user_content = (
        "Compare the following two versions and produce the JSON report.\n\n"
        "--- ORIGINAL DOCUMENT (extracted text) ---\n"
        f"{original_text}\n\n"
        "--- OCR-GENERATED OUTPUT (markdown) ---\n"
        f"{ocr_markdown}\n\n"
        "--- END ---\n\n"
        "Output only the JSON report."
    )

    client = OpenAI(api_key=api_key, base_url="https://api.together.xyz/v1")
    response = client.chat.completions.create(
        model=TOGETHER_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        max_tokens=4096,
        temperature=0.2,
    )
    choice = response.choices[0]
    raw = (choice.message.content or "").strip()
    # Allow optional markdown code block
    if raw.startswith("```"):
        lines = raw.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {
            "overall_accuracy_score": None,
            "summary": "Model response could not be parsed as JSON.",
            "mismatches": [],
            "raw_response": raw,
        }


@app.post(
    "/process-pdf",  # kept for compatibility; now submits a queued job
    responses={
        200: {
            "description": "Job accepted",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "job_id": {"type": "string"},
                            "status": {"type": "string"},
                        },
                        "required": ["job_id", "status"],
                    }
                }
            },
        }
    },
)
async def process_pdf(file: UploadFile = File(..., description="PDF file to process")):
    """
    Submit a PDF file for OCR. Returns a job id immediately.
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="Invalid file: only PDF files are accepted.",
        )

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    job_id = uuid4().hex

    # Persist the PDF to a stable local folder so jobs survive restarts.
    pdf_path = UPLOADS_DIR / f"{job_id}.pdf"
    pdf_path.write_bytes(contents)

    with jobs_lock:
        jobs[job_id] = Job(
            id=job_id,
            filename=file.filename or "document.pdf",
            status="queued",
            input_pdf_path=str(pdf_path),
        )
        _persist_jobs_locked()

    # Patch the worker to use this uploaded PDF path.
    # We store it by overwriting the job's filename and using job_id.pdf convention in _process_job.
    # (Worker reads uploads from ocr-api-uploads/<job_id>.pdf)
    _ensure_worker_started()
    job_queue.put(job_id)
    _log(f"Job {job_id[:8]}... queued ({file.filename or 'document.pdf'})")

    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}")
async def get_job_status(job_id: str):
    """Return job status for a given job id."""
    _cleanup_old_jobs()
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")
        return _safe_job_dict(job)


@app.get(
    "/jobs/{job_id}/result",
    responses={
        200: {
            "description": "OCR output zip file",
            "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def download_job_result(job_id: str):
    """Download the zip result for a completed job."""
    _cleanup_old_jobs()
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")
        if job.status != "succeeded" or not job.result_zip_path:
            raise HTTPException(status_code=409, detail="Result not available yet.")
        zip_path = Path(job.result_zip_path)
        filename = f"{Path(job.filename).stem}_ocr_output.zip"

    if not zip_path.exists():
        raise HTTPException(status_code=410, detail="Result expired or removed.")

    return Response(
        content=zip_path.read_bytes(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/jobs/{job_id}/accuracy")
async def get_job_accuracy(job_id: str) -> Dict[str, Any]:
    """
    Compare the original document and OCR output for this job using the GPT-OSS 120B model
    (Together AI). Returns accuracy metrics and a list of mismatches with reasons.
    Requires TOGETHER_API_KEY to be set.
    """
    _cleanup_old_jobs()
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")
        if job.status != "succeeded" or not job.result_zip_path:
            raise HTTPException(
                status_code=409,
                detail="Accuracy is only available for completed jobs with a result.",
            )
        zip_path = Path(job.result_zip_path)
        stem = Path(job.filename).stem

    pdf_path = UPLOADS_DIR / f"{job_id}.pdf"
    if not pdf_path.exists():
        raise HTTPException(
            status_code=410,
            detail="Original document no longer available (expired or deleted).",
        )
    if not zip_path.exists():
        raise HTTPException(status_code=410, detail="OCR result no longer available.")

    try:
        original_text = _extract_text_from_pdf(pdf_path)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Failed to read original PDF: {e}")

    try:
        ocr_markdown = _get_ocr_markdown_from_zip(zip_path, stem)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Failed to read OCR output: {e}")

    try:
        report = _get_accuracy_via_together(original_text, ocr_markdown)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    return report


@app.get("/")
async def root():
    return {
        "message": "PDF OCR API (Chandra)",
        "docs": "/docs",
        "endpoints": {
            "submit": "POST /process-pdf (multipart form field 'file') -> {job_id}",
            "status": "GET /jobs/{job_id}",
            "result": "GET /jobs/{job_id}/result (zip when succeeded)",
            "accuracy": "GET /jobs/{job_id}/accuracy (Together AI GPT-OSS 120B comparison)",
        },
    }
