# webui_app.py

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
REDIS_URL = os.environ.get('REDIS_URL', 'redis://redis:6379/0') # Use 'redis' hostname
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
log.info(f"WEBUI: SocketIO initialized with async_mode='eventlet' and message_queue='{REDIS_URL}'")


# --- Redis Client for Pub/Sub Subscription ---
# Global variable to hold the green thread instance
redis_listener_greenlet = None

def redis_listener_worker():
    """Listens to Redis Pub/Sub channel and emits messages via SocketIO."""
    log.info("WEBUI: Redis listener green thread started.")
    redis_client_pubsub = None
    pubsub = None # Initialize pubsub here
    while True: # Loop indefinitely until application stops
        try:
            if redis_client_pubsub is None or not redis_client_pubsub.ping(): # Check connection using ping
                log.info(f"WEBUI: Connecting/Reconnecting PubSub client to Redis: {REDIS_URL}")
                if pubsub:
                    try: pubsub.close()
                    except Exception as e: log.warning(f"WEBUI: Error closing old pubsub: {e}")
                if redis_client_pubsub:
                    try: redis_client_pubsub.close()
                    except Exception as e: log.warning(f"WEBUI: Error closing old redis client: {e}")

                # Add connection error handling during client creation
                redis_client_pubsub = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=5)
                redis_client_pubsub.ping() # Verify connection
                log.info(f"WEBUI: PubSub client connected to Redis.")
                pubsub = redis_client_pubsub.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(PUB_SUB_CHANNEL)
                log.info(f"WEBUI: Subscribed to Redis channel: {PUB_SUB_CHANNEL}")

            # listen() is blocking, but check for messages periodically with timeout
            message = pubsub.get_message(timeout=1.0) # Check every 1 second
            if message and message['type'] == 'message':
                log.debug(f"WEBUI: Received message from Redis: {message['data']}")
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

                        # Construct download URL within app context if needed for payload
                        # Using url_for inside the listener thread requires app context.
                        # It's often simpler to construct it on the client-side or pass relative path.
                        # Let's pass the relative filename and let JS construct the full URL.
                        download_path = url_for('download_file', filename=output_file, _external=False) if output_file else None

                        payload = {
                            'job_id': job_id,
                            'progress': progress,
                            'message': status_message,
                            'output_file': output_file,
                            # Pass relative path or just filename
                            'download_url': download_path
                        }
                        # Emit specifically to the namespace/room if needed, or globally
                        # IMPORTANT: Use socketio.sleep(0) to yield control in eventlet
                        # Make sure emit is done correctly within the eventlet loop
                        socketio.emit(event_name, payload)
                        # log.info(f"WEBUI: Emitted '{event_name}' for job {job_id}") # Can be noisy
                        socketio.sleep(0) # Yield control back to eventlet
                    else:
                        log.warning(f"WEBUI: Received Redis message without job_id: {data}")

                except json.JSONDecodeError:
                     log.error(f"WEBUI: Could not decode JSON from Redis message: {message['data']}")
                except Exception as e:
                    log.error(f"WEBUI: Error processing Redis message or emitting via SocketIO: {e}", exc_info=True)

            # Minimal sleep if no message to prevent busy-waiting entirely
            # Use eventlet sleep when running with eventlet
            eventlet.sleep(0.01) # Yield control

        except redis.exceptions.TimeoutError:
             # This is expected when no message arrives within the timeout
             # log.debug("WEBUI: Redis PubSub get_message timed out (normal).")
             eventlet.sleep(0.1) # Slightly longer sleep on timeout
             continue # Continue loop after timeout
        except redis.exceptions.ConnectionError as e:
            log.error(f"WEBUI: Redis connection error in listener: {e}. Reconnecting in 5s...")
            if pubsub:
                try: pubsub.unsubscribe() # Unsubscribe before closing
                except Exception as unsub_e: log.warning(f"WEBUI: Error unsubscribing pubsub: {unsub_e}")
                try: pubsub.close()
                except Exception as close_e: log.warning(f"WEBUI: Error closing pubsub: {close_e}")
            if redis_client_pubsub:
                 try: redis_client_pubsub.close()
                 except Exception as close_e: log.warning(f"WEBUI: Error closing redis client: {close_e}")
            pubsub = None
            redis_client_pubsub = None # Force reconnect
            eventlet.sleep(5)
        except Exception as e:
            log.error(f"WEBUI: Unexpected error in Redis listener thread: {e}", exc_info=True)
            eventlet.sleep(5) # Prevent rapid failing loop

# Use SocketIO's 'connect' event for the first client to trigger listener start
listener_started = False

@socketio.on('connect')
def handle_connect_start_listener():
    global listener_started
    # Start listener only once using eventlet.spawn
    with app.app_context(): # Ensure context for url_for if used indirectly later
        if not listener_started:
            log.info("WEBUI: First client connected, spawning Redis listener green thread.")
            # Use start_background_task which integrates better with SocketIO's management
            socketio.start_background_task(target=redis_listener_worker)
            listener_started = True # Mark as started
    log.info(f"WEBUI: Client connected: SID={request.sid}")
    emit('status', {'message': 'Connected and ready to transcribe.'})


# --- Helper Function ---
def allowed_file(filename):
    """Checks if the file extension is allowed."""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# --- Flask Routes ---
@app.route("/", methods=["GET"])
def upload_form_route():
    """Serves the main upload page."""
    log.info("WEBUI: Serving upload form.")
    return render_template("upload.html")

@app.route("/upload", methods=["POST"])
def upload_and_queue_files_route():
    """Handles file uploads, saves them, and queues transcription tasks via Celery."""
    if 'audio_files' not in request.files:
        log.warning("WEBUI: Upload request missing 'audio_files' part.")
        return jsonify({"error": "No 'audio_files' part in the request."}), 400

    files = request.files.getlist('audio_files')
    # --- GET LANGUAGE FROM FORM ---
    language = request.form.get("language", "en") # Default to 'en' if not provided
    # -----------------------------

    if not files or all(f.filename == '' for f in files):
        log.warning("WEBUI: Upload request received, but no files selected.")
        return jsonify({"error": "No files selected."}), 400

    log.info(f"WEBUI: Received {len(files)} file(s) for upload. Language specified: '{language}'")

    queued_jobs = []
    errors = []

    for file in files:
        if file and allowed_file(file.filename):
            original_filename = secure_filename(file.filename)
            # Ensure filename is not excessively long (optional)
            if len(original_filename) > 200:
                original_filename = original_filename[-200:] # Truncate if too long

            job_id = str(uuid.uuid4()) # Unique ID for this task
            # Save with a unique name to prevent collisions
            save_filename = f"{job_id}_{original_filename}"
            upload_path = os.path.join(current_app.config['UPLOAD_FOLDER'], save_filename)

            try:
                log.debug(f"WEBUI [{job_id}]: Attempting to save '{original_filename}' to '{upload_path}'")
                file.save(upload_path)
                log.info(f"WEBUI [{job_id}]: File '{original_filename}' saved successfully.")

                # --- QUEUE TASK WITH CELERY, INCLUDING LANGUAGE ---
                # Ensure 'worker.transcribe_task' matches the registered task name in worker.py
                # Pass arguments in the order defined in the task function: job_id, file_path, language
                celery.send_task('worker.transcribe_task', args=[job_id, upload_path, language])
                log.info(f"WEBUI [{job_id}]: Task queued for Celery. File: {original_filename}, Lang: {language}")
                # ----------------------------------------------------
                queued_jobs.append({"job_id": job_id, "filename": original_filename, "status": "Queued"})

            except Exception as e:
                log.error(f"WEBUI [{job_id}]: Error saving file '{original_filename}' or queuing task: {e}", exc_info=True)
                errors.append(f"Failed to process {original_filename} due to server error.")
                # Clean up saved file if queuing failed
                if os.path.exists(upload_path):
                    try:
                        log.warning(f"WEBUI [{job_id}]: Cleaning up failed upload file: {upload_path}")
                        os.remove(upload_path)
                    except OSError as rm_e:
                         log.warning(f"WEBUI [{job_id}]: Could not remove file after queue failure: {rm_e}")
        elif file.filename: # Check if filename exists (might be empty file part)
            errors.append(f"File type not allowed or invalid file: {secure_filename(file.filename)}")
            log.warning(f"WEBUI: Disallowed or invalid file uploaded: {secure_filename(file.filename)}")

    status_code = 202 if queued_jobs else 400 # Accepted if at least one job queued
    response = {"queued_jobs": queued_jobs, "errors": errors}
    log.info(f"WEBUI: Upload processing finished. Queued: {len(queued_jobs)}, Errors: {len(errors)}")
    return jsonify(response), status_code

@app.route("/download/<path:filename>", methods=["GET"]) # Use <path:> for potentially nested filenames
def download_file(filename):
    """Serves the completed transcription file for download."""
    log.info(f"WEBUI: Download request received for filename: '{filename}'")
    # Security: Basic check, ensure filename doesn't contain '..' etc.
    # Normalize the path and ensure it's within the designated output folder.
    output_dir = os.path.abspath(current_app.config['OUTPUT_FOLDER'])
    requested_path = os.path.join(output_dir, filename)
    safe_path = os.path.abspath(requested_path) # Resolve any '..'

    # Check if the resolved path is still inside the output directory
    if not safe_path.startswith(output_dir + os.sep) and safe_path != output_dir:
         log.warning(f"WEBUI: Directory traversal attempt denied for: '{filename}'. Resolved path: '{safe_path}'")
         return "Forbidden: Access denied.", 403

    log.debug(f"WEBUI: Attempting to serve file from resolved path: '{safe_path}'")
    if not os.path.isfile(safe_path):
         log.error(f"WEBUI: Download request for non-existent file: '{safe_path}'")
         return "File not found.", 404

    try:
        return send_from_directory(
            directory=output_dir, # Use the absolute directory path
            path=filename, # Use the original relative path as requested by the client
            as_attachment=True # Force download dialog
        )
    except Exception as e:
        log.error(f"WEBUI: Error serving file '{filename}' from '{output_dir}': {e}", exc_info=True)
        return "Error serving file.", 500

@app.route("/health", methods=["GET"])
def health():
    """Basic health check."""
    log.debug("WEBUI: Health check requested.")
    # Check Redis connection
    redis_ok = False
    redis_ping_error = None
    try:
        r = redis.Redis.from_url(REDIS_URL, socket_timeout=1, socket_connect_timeout=1)
        r.ping()
        redis_ok = True
        log.debug("WEBUI: Health check - Redis ping successful.")
    except Exception as e:
        redis_ping_error = str(e)
        log.warning(f"WEBUI: Health check - Redis ping failed: {redis_ping_error}")

    # Basic Celery check (very limited - just checks if app instance exists)
    celery_ok = celery is not None

    status = "ok" if redis_ok and celery_ok else "degraded"
    status_code = 200 if status == "ok" else 503
    response_data = {
        "status": status,
        "checks": {
            "redis_connected": redis_ok,
            "celery_app_loaded": celery_ok,
        }
    }
    if not redis_ok:
        response_data["checks"]["redis_error"] = redis_ping_error

    log.info(f"WEBUI: Health check result: {status}")
    return jsonify(response_data), status_code


# --- SocketIO Events (Connect handled above) ---
@socketio.on('disconnect')
def handle_disconnect():
    log.info(f"WEBUI: Client disconnected: SID={request.sid}")
    # Clean up any client-specific state if necessary (e.g., removing from a room)

# --- Main Execution ---
if __name__ == "__main__":
    log.info("-----------------------------------------------------")
    log.info("Starting Flask-SocketIO development server (via eventlet)...")
    log.info(f"Upload Folder: {app.config['UPLOAD_FOLDER']}")
    log.info(f"Output Folder: {app.config['OUTPUT_FOLDER']}")
    log.info(f"Redis URL: {REDIS_URL}")
    log.info(f"Debug Mode: True")
    if FLASK_SECRET_KEY == "default_secret_key_please_change":
         log.warning("SECURITY WARNING: Using default Flask Secret Key!")
    log.info("-----------------------------------------------------")
    # use_reloader=False is important if you spawn background threads/greenlets like the listener
    socketio.run(app, host="0.0.0.0", port=5000, debug=True, use_reloader=False)