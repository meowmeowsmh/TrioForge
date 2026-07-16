FROM python:3.10-slim

WORKDIR /app

# Install system build tools (needed for psutil, sentence-transformers, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the code
COPY . .

# Create directories used as volumes
RUN mkdir -p json_configuration sqlite_data cert_store static/uploads

EXPOSE 5001

CMD ["python", "app.py"]