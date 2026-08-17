FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY test_connection.py .
RUN useradd --create-home --uid 10001 connector
USER connector
ENV TRANSPORT=streamable-http HOST=0.0.0.0 PORT=8000
EXPOSE 8000
CMD ["python", "-m", "app.server"]
