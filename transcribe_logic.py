# transcribe_logic.py

import os
import time
import logging
import pydub # For audio loading / manipulation (if needed beyond pipeline)
import torch # Only for type hints if necessary, main processing in pipeline

# Setup logger for this module
log = logging.getLogger(__name__)

def transcribe_audio_file_logic(
    file_path: str,
    asr_pipeline, # The loaded transformers pipeline
    language: str,
    job_id: str,
    progress_callback # Function to call with (job_id, progress, message, error, output_file)
    ) -> str | None:
    """
    Performs audio transcription using the provided pipeline and reports progress.

    Args:
        file_path: Path to the audio file.
        asr_pipeline: Pre-loaded Hugging Face ASR pipeline.
        language: Target language code (e.g., 'en', 'es').
        job_id: Unique ID for this transcription job.
        progress_callback: Function to report progress updates.

    Returns:
        The transcription text as a string, or None if an error occurred.
    """
    # Use adapter for job_id logging
    logic_log = logging.LoggerAdapter(log, {'job_id': job_id})

    logic_log.info(f"Starting transcription logic for file: {file_path}, Language: {language}")

    if not os.path.exists(file_path):
        logic_log.error(f"File does not exist: {file_path}")
        progress_callback(job_id, 0, f"Error: Input file not found.", error=True, output_file=None)
        return None

    # --- Optional: Use pydub for initial checks or format conversion if needed ---
    try:
        logic_log.debug("Attempting to load audio info with pydub...")
        audio = pydub.AudioSegment.from_file(file_path)
        duration_seconds = len(audio) / 1000.0
        logic_log.info(f"Audio duration: {duration_seconds:.2f} seconds.")
        if duration_seconds < 0.5: # Handle very short/empty files
             logic_log.warning("Audio duration is very short (< 0.5s). Skipping transcription.")
             progress_callback(job_id, 100, "Warning: Audio file is too short or empty.", error=False, output_file=None) # Treat as success with warning
             return "" # Return empty string for very short files
        # You could convert format here if needed:
        # if not file_path.lower().endswith(".wav"):
        #     wav_path = file_path + ".wav"
        #     logic_log.info(f"Converting to WAV: {wav_path}")
        #     audio.export(wav_path, format="wav")
        #     file_path = wav_path # Use the converted file path
        #     # Remember to potentially clean up this temporary file later
    except pydub.exceptions.CouldntDecodeError:
        logic_log.error(f"pydub could not decode audio file: {file_path}. Check format/integrity.")
        progress_callback(job_id, 0, "Error: Could not decode audio file.", error=True, output_file=None)
        return None
    except Exception as e:
        logic_log.error(f"Error during pydub processing: {e}", exc_info=True)
        progress_callback(job_id, 0, "Error: Failed during audio pre-processing.", error=True, output_file=None)
        return None
    # --- End Optional pydub section ---

    try:
        # Initial progress update
        progress_callback(job_id, 5, f"Starting transcription process...", error=False, output_file=None)
        logic_log.info("Calling ASR pipeline...")
        start_time = time.time()

        # --- Core Transcription Call ---
        # The pipeline handles loading, chunking, processing internally based on its init params
        # Pass generate_kwargs for language - requires transformers >= 4.26? Check docs.
        # For older versions, language might need to be forced differently or handled by model config.
        # Whisper models often detect language automatically if not specified.
        # Let's assume newer transformers pipeline that accepts generate_kwargs
        # Note: The pipeline might internally report progress if configured, but we do it manually here.
        outputs = asr_pipeline(
            file_path,
            generate_kwargs={"language": language} # Pass language here
        )
        # -----------------------------

        end_time = time.time()
        processing_time = end_time - start_time
        logic_log.info(f"Pipeline finished in {processing_time:.2f} seconds.")

        transcription = outputs["text"] if outputs and "text" in outputs else ""

        if not transcription:
            logic_log.warning("Transcription result is empty.")
            # Report intermediate progress if needed
            progress_callback(job_id, 95, "Warning: Transcription result is empty.", error=False, output_file=None)
        else:
             # Log first N characters for debugging
             logic_log.info(f"Transcription generated (first 100 chars): {transcription[:100]}...")
             progress_callback(job_id, 95, "Transcription generated, preparing save.", error=False, output_file=None)


        # Simulate some final step if needed
        time.sleep(0.1)

        logic_log.info("Transcription logic completed.")
        return transcription # Return the final text

    except Exception as e:
        logic_log.error(f"Error during ASR pipeline execution: {e}", exc_info=True)
        progress_callback(job_id, -1, f"Error during transcription: {e}", error=True, output_file=None) # Indicate error with -1 or specific code
        return None