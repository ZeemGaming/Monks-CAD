FROM python:3.11-slim

# Install system utilities & OpenSSH server
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-server \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Configure SSH directory
RUN mkdir -p /var/run/sshd ~/.ssh && chmod 700 ~/.ssh

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Make entrypoint executable
RUN chmod +x entrypoint.sh

EXPOSE 8000 22

ENTRYPOINT ["/app/entrypoint.sh"]
