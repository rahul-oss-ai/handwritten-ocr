# PDF OCR API (Chandra) — Production GPU Deployment

This service exposes a FastAPI API that:

- accepts a PDF upload
- queues it (FIFO) and processes **one job at a time**
- produces Chandra OCR outputs (markdown/html/metadata/images)
- returns a **zip file** once the job is done

## API

- **Submit**: `POST /process-pdf` (multipart form field: `file`)
  - returns: `{ "job_id": "<id>", "status": "queued" }`
- **Status**: `GET /jobs/{job_id}`
- **Download zip**: `GET /jobs/{job_id}/result`

OpenAPI docs:
- Swagger UI: `/docs`
- OpenAPI files: `openapi.json`, `openapi.yaml` (regenerate with `python generate_openapi.py`)

## Important production notes

- **Queue is in-memory** (`queue.Queue` in `main.py`). For strict FIFO + single-worker semantics:
  - run **1 replica**
  - run **1 uvicorn worker** (`--workers 1`)
- If you need **multiple replicas**, move the queue/state to a shared system (Redis/RabbitMQ + DB/S3) and update the code.

## Containerization (recommended)

Create a container image and run it on a GPU-enabled Kubernetes pod.

### Example `Dockerfile`

Create `Dockerfile` at repo root:

```dockerfile
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY main.py /app/main.py
COPY generate_openapi.py /app/generate_openapi.py

EXPOSE 8000

# Single worker to preserve single-queue semantics
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
```

Build and push:

```bash
docker build -t <your-registry>/pdf-ocr-api:1.0.0 .
docker push <your-registry>/pdf-ocr-api:1.0.0
```

## Kubernetes (GPU pod)

### Prerequisites

- Kubernetes cluster with GPU nodes (NVIDIA)
- NVIDIA device plugin installed (so pods can request `nvidia.com/gpu`)
- Enough storage for model caches (HuggingFace/transformers) and temporary job artifacts

### Recommended runtime settings

- **Replicas**: `1`
- **Resources**:
  - request/limit at least `nvidia.com/gpu: 1`
  - provide adequate RAM (model loading can be heavy)
- **Persistent volume** for model cache to avoid re-downloading on every restart:
  - mount a PVC to `/root/.cache/huggingface` (and optionally `/root/.cache/torch`)

### Example deployment (edit to match your cluster)

Save as `k8s-deployment.yaml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pdf-ocr-api
spec:
  replicas: 1
  selector:
    matchLabels:
      app: pdf-ocr-api
  template:
    metadata:
      labels:
        app: pdf-ocr-api
    spec:
      containers:
        - name: api
          image: <your-registry>/pdf-ocr-api:1.0.0
          ports:
            - containerPort: 8000
          resources:
            limits:
              nvidia.com/gpu: 1
              cpu: "4"
              memory: "16Gi"
            requests:
              nvidia.com/gpu: 1
              cpu: "2"
              memory: "12Gi"
          env:
            # Optional but recommended for faster/consistent caching
            - name: HF_HOME
              value: /cache/huggingface
            - name: TRANSFORMERS_CACHE
              value: /cache/huggingface
          volumeMounts:
            - name: hf-cache
              mountPath: /cache
          readinessProbe:
            httpGet:
              path: /
              port: 8000
            initialDelaySeconds: 10
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /
              port: 8000
            initialDelaySeconds: 30
            periodSeconds: 20
      volumes:
        - name: hf-cache
          persistentVolumeClaim:
            claimName: pdf-ocr-hf-cache-pvc
```

PVC example (`pdf-ocr-hf-cache-pvc`) depends on your storage class. Example:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: pdf-ocr-hf-cache-pvc
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 50Gi
```

Apply:

```bash
kubectl apply -f k8s-deployment.yaml
```

### Service / Ingress

Service example:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: pdf-ocr-api
spec:
  selector:
    app: pdf-ocr-api
  ports:
    - name: http
      port: 80
      targetPort: 8000
  type: ClusterIP
```

Expose via your Ingress controller / Gateway (cluster-specific).

## Operational guidance

- **First request is slow**: model weights may download and the model will load.
  - Use a PVC for cache to make restarts faster.
- **Concurrency**: the API accepts multiple submissions quickly, but OCR runs sequentially.
- **Timeouts**: the worker uses a 1-hour timeout for the OCR subprocess (see `timeout=3600`).
- **Result retention**: completed results are kept for 24 hours (`JOB_TTL_SECONDS`) and then deleted.

## Health check

`GET /` returns a small JSON payload and is suitable for readiness/liveness probes.

