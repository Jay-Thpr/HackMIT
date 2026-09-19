# One image for every Python service; compose picks the module. Build context = repo root
# (so the fault controller and control service can use faultline_contracts models).
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY sandbox/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt
COPY contracts /contracts
RUN pip install /contracts
COPY sandbox/services /app/services
