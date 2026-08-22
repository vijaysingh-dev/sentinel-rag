FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY monitor.py tree.yaml ./
CMD ["uvicorn", "monitor:app", "--host", "0.0.0.0", "--port", "8000"]
