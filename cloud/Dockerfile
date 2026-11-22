ARG GUANYE_PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${GUANYE_PYTHON_IMAGE}@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
ARG GUANYE_PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    HOME=/home/guanye

RUN set -eux; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        fontconfig \
        fonts-noto-cjk \
        libreoffice-impress; \
    fc-cache -f; \
    rm -rf /var/lib/apt/lists/*; \
    groupadd --gid 10001 guanye; \
    useradd --uid 10001 --gid 10001 --create-home --home-dir /home/guanye --shell /usr/sbin/nologin guanye

WORKDIR /app

COPY cloud/gateway-requirements.txt cloud/gateway-requirements.txt
RUN python -m pip install \
    --disable-pip-version-check \
    --no-cache-dir \
    --only-binary=:all: \
    --require-hashes \
    --index-url "${GUANYE_PIP_INDEX_URL}" \
    -r cloud/gateway-requirements.txt

COPY --chown=10001:10001 cloud/ai_gateway.py cloud/ai_reviews.py cloud/ai_planning.py cloud/ai_generation.py ./cloud/
COPY --chown=10001:10001 cloud/ecs_image_provider.py cloud/ecs_worker.py cloud/local_professional.py cloud/professional_creation.py cloud/professional_image_client.py cloud/professional_import.py cloud/professional_intake.py cloud/professional_review_worker.py cloud/windows_image_worker.py ./cloud/
COPY --chown=10001:10001 document_import.py ./document_import.py

USER 10001:10001
EXPOSE 8787
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-B", "-m", "cloud.ai_gateway"]
