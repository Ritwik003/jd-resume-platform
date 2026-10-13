FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# libreoffice gives the highest-fidelity DOCX->PDF conversion.
# Remove it to halve the image size; the ReportLab fallback still produces PDFs.
# curl/ca-certificates are only needed to fetch tectonic below; they stay in
# the final image (small, and useful for debugging in a shell) rather than
# adding a multi-stage copy for a few MB.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev libreoffice-writer curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# app/services/rendering/latex.py tries tectonic first because it resolves
# missing LaTeX packages itself -- the usual reason a borrowed resume template
# fails to build on a machine that only has a base TeX install. The official
# installer drops a single static binary; no TeX Live, no package manager.
# --installation-dir is silently ignored by this installer version (it always
# unpacks into the current directory), so the move is done explicitly rather
# than trusted to the flag -- verified by inspecting the actual build output.
WORKDIR /tmp/tectonic-install
RUN curl --proto '=https' --tlsv1.2 -fsSL https://drop-sh.fullyjustified.net | sh \
    && mv tectonic /usr/local/bin/tectonic \
    && cd / && rm -rf /tmp/tectonic-install \
    && tectonic --version

WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
