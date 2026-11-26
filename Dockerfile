FROM python:3.12.16-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY rss_addon ./rss_addon
USER 10001:10001
EXPOSE 8775
CMD ["python", "-m", "rss_addon.server"]
