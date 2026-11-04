FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY src ./src
RUN pip install --no-cache-dir --no-deps . && useradd --create-home appuser
USER appuser
COPY tests ./tests
COPY alembic.ini ./
COPY migrations ./migrations
EXPOSE 8000
CMD ["uvicorn", "stockapp.main:app", "--host", "0.0.0.0", "--port", "8000"]
