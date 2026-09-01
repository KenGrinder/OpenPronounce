# Docker and Unraid deployment

OpenPronounce ships two reusable Linux/amd64 images:

- `ghcr.io/kengrinder/openpronounce:gpu` uses the PyTorch CUDA 11.8 wheel. This is the recommended image for the NVIDIA GTX 1080 Ti (Pascal, compute capability 6.1).
- `ghcr.io/kengrinder/openpronounce:cpu` has the same API without CUDA libraries.

The images do not bake multi-gigabyte speech models into every release. They download models and the selected Piper voice into `/config`, which should always be persistent. The first startup therefore needs internet access and several gigabytes of free appdata space; subsequent starts reuse the cache.

## Docker Compose

Requirements for the GPU stack:

1. A current NVIDIA host driver that supports the GTX 1080 Ti.
2. Docker with the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) registered.
3. Docker Compose with GPU support.

Copy the environment template and set a long random API key if the service will be reachable beyond a trusted LAN:

```bash
cp .env.example .env
docker compose pull
docker compose up -d
```

If the GitHub Container Registry image has not been published yet, build the exact same image locally:

```bash
docker compose build --pull
docker compose up -d
```

Useful checks:

```bash
docker compose ps
docker compose logs -f openpronounce
curl http://localhost:8000/api/v1/health
curl http://localhost:8000/api/v1/ready
curl http://localhost:8000/api/v1/info
```

`/health` reports process liveness. `/ready` returns HTTP 503 while startup preloading is in progress and HTTP 200 when the service is ready or configured for lazy loading. The first model download can take several minutes.

## Unraid with a GTX 1080 Ti

1. Install the **NVIDIA Driver** plugin from Unraid Apps, reboot if requested, and verify that the Unraid terminal can see the card with `nvidia-smi -L`.
2. Confirm that Docker can expose the GPU:

   ```bash
   docker run --rm --runtime=nvidia --gpus all ghcr.io/kengrinder/openpronounce:gpu \
     python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
   ```

3. Download [the included Unraid template](../unraid/OpenPronounce.xml) to the user-template directory on the Unraid boot device:

   ```bash
   curl -fsSL \
     https://raw.githubusercontent.com/KenGrinder/OpenPronounce/main/unraid/OpenPronounce.xml \
     -o /boot/config/plugins/dockerMan/templates-user/OpenPronounce.xml
   ```

4. In the Unraid Docker tab choose **Add Container**, select the **OpenPronounce** user template, and review these values:

   - Repository: `ghcr.io/kengrinder/openpronounce:gpu`
   - Appdata: `/mnt/user/appdata/openpronounce`
   - GPU: `all`, `0`, or the stable `GPU-...` UUID reported by `nvidia-smi -L`
   - API key: optional on a trusted LAN, strongly recommended otherwise
   - Maximum concurrent analyses: `1` for the 11 GB 1080 Ti

5. Apply the template, follow the container log during the initial model download, then open `http://UNRAID-IP:8000/` or `http://UNRAID-IP:8000/docs`.

The template uses the NVIDIA runtime, requests the GPU, drops Linux capabilities, prevents privilege escalation, and gives the container only one persistent writable path. It does not require privileged mode or access to the Docker socket.

The GPU image intentionally uses the CUDA 11.8 PyTorch build. Pascal is supported by CUDA 11.8 and this avoids newer CUDA defaults that no longer include GTX 10-series kernels. The CUDA user-space runtime is inside the image; the Unraid host supplies the driver through the NVIDIA container runtime.

### Building directly on Unraid

If you prefer not to use GHCR, clone the repository on an SSD/cache-backed share and build a local tag:

```bash
git clone https://github.com/KenGrinder/OpenPronounce.git
cd OpenPronounce
docker build --pull -f Dockerfile.gpu -t openpronounce:gpu .
```

Change the Unraid template Repository field to `openpronounce:gpu` and disable automatic image updates for that local-only tag.

## API for apps and tools

The stable integration surface is under `/api/v1`. Interactive Swagger documentation is at `/docs`, and the machine-readable OpenAPI schema is at `/openapi.json`. Audio endpoints accept `multipart/form-data`; text-only endpoints accept JSON.

When `OPENPRONOUNCE_API_KEY` is set, compute endpoints accept either:

```text
X-API-Key: your-secret
Authorization: Bearer your-secret
```

Health, readiness, discovery, runtime info, and language metadata remain public so monitors and clients can discover the service. Do not expose the raw container port to the public internet; put it behind HTTPS and an authenticating reverse proxy. HTTPS is also required for browser microphone recording except on `localhost`.

### Pronunciation analysis

```bash
curl --fail-with-body \
  -H "X-API-Key: $OPENPRONOUNCE_API_KEY" \
  -F "file=@recording.wav" \
  -F "expected_text=Hello, how are you?" \
  -F "lang=en" \
  http://UNRAID-IP:8000/api/v1/pronunciation
```

The response contains the overall score, transcript, per-word and per-phone errors, confidence values, pitch, energy, and alignment traces.

### Speech to text

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $OPENPRONOUNCE_API_KEY" \
  -F "file=@recording.wav" \
  -F "lang=en" \
  http://UNRAID-IP:8000/api/v1/speech-to-text
```

### Phonemes

```bash
curl --fail-with-body \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $OPENPRONOUNCE_API_KEY" \
  -d '{"text":"Hello world","lang":"en"}' \
  http://UNRAID-IP:8000/api/v1/phonemes
```

### Reference speech

```bash
curl --fail-with-body \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $OPENPRONOUNCE_API_KEY" \
  -d '{"text":"Hello world","lang":"en"}' \
  -o reference.wav \
  http://UNRAID-IP:8000/api/v1/tts
```

Legacy unversioned form endpoints remain available for existing clients, but new integrations should use `/api/v1`.

## Configuration

| Variable | Container default | Purpose |
|---|---:|---|
| `OPENPRONOUNCE_API_KEY` | empty | Optional shared API secret. Empty disables API authentication. |
| `OPENPRONOUNCE_CORS_ORIGINS` | empty | Comma-separated browser origins; `*` permits all origins. |
| `OPENPRONOUNCE_DEVICE` | `cuda` GPU / `cpu` CPU | Torch device. |
| `OPENPRONOUNCE_PRELOAD_MODELS` | off in app, `1` in Compose/template | Load English word and phone models in the background at startup. |
| `OPENPRONOUNCE_MAX_CONCURRENCY` | `1` | Simultaneous model jobs. Keep at one on a 1080 Ti. |
| `OPENPRONOUNCE_MAX_UPLOAD_MB` | `25` | Maximum uploaded audio size before decoding. |
| `OPENPRONOUNCE_MAX_TEXT_LENGTH` | `2000` | Maximum request text length. |
| `OPENPRONOUNCE_TTS` | `piper` in images | Reference-voice engine. Piper is offline after download. |
| `OPENPRONOUNCE_TTS_VOICE` | engine/language default | Piper voice ID or other engine-specific voice. |
| `HF_HOME` | `/config/huggingface` | Persistent Hugging Face model and voice cache. |
| `OPENPRONOUNCE_CACHE_DIR` | `/config/tts` | Persistent synthesized-reference cache. |
| `NVIDIA_VISIBLE_DEVICES` | `all` | NVIDIA GPU index, UUID, or `all`. |

## Operations and troubleshooting

- Back up `/mnt/user/appdata/openpronounce`. It contains only downloadable models/voices and generated reference audio; deleting it is recoverable but forces a full download.
- A 503 from `/ready` during first start is normal. A persistent `{"status":"error"}` means model preloading failed; inspect container logs and check internet access and free appdata space.
- If `/api/v1/info` reports `device: unavailable` or `device_error`, verify the Unraid NVIDIA Driver plugin, the template's `--runtime=nvidia --gpus=all` extra parameters, and the GPU selector.
- A CUDA out-of-memory error usually means more than one analysis was allowed at once. Restore `OPENPRONOUNCE_MAX_CONCURRENCY=1` and restart the container.
- Other languages download their word-recognition checkpoint on first use. Change the Piper voice to one matching that language if reference synthesis is enabled.
