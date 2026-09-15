#!/bin/bash
set -e

# Write your SSH key from environment variable if provided
if [ -n "$PUBLIC_SSH_KEY" ]; then
    echo "$PUBLIC_SSH_KEY" > ~/.ssh/authorized_keys
    chmod 600 ~/.ssh/authorized_keys
    echo "SSH Authorized key configured successfully."
fi

# Generate host keys if missing
ssh-keygen -A

# Start SSH daemon in background
/usr/sbin/sshd

# Start FastAPI application
exec uvicorn main:app --host 0.0.0.0 --port 8000
