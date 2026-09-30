FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 KASM_USE_TRANSPORT=http KASM_USE_HOST=0.0.0.0
COPY . /src
RUN pip install --no-cache-dir /src && rm -rf /src && useradd --uid 1000 --create-home app
USER app
EXPOSE 8000
CMD ["kasm-use"]
