# Kala – Voice Assistant for Kali Linux / KDE / Debian

## 🇮🇹 Italiano

### Descrizione
Kala è un assistente vocale **Jarvis‑like** intelligente e leggero per Kali Linux e ambienti desktop come **KDE Plasma**.
- Riconosce la parola chiave **“Kala”**.
- Incorpora **AEC (Acoustic Echo Cancellation)** dinamico tramite PulseAudio/PipeWire per eliminare l'eco degli altoparlanti.
- **Supporto multi-schermo HDMI / KDE**: Rileva ed esclude automaticamente i flussi `monitor` audio generati dalle uscite HDMI dei monitor, garantendo l'ascolto dal microfono fisico reale.
- **Sintesi Vocale ad Alta Fedeltà**: Usa **Edge-TTS** (voce neurale italiana `it-IT-ElsaNeural`), con fallback automatico su **gTTS** ed **eSpeak**.
- **Riconoscimento Vocale Offline**: Alimentato dal motore **Vosk** offline per massima privacy e velocità.

### Requisiti di Sistema e Driver Audio
Prima di eseguire Kala, assicurarsi che i driver audio e i pacchetti di sistema siano installati:
- **Server Audio**: `PipeWire` o `PulseAudio` con supporto `pulseaudio-utils` (`pactl`).
- **Driver Linux Audio**: ALSA (`libasound2-dev`), PortAudio (`portaudio19-dev`), Python PyAudio & SoundDevice (`python3-pyaudio`, `python3-sounddevice`).
- **Riproduttori Audio**: `mpg123`, `espeak`, `ffmpeg`.

### Installazione Automatica (Consigliata)
Esegui lo script d'installazione automatizzato che controlla i driver, installa i pacchetti di sistema, configura l'ambiente Python e scarica il modello Vosk:

```bash
cd /home/spiderjoker/Kala_cala
./setup.sh
```

### Installazione Manuale
```bash
# 1️⃣ Installa le dipendenze di sistema e i driver audio
sudo apt update
sudo apt install -y python3-pip python3-venv python3-dev \
    python3-pyaudio python3-sounddevice python3-vosk \
    portaudio19-dev libasound2-dev pulseaudio-utils \
    pipewire mpg123 espeak ffmpeg wget unzip git

# 2️⃣ Configura l'ambiente virtuale Python
python3 -m venv --system-site-packages venv
./venv/bin/python3 -m pip install -r requirements.txt

# 3️⃣ Scarica il modello Vosk italiano (se non scaricato automaticamente)
mkdir -p model
wget -O /tmp/vosk-it.zip https://alphacephei.com/vosk/models/vosk-model-small-it-0.22.zip
unzip /tmp/vosk-it.zip -d model
mv model/vosk-model-small-it-0.22 model/it_model

# 4️⃣ Avvia o riavvia Kala
./bin/kala.sh restart
```

### Gestione e Log
- **Visualizza i log in tempo reale**: `./bin/kala.sh logs`
- **Riavvia Kala**: `./bin/kala.sh restart`
- **Stato del servizio**: `./bin/kala.sh status`

---

## 🇬🇧 English

### Description
Kala is a lightweight, **Jarvis‑style voice assistant** designed for Kali Linux, Debian, and **KDE Plasma** desktop environments.
- Listens for the wake-word **“Kala”**.
- Built-in **AEC (Acoustic Echo Cancellation)** via PipeWire/PulseAudio to prevent speaker loopbacks.
- **HDMI Multi-Monitor Support**: Automatically filters out virtual HDMI output monitor streams in dual-display KDE setups to bind directly to physical microphones.
- **High-Fidelity Neural Speech**: Uses **Edge-TTS** (`it-IT-ElsaNeural` / `en-US-JennyNeural`) with seamless fallbacks to **gTTS** and **eSpeak**.
- **Offline Speech-to-Text**: Powered by **Vosk** offline engine.

### System Requirements & Audio Drivers
Ensure system audio tools and drivers are present before running Kala:
- **Audio Server**: `PipeWire` or `PulseAudio` with `pulseaudio-utils` (`pactl`).
- **Linux Audio Libraries**: ALSA (`libasound2-dev`), PortAudio (`portaudio19-dev`), PyAudio, SoundDevice.
- **Audio Players**: `mpg123`, `espeak`, `ffmpeg`.

### Automated Installation (Recommended)
Run the setup script to install system drivers, Python packages, Vosk model, and systemd service:

```bash
cd /home/spiderjoker/Kala_cala
./setup.sh
```

### Manual Installation
```bash
# 1️⃣ Install system packages and audio drivers
sudo apt update
sudo apt install -y python3-pip python3-venv python3-dev \
    python3-pyaudio python3-sounddevice python3-vosk \
    portaudio19-dev libasound2-dev pulseaudio-utils \
    pipewire mpg123 espeak ffmpeg wget unzip git

# 2️⃣ Create virtual environment
python3 -m venv --system-site-packages venv
./venv/bin/python3 -m pip install -r requirements.txt

# 3️⃣ Start the service
./bin/kala.sh restart
```

---

### License
Licensed under the MIT License – see `LICENSE` for details.
