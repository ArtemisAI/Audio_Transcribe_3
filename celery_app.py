# Add this line AT THE VERY TOP, before any other imports
import eventlet
eventlet.monkey_patch() # Ensure monkey patching is done early

# --- Existing Imports ---
import os
from celery import Celery
import redis
import logging

# Configure logging for Celery app setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# Get Redis URL from environment variable, default for local Docker setup
redis_url = os.environ.get('REDIS_URL', 'redis://redis:6379/0') # Use 'redis' hostname from compose
log.info(f"CELERY_APP: Configuring Celery with Redis broker/backend: {redis_url}")

# Initialize Celery
# Ensure the first argument matches the filename if discovery relies on it.
# Using 'celery_app' explicitly is safer.
celery = Celery(
    'celery_app', # Name of the module where this instance is defined
    broker=redis_url,
    backend=redis_url, # Using Redis as backend for task state (optional but useful)
    include=['worker'] # Module where tasks are defined ('worker.py')
)

# Optional Celery configuration
celery.conf.update(
    task_serializer='json',
    accept_content=['json'],  # Ensure Celery uses JSON
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    # Increase visibility timeout if tasks are long; value in seconds
    broker_transport_options = {'visibility_timeout': 3600 * 6}, # 6 hours
    # Helps prevent memory leaks in long-running workers
    worker_max_tasks_per_child=100,
    # Make tasks eager for local debugging without Celery/Redis? (Set to True)
    # task_always_eager=False,
)
log.info("CELERY_APP: Celery instance configured.")
log.info(f"CELERY_APP: Included tasks module: {celery.conf.include}")

# Initialize Redis client for Pub/Sub within the worker later
# We create it here mainly to test connectivity on app setup if desired
redis_client_check = None
try:
    # Use decode_responses=True if you want strings from Redis, otherwise bytes
    redis_client_check = redis.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=5)
    redis_client_check.ping() # Test connection
    log.info(f"CELERY_APP: Initial Redis connection check successful at {redis_url}.")
except Exception as e:
    log.error(f"CELERY_APP: Failed initial Redis connection check at {redis_url}: {e}", exc_info=True)
    # The application might still start, but tasks won't work

# Note: worker.py and webui_app.py will create their own Redis clients as needed.

if __name__ == '__main__':
    # This allows running the worker directly, e.g., for debugging:
    # python celery_app.py worker --loglevel=info -P eventlet
    # But standard way is: celery -A celery_app:celery worker ...
    log.info("CELERY_APP: Running celery.start() (typically used for direct execution)")
    celery.start()