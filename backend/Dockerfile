# RationAI backend — FastAPI + PuLP solver + ChromaDB/RAG + LangGraph agent.
#
# Taban imaj neden slim (Alpine DEĞİL): PuLP'ın paket içinde gelen CBC binary'si ve
# chromadb'nin onnxruntime'ı glibc'ye bağlı; Alpine'ın musl libc'si ikisini de kırar.
FROM python:3.14-slim

# Python konteyner davranışı: .pyc yazma, logları tamponlama (docker logs anlık aksın).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Root olmayan kullanıcı. HOME açıkça verilir: ChromaDB'nin embedding modeli
# $HOME/.cache/chroma altına iner ve aşağıdaki build adımında imaja gömülür —
# runtime'da aynı HOME olmazsa model bulunamaz ve yeniden indirilmeye çalışılır.
RUN useradd --create-home --home-dir /home/app --shell /bin/bash app
ENV HOME=/home/app

WORKDIR /app

# Önce sadece bağımlılık dosyası kopyalanır: kaynak değiştiğinde pip katmanı
# önbellekten gelir, build hızlanır.
COPY requirements-backend.txt ./
RUN pip install --no-cache-dir -r requirements-backend.txt

# Uygulama kaynağı ve RAG kaynak dokümanları
COPY backend/ ./backend/
COPY data/docs/ ./data/docs/

RUN chown -R app:app /app
USER app

# ChromaDB indeksini build sırasında oluştur (~615 chunk / 12 MB) ve bu adımda inen
# ONNX embedding modelini (~166 MB) imaja göm. Sonuç: konteyner internetsiz çalışır,
# ilk sorguda indirme beklemesi olmaz ve herkeste birebir aynı indeks bulunur.
RUN python -m backend.app.rag

EXPOSE 8000

# curl kurmamak için sağlık kontrolü Python ile yapılır.
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
