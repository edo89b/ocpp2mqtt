FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY central_system.py .

CMD ["python", "-u", "central_system.py"]
