FROM python:3.11-slim

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .[server,ui]

EXPOSE 8000
EXPOSE 8501

CMD ["sh", "-c", "llm-gate serve --host 0.0.0.0 --port 8000 & llm-gate ui --server.port 8501 --server.address 0.0.0.0"]
