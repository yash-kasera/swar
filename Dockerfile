# Works on any Docker host (Hugging Face Spaces, Fly.io, Railway, Koyeb...). Hosted mode is on by default.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend backend
COPY frontend frontend
COPY samples samples
ENV SWAR_HOSTED=1 PORT=7860
EXPOSE 7860
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips '*'"]
