cd backend
cat > Dockerfile << 'EOF'
FROM python:3.11-slim

WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# If app.py is inside app/ directory
ENV PYTHONPATH=/app

EXPOSE 8080

# Adjust the path based on your structure
CMD ["gunicorn", "--bind", ":8080", "--timeout", "120", "app.app:app"]
EOF

# Create requirements.txt if it doesn't exist
cat > requirements.txt << 'EOF'
Flask==2.3.3
flask-cors==4.0.0
gunicorn==21.2.0
pandas
numpy
EOF
