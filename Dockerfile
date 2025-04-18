# Use an official PyTorch runtime with CUDA support matching your RTX 3090
# Check NVIDIA driver version and choose appropriate CUDA version
# Example: CUDA 11.8 is common for recent drivers/PyTorch versions
FROM pytorch/pytorch:2.1.0-cuda11.8-cudnn8-runtime
# Or use the specific version you confirmed works:
# FROM pytorch/pytorch:2.0.1-cuda11.7-cudnn8-runtime

# Set environment variables to prevent interactive prompts during build
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
# Environment variable for Hugging Face cache (optional, can speed up builds)
ENV TRANSFORMERS_CACHE=/app/.cache/huggingface/

# Install ffmpeg for audio processing and redis-tools for debugging/cli
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg redis-tools && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Create application directories and cache directory
RUN mkdir -p /app /data /output /app/.cache/huggingface
WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Upgrade pip and install Python dependencies
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container
COPY . /app

# Make data/output writable if needed (depends on user running inside container)
# RUN chown -R <some_user>:<some_group> /data /output # Usually handled by volume mounts

# Expose the Flask port
EXPOSE 5000

# Set the default command to run the Flask app using eventlet for SocketIO
CMD ["python", "-m", "eventlet", "webui_app.py"]