# Verdict Core container image.
#
# Default: the credential-free offline demo (scripted workers, an injected fault, a scripted
# reviewer; CI runs it with --network none):
#   docker run --rm verdict-core
# Any other verdict command:
#   docker run --rm verdict-core --version
#   docker run --rm -p 8000:8000 -e LLMGATE_AUTH_TOKEN=... -e VERDICT_RECEIPTS_DB=/data/receipts.db \
#     -v verdict-data:/data verdict-core serve --host 0.0.0.0 --port 8000
#   docker run --rm -p 8501:8501 -e STREAMLIT_SERVER_ADDRESS=0.0.0.0 verdict-core ui
# `verdict serve` on a non-loopback host requires LLMGATE_AUTH_TOKEN and refuses to start an
# anonymous public server. With a token it also requires VERDICT_RECEIPTS_DB (a durable
# receipts database); /data is created writable for the non-root user for that purpose.
FROM python:3.12-slim

# git is required by `verdict demo` and `verdict orchestrate` (worktrees, integration).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir '.[server,dashboard]'

RUN useradd --create-home --uid 10001 verdict \
    && mkdir -p /data \
    && chown verdict:verdict /data
USER verdict
WORKDIR /home/verdict

EXPOSE 8000 8501

ENTRYPOINT ["verdict"]
CMD ["demo", "--speed", "0"]
