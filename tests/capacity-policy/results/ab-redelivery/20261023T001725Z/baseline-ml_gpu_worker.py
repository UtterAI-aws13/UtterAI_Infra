# ML GPU Worker
# utterai-dev-gpu-inference-queue 폴링
# 로드 모델: pyannote speaker-diarization, Whisper ASR
# 담당 단계: 화자 분리 + STT → transcript draft 저장 → 완료 (리포트 큐는 BE finalize가 발행)
import asyncio
import json
import signal
import threading

import boto3
from loguru import logger
from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.observability.otel import initialize_observability
from app.observability.metrics import record_sqs_receive
from app.observability.metrics import record_stage_failure
from app.observability.sqs import extract_context_from_message_attributes
from app.schemas import MLGpuMessage
from app.pipelines.analysis_pipeline import run_ml_gpu_stage, MLGpuModels
from app.models.diarization_pyannote import PyannoteWrapper
from app.models.asr_whisper import WhisperASRWrapper
from app.storage.rds import get_be_engine

_shutdown = threading.Event()


def _extend_visibility(sqs_client, queue_url: str, receipt_handle: str, stop_event: threading.Event, interval: int = 240) -> None:
    # receive 직후 즉시 연장 후 interval마다 반복 — 큐 기본 visibility timeout에 무관하게 동작
    while True:
        try:
            sqs_client.change_message_visibility(
                QueueUrl=queue_url,
                ReceiptHandle=receipt_handle,
                VisibilityTimeout=1800,
            )
        except Exception as e:
            logger.warning(f"[heartbeat] VisibilityTimeout 연장 실패: {e}")
        if stop_event.wait(interval):
            break


def _load_models() -> MLGpuModels:
    diarize = PyannoteWrapper(
        settings.diarization_model_name,
        device=settings.diarization_device,
        hf_token=settings.hf_token,
    )
    diarize.load()
    logger.info("pyannote diarization 로드 완료")

    asr = WhisperASRWrapper(
        settings.asr_model_name,
        device=settings.asr_device,
    )
    asr.load()
    logger.info("Whisper ASR 로드 완료")

    return MLGpuModels(diarize=diarize, asr=asr)


def start_worker() -> None:
    signal.signal(signal.SIGTERM, lambda *_: _shutdown.set())

    initialize_observability()
    sqs = boto3.client("sqs", region_name=settings.aws_region)
    models = _load_models()
    logger.info(f"ML GPU Worker 시작. 큐: {settings.sqs_gpu_inference_queue_url}")

    while not _shutdown.is_set():
        try:
            response = sqs.receive_message(
                QueueUrl=settings.sqs_gpu_inference_queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=20,
                MessageAttributeNames=["All"],
            )
        except Exception as e:
            logger.error(f"[ml-gpu-worker] SQS receive_message 실패: {e}")
            continue

        messages = response.get("Messages", [])
        if not messages:
            continue

        raw = messages[0]
        receipt_handle = raw["ReceiptHandle"]
        body = json.loads(raw["Body"])
        message_context = extract_context_from_message_attributes(raw.get("MessageAttributes"))

        stop_event = threading.Event()
        heartbeat = threading.Thread(
            target=_extend_visibility,
            args=(sqs, settings.sqs_gpu_inference_queue_url, receipt_handle, stop_event),
            daemon=True,
        )
        heartbeat.start()

        try:
            tracer = trace.get_tracer(__name__)
            with tracer.start_as_current_span(
                "worker.ml_gpu.message",
                context=message_context,
                kind=trace.SpanKind.CONSUMER,
            ) as span:
                record_sqs_receive("ml-gpu-worker")
                msg = MLGpuMessage(**body)
                span.set_attribute("session_id", msg.session_id)
                span.set_attribute("job_id", msg.job_id)
                if msg.user_id:
                    span.set_attribute("user.id", msg.user_id)

                async def _run():
                    async with AsyncSession(get_be_engine()) as session:
                        await run_ml_gpu_stage(msg, models, session)

                asyncio.run(_run())
                logger.info(f"ML GPU STAGE 완료: job_id={body.get('job_id')}")
            sqs.delete_message(
                QueueUrl=settings.sqs_gpu_inference_queue_url,
                ReceiptHandle=receipt_handle,
            )
        except Exception as e:
            record_stage_failure("ml-gpu-worker", "message")
            logger.error(f"ML GPU STAGE 실패: {e}")
            try:
                sqs.change_message_visibility(
                    QueueUrl=settings.sqs_gpu_inference_queue_url,
                    ReceiptHandle=receipt_handle,
                    VisibilityTimeout=0,
                )
            except Exception as vis_e:
                logger.warning(f"[ml-gpu-worker] VisibilityTimeout 즉시 해제 실패: {vis_e}")
        finally:
            stop_event.set()


if __name__ == "__main__":
    start_worker()
