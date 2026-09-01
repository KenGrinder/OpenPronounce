"""FastAPI server exposing the OpenPronounce web UI and versioned JSON API.

Run with: uvicorn server:app --host 0.0.0.0 --port 8000
"""

import asyncio
import logging
import os
import re
import secrets
import tempfile
import threading
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import torch
from fastapi import FastAPI, File, Form, HTTPException, Request, Security, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from openpronounce import __version__, audio, phones, speech
from openpronounce.device import get_device
from openpronounce.languages import DEFAULT_LANGUAGE, LANGUAGES, get_language

logger = logging.getLogger("openpronounce.server")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read a bounded integer environment setting, falling back to ``default``."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", name, raw)
        return default
    if value < minimum or value > maximum:
        logger.warning("Ignoring out-of-range %s=%r", name, raw)
        return default
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


MAX_UPLOAD_MB = _env_int("OPENPRONOUNCE_MAX_UPLOAD_MB", 25, 1, 1024)
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_TEXT_LENGTH = _env_int("OPENPRONOUNCE_MAX_TEXT_LENGTH", 2000, 1, 100_000)
MAX_CONCURRENCY = _env_int("OPENPRONOUNCE_MAX_CONCURRENCY", 1, 1, 32)
PRELOAD_MODELS = _env_bool("OPENPRONOUNCE_PRELOAD_MODELS")

# A single 1080 Ti can hold the English word and phone models, but concurrent jobs can
# create large temporary CUDA allocations. Keep the default at one; operators with more
# VRAM can opt in to a larger value.
_INFERENCE_GATE = threading.BoundedSemaphore(MAX_CONCURRENCY)
_MODEL_STATE = {"status": "lazy", "detail": None}


def _call_inference(func, args, kwargs):
    with _INFERENCE_GATE:
        return func(*args, **kwargs)


async def _run_inference(func, *args, **kwargs):
    return await run_in_threadpool(_call_inference, func, args, kwargs)


def _load_default_models():
    speech._load_models()  # noqa: SLF001 - intentional server warm-up
    if phones.is_enabled():
        phones._load_model()  # noqa: SLF001 - intentional server warm-up


async def _preload_models():
    _MODEL_STATE.update(status="warming", detail=None)
    try:
        await _run_inference(_load_default_models)
    except Exception as exc:  # noqa: BLE001 - keep the API alive so a later request can retry
        logger.exception("model preload failed")
        _MODEL_STATE.update(status="error", detail=type(exc).__name__)
    else:
        _MODEL_STATE.update(status="ready", detail=None)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = None
    if PRELOAD_MODELS:
        task = asyncio.create_task(_preload_models(), name="openpronounce-model-preload")
    try:
        yield
    finally:
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(
    title="OpenPronounce API",
    description=(
        "Self-hosted phoneme-level pronunciation assessment. Use /api/v1 endpoints for integrations; "
        "legacy unversioned endpoints remain available for existing clients."
    ),
    version=__version__,
    lifespan=lifespan,
    openapi_tags=[
        {"name": "system", "description": "Discovery, health, readiness, and runtime information."},
        {"name": "pronunciation", "description": "Pronunciation analysis and speech recognition."},
        {"name": "language", "description": "Phonemization, reference speech, and language metadata."},
    ],
)

cors_origins = [item.strip() for item in os.environ.get("OPENPRONOUNCE_CORS_ORIGINS", "").split(",") if item.strip()]
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Accept", "Authorization", "Content-Type", "X-API-Key"],
    )

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
bearer_header = HTTPBearer(auto_error=False)


async def require_api_key(
    header_value: str | None = Security(api_key_header),
    bearer: HTTPAuthorizationCredentials | None = Security(bearer_header),
):
    """Require a configured API key, accepting either X-API-Key or Bearer auth."""
    expected = os.environ.get("OPENPRONOUNCE_API_KEY", "")
    if not expected:
        return
    supplied = header_value or (bearer.credentials if bearer is not None else "")
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )


PROTECTED = [Security(require_api_key)]
_SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,10}$")


@asynccontextmanager
async def _uploaded_wav(upload: UploadFile):
    """Persist a bounded upload, convert it to 16 kHz wav, and remove all temp files."""
    suffix = Path(Path(upload.filename or "").name).suffix
    if not _SAFE_SUFFIX.fullmatch(suffix):
        suffix = ".bin"

    try:
        with tempfile.TemporaryDirectory(prefix="openpronounce-upload-") as directory:
            source = os.path.join(directory, f"source{suffix}")
            size = 0
            with open(source, "wb") as buffer:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"Audio upload exceeds the {MAX_UPLOAD_MB} MB limit",
                        )
                    buffer.write(chunk)
            if size == 0:
                raise HTTPException(status_code=422, detail="Audio upload is empty")
            wav_path = await run_in_threadpool(audio.webm2wav, source)
            yield wav_path
    finally:
        await upload.close()


def _validate_lang(lang: str) -> str:
    try:
        return get_language(lang).code
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class TextRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH, description="Text to phonemize or synthesize.")
    lang: str = Field(default=DEFAULT_LANGUAGE, description="Two-letter language code from GET /api/v1/languages.")


@app.post(
    "/api/v1/pronunciation",
    tags=["pronunciation"],
    operation_id="analyze_pronunciation",
    summary="Score an audio recording against expected text",
    dependencies=PROTECTED,
)
@app.post("/pronunciation", include_in_schema=False, dependencies=PROTECTED)
async def api_analyze_pronunciation(
    file: UploadFile = File(..., description="Audio in any format supported by ffmpeg."),
    expected_text: str = Form(..., min_length=1, max_length=MAX_TEXT_LENGTH),
    lang: str = Form(DEFAULT_LANGUAGE),
):
    lang = _validate_lang(lang)
    try:
        async with _uploaded_wav(file) as wav_file:
            sound = await run_in_threadpool(audio.load, wav_file)
            return await _run_inference(speech.compare_audio_with_text, sound, expected_text, lang=lang)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - keep internal/model details out of API responses
        logger.exception("pronunciation analysis failed")
        raise HTTPException(status_code=500, detail="Pronunciation analysis failed") from exc


@app.post(
    "/api/v1/speech-to-text",
    tags=["pronunciation"],
    operation_id="speech_to_text",
    summary="Transcribe an audio recording",
    dependencies=PROTECTED,
)
@app.post("/speech2text", include_in_schema=False, dependencies=PROTECTED)
async def api_speech2text(
    request: Request,
    file: UploadFile = File(..., description="Audio in any format supported by ffmpeg."),
    lang: str = Form(DEFAULT_LANGUAGE),
):
    lang = _validate_lang(lang)
    try:
        async with _uploaded_wav(file) as wav_file:
            sound = await run_in_threadpool(audio.load, wav_file)
            transcript = await _run_inference(speech.transcribe, sound, lang)
            if request.url.path.startswith("/api/v1/"):
                return {"transcript": transcript, "language": lang}
            return {"transcript": transcript}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("transcription failed")
        raise HTTPException(status_code=500, detail="Transcription failed") from exc


@app.post(
    "/api/v1/phonemes",
    tags=["language"],
    operation_id="phonemize_text",
    summary="Return IPA phonemes for text",
    dependencies=PROTECTED,
)
async def api_v1_phonemes(payload: TextRequest):
    return await _phonemes(payload.text, payload.lang)


@app.post("/phonemes", include_in_schema=False, dependencies=PROTECTED)
async def api_legacy_phonemes(
    text: str = Form(..., min_length=1, max_length=MAX_TEXT_LENGTH),
    lang: str = Form(DEFAULT_LANGUAGE),
):
    return await _phonemes(text, lang)


async def _phonemes(text: str, lang: str):
    lang = _validate_lang(lang)
    try:
        phonemes, words = await run_in_threadpool(speech.get_phonemes_with_word_mapping, text, lang)
        return {"phonemes": phonemes, "words": list(words.values()), "language": lang}
    except Exception as exc:  # noqa: BLE001
        logger.exception("phonemization failed")
        raise HTTPException(status_code=500, detail="Phonemization failed") from exc


@app.post(
    "/api/v1/tts",
    tags=["language"],
    operation_id="synthesize_reference_speech",
    summary="Synthesize a reference pronunciation",
    response_class=FileResponse,
    dependencies=PROTECTED,
)
async def api_v1_tts(payload: TextRequest):
    return await _tts(payload.text, payload.lang)


@app.post("/tts", include_in_schema=False, response_class=FileResponse, dependencies=PROTECTED)
async def api_legacy_tts(
    text: str = Form(..., min_length=1, max_length=MAX_TEXT_LENGTH),
    lang: str = Form(DEFAULT_LANGUAGE),
):
    return await _tts(text, lang)


async def _tts(text: str, lang: str):
    lang = _validate_lang(lang)
    try:
        path = await _run_inference(audio.text2speech, text, lang=lang)
        return FileResponse(path, media_type="audio/wav", filename="reference.wav")
    except Exception as exc:  # noqa: BLE001
        logger.exception("tts failed")
        raise HTTPException(status_code=500, detail="Reference speech synthesis failed") from exc


@app.get(
    "/api/v1/languages",
    tags=["language"],
    operation_id="list_languages",
    summary="List supported languages",
)
@app.get("/languages", include_in_schema=False)
async def api_languages():
    languages = [{"code": language.code, "name": language.name} for language in LANGUAGES.values()]
    return {"default": DEFAULT_LANGUAGE, "languages": languages}


@app.get("/api/v1/health", tags=["system"], operation_id="health_check", summary="Liveness check")
@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}


@app.get("/api/v1/ready", tags=["system"], operation_id="readiness_check", summary="Model readiness check")
@app.get("/ready", include_in_schema=False)
async def ready():
    state = dict(_MODEL_STATE)
    if state["status"] in {"warming", "error"}:
        return JSONResponse(status_code=503, content=state)
    return state


@app.get("/api/v1/info", tags=["system"], operation_id="runtime_info", summary="Runtime and API configuration")
async def info():
    selected_device = "unavailable"
    gpu_name = None
    device_error = None
    try:
        selected = get_device()
        selected_device = str(selected)
        if selected.type == "cuda" and torch.cuda.is_available():
            gpu_name = torch.cuda.get_device_name(selected)
    except Exception as exc:  # noqa: BLE001 - report a bad device setting without failing liveness
        device_error = type(exc).__name__

    return {
        "name": "OpenPronounce",
        "version": __version__,
        "api_version": "v1",
        "device": selected_device,
        "gpu": gpu_name,
        "device_error": device_error,
        "models": dict(_MODEL_STATE),
        "authentication_required": bool(os.environ.get("OPENPRONOUNCE_API_KEY")),
        "tts": os.environ.get("OPENPRONOUNCE_TTS", "gtts"),
        "limits": {
            "max_upload_mb": MAX_UPLOAD_MB,
            "max_text_length": MAX_TEXT_LENGTH,
            "max_concurrency": MAX_CONCURRENCY,
        },
    }


@app.get("/api/v1", tags=["system"], operation_id="api_discovery", summary="API discovery document")
async def api_root():
    return {
        "name": "OpenPronounce",
        "version": __version__,
        "documentation": "/docs",
        "openapi": "/openapi.json",
        "health": "/api/v1/health",
        "ready": "/api/v1/ready",
        "info": "/api/v1/info",
    }


@app.get("/", include_in_schema=False)
async def home(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={})
