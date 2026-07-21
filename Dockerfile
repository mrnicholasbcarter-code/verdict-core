FROM python:3.11-slim

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir '.[server,dashboard]'

EXPOSE 8000
EXPOSE 8501

CMD ["sh", "-c", "verdict serve --host 0.0.0.0 --port 8000 & verdict dashboard --port 8501 --host 0.0.0.0"]
