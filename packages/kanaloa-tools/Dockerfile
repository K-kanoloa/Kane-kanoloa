FROM python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 IPYTHONDIR=/tmp/ipython HOME=/tmp
COPY requirements.lock /opt/kanaloa/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /opt/kanaloa/requirements.lock
COPY worker.py /opt/kanaloa/worker.py
USER 1000:1000
WORKDIR /tmp
ENTRYPOINT ["python", "-I", "/opt/kanaloa/worker.py"]
