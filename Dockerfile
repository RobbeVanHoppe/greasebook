# GreaseBook web -- garage maintenance manager on a server.
FROM docker.io/library/python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY templates templates
COPY static static
COPY hwdocs hwdocs

# database + attachments live here -- mount a volume
ENV GREASEBOOK_DATA=/data
VOLUME /data

EXPOSE 8000
# waitress: production WSGI server, multi-threaded, pure Python
CMD ["waitress-serve", "--host=0.0.0.0", "--port=8000", "app:app"]
