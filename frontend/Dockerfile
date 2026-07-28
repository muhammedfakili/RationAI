# RationAI arayüzü — Streamlit. Backend ile yalnızca HTTP üzerinden konuşur,
# bu yüzden imajda chromadb/langchain/pulp yoktur.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN useradd --create-home --home-dir /home/app --shell /bin/bash app
ENV HOME=/home/app

WORKDIR /app

COPY requirements-frontend.txt ./
RUN pip install --no-cache-dir -r requirements-frontend.txt

COPY frontend/ ./frontend/
COPY .streamlit/ ./.streamlit/

RUN chown -R app:app /app
USER app

EXPOSE 8501

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4).status==200 else 1)"

# headless: tarayıcı açmaya çalışmasın; address 0.0.0.0: konteyner dışından erişilsin.
CMD ["streamlit", "run", "frontend/app.py", \
     "--server.address=0.0.0.0", "--server.port=8501", \
     "--server.headless=true", "--browser.gatherUsageStats=false"]
