# Moi truong Linux de chay lab tren Windows (chaos/kill_region.py can SIGSTOP/SIGCONT).
# Repo duoc mount vao /lab -> sua code tren Windows, chay lenh trong container.
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends make curl procps \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
WORKDIR /lab
CMD ["sleep", "infinity"]
