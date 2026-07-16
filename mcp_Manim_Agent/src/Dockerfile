FROM python:3.11-slim

# System dependencies for Manim rendering:
# - ffmpeg: video encoding (Manim's actual render output)
# - libcairo2-dev, libpango1.0-dev: required by pycairo/ManimPango for
#   text and vector rendering
# - pkg-config: needed at build time so pip can locate the above libs
# - build-essential: pycairo/ManimPango compile C extensions during pip install
# - libgl1: OpenGL runtime, needed by moderngl/moderngl-window even
#   for headless/software rendering
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libcairo2-dev \
    libpango1.0-dev \
    pkg-config \
    build-essential \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python", "server.py"]