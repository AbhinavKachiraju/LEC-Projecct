FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default command runs the API; docker-compose overrides this for the
# dashboard service. Kept as a plain CMD (not baked into a specific
# service) so the same image serves both.
EXPOSE 8000 8501
CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8000"]
