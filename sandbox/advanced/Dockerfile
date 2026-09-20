FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY sandbox/advanced/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt
COPY contracts /contracts
RUN pip install /contracts
COPY sandbox/services/__init__.py /app/services/__init__.py
COPY sandbox/services/common /app/services/common
COPY sandbox/advanced/schema.sql sandbox/advanced/sql.py sandbox/advanced/evidence.py \
     sandbox/advanced/spec.json sandbox/advanced/app.py sandbox/advanced/store.py \
     sandbox/advanced/runtime.py sandbox/advanced/worker.py sandbox/advanced/relay.py \
     sandbox/advanced/loadgen.py sandbox/advanced/control.py sandbox/advanced/observe.py \
     sandbox/advanced/__init__.py /app/advanced/
RUN useradd --system --uid 1000 app && chown -R app:app /app
USER 1000
CMD ["uvicorn", "advanced.app:app", "--host", "0.0.0.0", "--port", "8000"]
