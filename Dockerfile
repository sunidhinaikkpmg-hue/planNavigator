FROM python:3.11-slim

WORKDIR /app

# If planNavigator has requirements.txt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

EXPOSE 8080

# Adjust based on your entry point file
CMD ["python", "app.py"]
# OR if it's main.py
# CMD ["python", "main.py"]
