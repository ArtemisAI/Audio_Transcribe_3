# Add these lines AT THE VERY TOP, before any other imports
import eventlet
eventlet.monkey_patch()

# --- Existing Imports ---
import os
import logging
import uuid
import json # For parsing Redis messages
# import threading - No longer using threading directly, use eventlet.spawn
import time
from flask import Flask, request, send_from_directory, render_template, redirect, url_for, flash, jsonify, current_app
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename
import redis

# Import the Celery app instance to send tasks
from celery_app import celery # Import the configured Celery instance

# --- Configuration ---
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "/data") # Use env var from compose
OUTPUT_FOLDER = os.environ.get("OUTPUT_FOLDER", "/output") # Use env var from compose
ALLOWED_EXTENSIONS = {'mp3', 'wav', 'flac', 'ogg', 'm4a', 'aac'}
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "default_secret_key_please_change") # Read from env
PUB_SUB_CHANNEL = 'transcription_progress' # Must match worker

# Ensure directories exist (Flask app creates them if needed)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# --- Flask App Initialization ---
app = Flask(__name__, template_folder="templates", static_folder="static")
app.config['SECRET_KEY'] = FLASK_SECRET_KEY
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['OUTPUT_FOLDER'] = OUTPUT_FOLDER
# Optional: Limit upload size
# app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 500 # 500 MB limit example

# --- SocketIO Initialization ---
# Make sure async_mode matches your deployment server (eventlet here)
socketio = SocketIO(app, async_mode='eventlet', message_queue=REDIS_URL, cors_allowed_origins="*")
log.info(f"SocketIO initialized with async_mode='eventlet' and message_queue='{REDIS_URL}'")


# --- Redis Client for Pub/Sub Subscription ---
# Global variable to hold the green thread instance
redis_listener_greenlet = None

def redis_listener_worker():
    """Listens to Redis Pub/Sub channel and emits messages via SocketIO."""
    log.info("Redis listener green thread started.")
    redis_client_pubsub = None
    pubsub = None # Initialize pubsub here
    while True: # Loop indefinitely until application stops
        try:
            if redis_client_pubsub is None or not redis_client_pubsub.ping(): # Check connection using ping
                log.info(f"Connecting/Reconnecting PubSub client to Redis: {REDIS_URL}")
                if pubsub:
                    try: pubsub.close()
                    except: pass
                if redis_client_pubsub:
                    try: redis_client_pubsub.close()
                    except: pass
                # Add connection error handling during client creation
                redis_client_pubsub = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=5)
                redis_client_pubsub.ping() # Verify connection
                log.info(f"PubSub client connected to Redis.")
                pubsub = redis_client_pubsub.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(PUB_SUB_CHANNEL)
                log.info(f"Subscribed to Redis channel: {PUB_SUB_CHANNEL}")

            # listen() is blocking, but check for messages periodically with timeout
            message = pubsub.get_message(timeout=1.0) # Check every 1 second
            if message and message['type'] == 'message':
                log.debug(f"Received message from Redis: {message['data']}")
                try:
                    # Parse the JSON data sent by the worker
                    data = json.loads(message['data'])
                    job_id = data.get('job_id')
                    progress = data.get('progress')
                    status_message = data.get('message')
                    is_error = data.get('error', False)
                    output_file = data.get('output_file') # Filename of the result

                    if job_id:
                        event_name = 'transcription_error' if is_error else \
                                     'transcription_complete' if output_file and progress == 100 else \
                                     'progress_update'

                        payload = {
                            'job_id': job_id,
                            'progress': progress,
                            'message': status_message,
                            'output_file': output_file,
                            # Generate download URL correctly within app context
                            'download_url': url_for('download_file', filename=output_file, _external=False) if output_file else None
                        }
                        # Emit specifically to the namespace/room if needed, or globally
                        # IMPORTANT: Use socketio.sleep(0) to yield control in eventlet
                        socketio.emit(event_name, payload)
                        # log.info(f"Emitted '{event_name}' for job {job_id}") # Can be noisy
                        socketio.sleep(0) # Yield control back to eventlet
                    else:
                        log.warning(f"Received Redis message without job_id: {data}")

                except json.JSONDecodeError:
                     log.error(f"Could not decode JSON from Redis message: {message['data']}")
                except Exception as e:
                    log.error(f"Error processing Redis message or emitting via SocketIO: {e}", exc_info=True)

            # Minimal sleep if no message to prevent busy-waiting entirely
            # Use eventlet sleep when running with eventlet
            eventlet.sleep(0.01) # Yield control

        except redis.exceptions.TimeoutError:
             # This is expected when no message arrives within the timeout
             #log.debug("Redis PubSub get_message timed out (normal).")
             continue # Continue loop after timeout
        except redis.exceptions.ConnectionError as e:
            log.error(f"Redis connection error in listener: {e}. Reconnecting in 5s...")
            if pubsub:
                try: pubsub.unsubscribe() # Unsubscribe before closing
                except: pass
                try: pubsub.close()
                except: pass
            if redis_client_pubsub:
                 try: redis_client_pubsub.close()
                 except: pass
            pubsub = None
            redis_client_pubsub = None # Force reconnect
            eventlet.sleep(5)
        except Exception as e:
            log.error(f"Unexpected error in Redis listener thread: {e}", exc_info=True)
            eventlet.sleep(5) # Prevent rapid failing loop

# Use SocketIO's 'connect' event for the first client to trigger listener start
# This avoids issues with Flask's before_first_request in some deployment scenarios
# This needs to be run within the app context, so using a simple flag is better
listener_started = False

@socketio.on('connect')
def handle_connect_start_listener():
    global listener_started
    # Start listener only once using eventlet.spawn
    if not listener_started:
        log.info("First client connected, spawning Redis listener green thread.")
        # Use start_background_task which integrates better with SocketIO's management
        socketio.start_background_task(target=redis_listener_worker)
        listener_started = True # Mark as started
    log.info(f"Client connected: SID={request.sid}")
    emit('status', {'message': 'Connected and ready to transcribe.'})


# --- Flask Routes ---
@app.route("/", methods=["GET"])
def upload_form_route():
    """Serves the main upload page."""
    return render_template("upload.html")

@app.route("/upload", methods=["POST"])
def upload_and_queue_files_route():
    """Handles file uploads, saves them, and queues transcription tasks via Celery."""
    if 'audio_files' not in request.files:
        return jsonify({"error": "No 'audio_files' part in the request."}), 400

    files = request.files.getlist('audio_files')
    language = request.form.get("language", "en") # Get language from form data

    if not files or all(f.filename == '' for f in files):
        return jsonify({"error": "No files selected."}), 400

    log.info(f"Received {len(files)} file(s) for upload. Language: {language}")

    queued_jobs = []
    errors = []

    for file in files:
        if file and allowed_file(file.filename):
            original_filename = secure_filename(file.filename)
            job_id = str(uuid.uuid4()) # Unique ID for this task
            # Save with a unique name to prevent collisions
            save_filename = f"{job_id}_{original_filename}"
            upload_path = os.path.join(current_app.config['UPLOAD_FOLDER'], save_filename)

            try:
                file.save(upload_path)
                log.info(f"[{job_id}] File '{original_filename}' saved to: {upload_path}")

                # Queue the task with Celery
                # Ensure 'worker.transcribe_task' matches the registered task name
                celery.send_task('worker.transcribe_task', args=[job_id, upload_path, language])
                log.info(f"[{job_id}] Task queued for Celery. File: {original_filename}, Lang: {language}")
                queued_jobs.append({"job_id": job_id, "filename": original_filename, "status": "Queued"})

            except Exception as e:
                log.error(f"Error saving file or queuing task for {original_filename}: {e}", exc_info=True)
                errors.append(f"Failed to process {original_filename}.")
                # Clean up saved file if queuing failed?
                if os.path.exists(upload_path):
                    try: os.remove(upload_path)
                    except OSError as rm_e:
                         log.warning(f"Could not remove file after queue failure: {rm_e}")
        elif file.filename: # Check if filename exists (might be empty file part)
            errors.append(f"File type not allowed or invalid file: {file.filename}")
            log.warning(f"Disallowed or invalid file uploaded: {file.filename}")

    status_code = 202 if queued_jobs else 400 # Accepted if at least one job queued
    response = {"queued_jobs": queued_jobs, "errors": errors}
    return jsonify(response), status_code

@app.route("/download/<path:filename>", methods=["GET"]) # Use <path:> for potentially nested filenames
def download_file(filename):
    """Serves the completed transcription file for download."""
    # Security: Basic check, ensure filename doesn't contain '..' etc.
    # werkzeug.utils.secure_filename is too restrictive here.
    # Use safe_join, but ensure OUTPUT_FOLDER is absolute and trusted.
    safe_path = os.path.abspath(os.path.join(current_app.config['OUTPUT_FOLDER'], filename))
    if not safe_path.startswith(os.path.abspath(current_app.config['OUTPUT_FOLDER'])):
         log.warning(f"Attempt to access file outside output folder: {filename}")
         return "Forbidden", 403

    log.info(f"Download request for: {filename} (resolved to {safe_path})")
    if not os.path.isfile(safe_path):
         log.error(f"Download request for non-existent file: {safe_path}")
         return "File not found.", 404

    try:
        return send_from_directory(
            directory=current_app.config['OUTPUT_FOLDER'],
            path=filename, # Use the original relative path for send_from_directory
            as_attachment=True
        )
    except Exception as e:
        log.error(f"Error serving file {filename}: {e}", exc_info=True)
        return "Error serving file.", 500

@app.route("/health", methods=["GET"])
def health():
    """Basic health check."""
    # Check Redis connection
    redis_ok = False
    try:
        r = redis.Redis.from_url(REDIS_URL, socket_timeout=1)
        r.ping()
        redis_ok = True
    except Exception:
        pass
    # Celery worker check is harder without specific health check task

    status = "ok" if redis_ok else "degraded"
    return jsonify({"status": status, "redis_connected": redis_ok}), 200 if redis_ok else 503


# --- SocketIO Events (Connect handled above) ---
@socketio.on('disconnect')
def handle_disconnect():
    log.info(f"Client disconnected: SID={request.sid}")
    # Clean up any client-specific state if necessary (e.g., removing from a room)

# --- Main Execution ---
if __name__ == "__main__":
    log.info("Starting Flask-SocketIO development server (via eventlet)...")
    # use_reloader=False is important if you spawn background threads/greenlets in debug mode
    socketio.run(app, host="0.0.0.0", port=5000, debug=True, use_reloader=False)