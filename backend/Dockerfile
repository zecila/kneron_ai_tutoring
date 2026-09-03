# System dependencies:
# - tesseract-ocr: required by pytesseract for OCR
# - poppler-utils: required by pdf2image to rasterize PDF pages
# - libgl1 / libglib2.0-0: common runtime libs opencv-python-headless still
#   dynamically links against, even in headless mode
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    poppler-utils \
    libgl1 \
    libglib2.0-0 \
    curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Backend deps
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Frontend build
COPY frontend/ frontend/
RUN cd frontend && npm install && npm run build

# Backend code
COPY backend/ backend/
RUN chmod +x backend/start.sh

WORKDIR /app/backend

EXPOSE 5000

CMD ["./start.sh"]
