"""
OCR API (Chandra) with a single-worker job queue:
- Submit a PDF -> returns job_id immediately
- Poll job status
- Download result zip when done
"""
from __future__ import annotations

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
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

app = FastAPI(title="PDF OCR API (Chandra)", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

JOB_TTL_SECONDS = 24 * 60 * 60  # keep completed jobs/results for 24h


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


jobs: Dict[str, Job] = {}
jobs_lock = threading.Lock()
job_queue: "Queue[str]" = Queue()
worker_started = False
worker_lock = threading.Lock()


def _cleanup_old_jobs() -> None:
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
            if job and job.result_zip_path:
                try:
                    Path(job.result_zip_path).unlink(missing_ok=True)
                except Exception:
                    pass


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


def _process_job(job_id: str) -> None:
    _cleanup_old_jobs()

    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        job.status = "running"
        job.started_at = time.time()
        job.error = None

    tmp_dir = Path(tempfile.mkdtemp(prefix=f"ocr_{job_id}_"))
    output_dir = tmp_dir / "output"
    uploads_dir = Path(tempfile.gettempdir()) / "ocr-api-uploads"
    pdf_path = uploads_dir / f"{job_id}.pdf"
    result_zip_path = tmp_dir / "result.zip"

    try:
        output_dir.mkdir(parents=True, exist_ok=True)

        # Run OCR
        run_chandra_ocr(pdf_path, output_dir)

        if not output_dir.exists() or not any(output_dir.iterdir()):
            raise RuntimeError("Chandra produced no output.")

        zip_bytes = zip_directory(output_dir)
        result_zip_path.write_bytes(zip_bytes)

        # Move zip to a stable path (outside tmp_dir) so we can safely delete tmp_dir.
        stable_dir = Path(tempfile.gettempdir()) / "ocr-api-results"
        stable_dir.mkdir(parents=True, exist_ok=True)
        stable_zip = stable_dir / f"{job_id}.zip"
        shutil.move(str(result_zip_path), str(stable_zip))

        with jobs_lock:
            job = jobs.get(job_id)
            if job:
                job.status = "succeeded"
                job.finished_at = time.time()
                job.result_zip_path = str(stable_zip)
    except subprocess.TimeoutExpired:
        with jobs_lock:
            job = jobs.get(job_id)
            if job:
                job.status = "failed"
                job.finished_at = time.time()
                job.error = "OCR timed out."
    except Exception as e:
        with jobs_lock:
            job = jobs.get(job_id)
            if job:
                job.status = "failed"
                job.finished_at = time.time()
                job.error = str(e)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        # Best-effort cleanup of uploaded PDF after processing
        try:
            pdf_path.unlink(missing_ok=True)
        except Exception:
            pass


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


def run_chandra_ocr(pdf_path: Path, output_dir: Path) -> None:
    """Run chandra CLI: chandra input.pdf ./output --method hf."""
    cmd = [
        sys.executable,
        "-m",
        "chandra.scripts.cli",
        str(pdf_path),
        str(output_dir),
        "--method",
        "hf",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    if result.returncode != 0:
        stderr = result.stderr or result.stdout or "Unknown error"
        raise RuntimeError(f"Chandra OCR failed: {stderr}")


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

    # Persist the PDF to a stable temp location for the worker.
    uploads_dir = Path(tempfile.gettempdir()) / "ocr-api-uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = uploads_dir / f"{job_id}.pdf"
    pdf_path.write_bytes(contents)

    with jobs_lock:
        jobs[job_id] = Job(
            id=job_id,
            filename=file.filename or "document.pdf",
            status="queued",
        )

    # Patch the worker to use this uploaded PDF path.
    # We store it by overwriting the job's filename and using job_id.pdf convention in _process_job.
    # (Worker reads uploads from ocr-api-uploads/<job_id>.pdf)
    _ensure_worker_started()
    job_queue.put(job_id)

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


@app.get("/")
async def root():
    return {
        "message": "PDF OCR API (Chandra)",
        "docs": "/docs",
        "endpoints": {
            "submit": "POST /process-pdf (multipart form field 'file') -> {job_id}",
            "status": "GET /jobs/{job_id}",
            "result": "GET /jobs/{job_id}/result (zip when succeeded)",
        },
    }
