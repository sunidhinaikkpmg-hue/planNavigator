FROM python:3.11-slim

WORKDIR /app

# Copy and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Flask runs on port 5000 by default
EXPOSE 5000

# Run the application
CMD ["python", "app.py"]
