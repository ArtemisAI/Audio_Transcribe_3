# Add these lines AT THE VERY TOP, before any other imports
import eventlet
eventlet.monkey_patch()

# --- Existing Imports ---
import os
import logging
import time
import torch
from transformers import pipeline, AutoModelForSpeechSeq2Seq, AutoProcessor
import redis
import json # For publishing JSON to Redis

from celery_app import celery # Import Celery app instance
from transcribe_logic import transcribe_audio_file_logic

# --- Configuration ---
# Adjusted format string slightly for clarity
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - [%(job_id)s] - %(message)s')
log = logging.getLogger(__name__)

# Get Redis URL from environment - essential for the worker
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
PUB_SUB_CHANNEL = 'transcription_progress' # Must match webui_app.py

# --- GPU / Device Setup ---
if torch.cuda.is_available():
    device = "cuda:0"
    torch_dtype = torch.float16 # Use float16 for faster inference on compatible GPUs
    log.info(f"WORKER: CUDA available. Using device: {device} with torch_dtype: {torch_dtype}")
else:
    device = "cpu"
    torch_dtype = torch.float32 # CPU uses float32
    log.info(f"WORKER: CUDA not available. Using device: {device} with torch_dtype: {torch_dtype}")

# --- Load Model Globally (ONCE per worker process) ---
ASR_PIPELINE = None
MODEL_ID = "openai/whisper-large-v3" # Or v2, or medium, base etc.

try:
    log.info(f"WORKER: Loading model '{MODEL_ID}' onto device '{device}' with dtype '{torch_dtype}'...")
    start_load_time = time.time()

    # Load model components
    model = AutoModelForSpeechSeq2Seq.from_pretrained(
        MODEL_ID, torch_dtype=torch_dtype, low_cpu_mem_usage=True, use_safetensors=True
    )
    model.to(device)

    processor = AutoProcessor.from_pretrained(MODEL_ID)

    # Create the pipeline manually for more control
    ASR_PIPELINE = pipeline(
        "automatic-speech-recognition",
        model=model,
        tokenizer=processor.tokenizer,
        feature_extractor=processor.feature_extractor,
        max_new_tokens=128,
        chunk_length_s=30, # Define chunk length for pipeline internal chunking
        batch_size=16,     # Adjust based on GPU memory
        torch_dtype=torch_dtype,
        device=device,
    )
    end_load_time = time.time()
    log.info(f"WORKER: ASR pipeline loaded successfully in {end_load_time - start_load_time:.2f} seconds.")
except Exception as e:
    log.error(f"WORKER CRITICAL: Failed to load ASR model '{MODEL_ID}'. Worker cannot process tasks.", exc_info=True)
    # Worker will likely fail tasks if model isn't loaded.
    ASR_PIPELINE = None # Ensure it's None if loading failed

# --- Redis Client for Publishing ---
# Each worker process gets its own client instance
redis_client = None
try:
    redis_client = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=5) # Added timeout
    redis_client.ping()
    log.info("WORKER: Redis client connected successfully.")
except Exception as e:
    log.error(f"WORKER: Failed to connect to Redis at {REDIS_URL}: {e}. Progress updates will fail.", exc_info=True)

# --- Progress Callback ---
def publish_progress_wrapper(job_id, progress_percent, message, error=False, output_file=None):
    """Safely publishes progress updates to Redis using JSON."""
    # Add job_id to log adapter context for consistent logging
    progress_log = JobIdLoggerAdapter(log, {'job_id': job_id})
    if not redis_client:
        # Log locally if Redis isn't available
        progress_log.warning(f"Redis client unavailable. Progress: {progress_percent}%, Msg: {message}, Error: {error}")
        return
    try:
        payload = {
            'job_id': job_id,
            'progress': progress_percent,
            'message': message,
            'error': error,
            'output_file': output_file # Include output file on completion/error
        }
        message_data = json.dumps(payload) # Serialize as JSON string
        redis_client.publish(PUB_SUB_CHANNEL, message_data)
        # Optional: Add logging here for debug if needed, but can be noisy
        # progress_log.debug(f"Published progress: {payload}")
    except Exception as e:
        progress_log.error(f"Failed to publish progress to Redis: {e}", exc_info=True)


# --- Celery Task ---
# Create a logger adapter to include job_id in logs easily
class JobIdLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        # This adapter relies on the extra dict being passed during logging calls
        # The basicConfig format string handles the [%(job_id)s] part
        job_id = self.extra.get('job_id', 'NO_JOB_ID')
        # No need to prepend here if formatter handles it
        return msg, kwargs

@celery.task(bind=True, name='worker.transcribe_task') # Explicit name is good practice
def transcribe_task(self, job_id, file_path, language):
    """
    Celery task to orchestrate the transcription of an audio file.
    Uses the globally loaded ASR_PIPELINE.
    """
    # Create a logger adapter for this specific task execution
    task_log = JobIdLoggerAdapter(log, {'job_id': job_id})

    task_log.info(f"Received task for file: {file_path}, Language: {language}")

    if ASR_PIPELINE is None:
        task_log.error("ASR pipeline not loaded. Aborting task.")
        publish_progress_wrapper(job_id, -1, "Error: ASR Model not available in worker.", error=True)
        # Use self.update_state to mark failure if needed for Celery monitoring
        self.update_state(state='FAILURE', meta={'exc_type': 'PipelineLoadError', 'exc_message': 'ASR pipeline not loaded.'})
        return # Indicate failure

    if not os.path.exists(file_path):
        task_log.error(f"Audio file not found at path: {file_path}. Aborting.")
        publish_progress_wrapper(job_id, -1, f"Error: File not found - {os.path.basename(file_path)}.", error=True)
        self.update_state(state='FAILURE', meta={'exc_type': 'FileNotFoundError', 'exc_message': file_path})
        return

    output_folder = os.environ.get("OUTPUT_FOLDER", "/output")
    # Derive base name from the original filename embedded after job_id_
    original_filename_part = os.path.basename(file_path)
    if original_filename_part.startswith(job_id + "_"):
        original_filename_part = original_filename_part[len(job_id)+1:]

    base_name = os.path.splitext(original_filename_part)[0]
    output_filename = base_name + ".txt"
    output_path = os.path.join(output_folder, output_filename)

    task_log.info(f"Output path set to: {output_path}")

    try:
        # Pass the pipeline and the wrapper callback to the logic function
        transcription = transcribe_audio_file_logic(
            file_path=file_path,
            asr_pipeline=ASR_PIPELINE,
            language=language,
            job_id=job_id,
            progress_callback=publish_progress_wrapper,
            # chunk_duration_ms is handled internally by pipeline's chunk_length_s
        )

        if transcription is not None:
            task_log.info("Transcription logic completed successfully.")
            try:
                 # Ensure output directory exists (though docker-compose should mount it)
                os.makedirs(output_folder, exist_ok=True)
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(transcription)
                task_log.info(f"Transcription saved to: {output_path}")
                # Final success message via Pub/Sub
                publish_progress_wrapper(job_id, 100, "Transcription complete!", output_file=output_filename)
                # Optionally remove the uploaded file now
                try:
                    os.remove(file_path)
                    task_log.info(f"Removed uploaded file: {file_path}")
                except OSError as rm_err:
                    task_log.warning(f"Could not remove uploaded file {file_path}: {rm_err}")

            except Exception as save_e:
                task_log.error(f"Failed to save transcription to {output_path}: {save_e}", exc_info=True)
                publish_progress_wrapper(job_id, -1, f"Error: Could not save output file.", error=True, output_file=output_filename)
                self.update_state(state='FAILURE', meta={'exc_type': type(save_e).__name__, 'exc_message': str(save_e)})
        else:
            # Failure might have been reported by the logic function via callback (e.g., empty audio case)
            task_log.warning("Transcription logic returned None.")
            # Check if an error was already reported via callback. If not, maybe report generic fail?
            # The current logic in transcribe_audio_file_logic reports errors via callback.

    except Exception as task_e:
        task_log.error(f"Unexpected error during transcription task execution: {task_e}", exc_info=True)
        publish_progress_wrapper(job_id, -1, f"Error: Unexpected worker error.", error=True)
        self.update_state(state='FAILURE', meta={'exc_type': type(task_e).__name__, 'exc_message': str(task_e)})
        # Re-raise if you want Celery to definitely record it as a failure state
        # raise task_e

    return output_filename # Celery task return value (optional)