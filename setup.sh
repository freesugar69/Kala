#!/bin/bash
# ==============================================================================
# Kala Voice Assistant - Automated System & Audio Driver Setup Script
# ==============================================================================
# This script installs all system dependencies, sound server utilities, audio
# drivers (ALSA, PortAudio, PulseAudio/PipeWire), Python packages, Vosk STT model,
# and configures the background systemd service for Linux (Kali / Ubuntu / Debian / KDE).
# ==============================================================================

set -e

KALA_DIR="/home/spiderjoker/Kala_cala"
MODEL_DIR="$KALA_DIR/model/it_model"

echo "================================================================="
echo "  🎙️  Kala Voice Assistant - Initializing System Setup"
echo "================================================================="

# 1. Update APT and install System Audio & Compiler Dependencies
echo "📦 Installing system dependencies and audio drivers..."
sudo apt-get update
sudo apt-get install -y \
    python3-pip \
    python3-venv \
    python3-dev \
    python3-pyaudio \
    python3-sounddevice \
    python3-vosk \
    portaudio19-dev \
    libasound2-dev \
    pulseaudio-utils \
    pipewire \
    mpg123 \
    espeak \
    ffmpeg \
    wget \
    unzip \
    git

# 2. Configure Python Virtual Environment
echo "🐍 Setting up Python Virtual Environment with system site packages..."
if [ -d "$KALA_DIR/venv" ]; then
    rm -rf "$KALA_DIR/venv"
fi
python3 -m venv --system-site-packages "$KALA_DIR/venv"

echo "📥 Installing required Python libraries..."
"$KALA_DIR/venv/bin/python3" -m pip install --upgrade pip --break-system-packages
"$KALA_DIR/venv/bin/python3" -m pip install -r "$KALA_DIR/requirements.txt" --break-system-packages

# 3. Download & Extract Vosk Italian Speech Recognition Model
if [ ! -d "$MODEL_DIR" ]; then
    echo "🧠 Downloading Vosk Italian Speech Model (~50 MB)..."
    mkdir -p "$KALA_DIR/model"
    wget -q --show-progress -O /tmp/vosk-model-small-it-0.22.zip https://alphacephei.com/vosk/models/vosk-model-small-it-0.22.zip
    unzip -q -o /tmp/vosk-model-small-it-0.22.zip -d "$KALA_DIR/model/"
    rm -rf "$MODEL_DIR"
    mv "$KALA_DIR/model/vosk-model-small-it-0.22" "$MODEL_DIR"
    rm -f /tmp/vosk-model-small-it-0.22.zip
    echo "✅ Vosk Model successfully installed."
else
    echo "✅ Vosk Speech Model already present at $MODEL_DIR"
fi

# 4. Install & Enable systemd User Service
echo "⚙️ Configuring systemd user service..."
SERVICE_DIR="$HOME/.config/systemd/user"
mkdir -p "$SERVICE_DIR"

cat << 'EOF' > "$SERVICE_DIR/kala.service"
[Unit]
Description=Kala Voice Assistant Daemon
After=pipewire.service pulseaudio.service

[Service]
Type=simple
WorkingDirectory=/home/spiderjoker/Kala_cala
ExecStart=/home/spiderjoker/Kala_cala/venv/bin/python3 /home/spiderjoker/Kala_cala/daemon/kala-daemon.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable kala.service
systemctl --user restart kala.service

echo "================================================================="
echo "  ✅ Kala Voice Assistant Setup Complete!"
echo "  To check live logs: ./bin/kala.sh logs"
echo "  To restart Kala:    ./bin/kala.sh restart"
echo "================================================================="
