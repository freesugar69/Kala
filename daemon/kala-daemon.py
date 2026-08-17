#!/usr/bin/env python3
"""Kala Voice Assistant Daemon.
Runs in the background, listens for the wake-word "Kala", and executes system commands.
"""

import json
import time
import pathlib
import sys
import os
import subprocess
import shutil
import difflib
import math
import struct
import queue
import threading

# Safe Imports
try:
    import pyautogui
    pyautogui.FAILSAFE = False
except Exception as e:
    pyautogui = None

try:
    import mss
except Exception:
    mss = None

try:
    import psutil
except Exception:
    psutil = None

try:
    import speech_recognition as sr
except Exception:
    sr = None

try:
    import sounddevice as sd
except Exception:
    sd = None

from vosk import Model, KaldiRecognizer

# Paths
KALA_DIR = pathlib.Path("/home/spiderjoker/Kala_cala")
LOG_PATH = KALA_DIR / "state" / "kala.log"
CONFIG_PATH = KALA_DIR / "state" / "config.json"
MODEL_PATH = KALA_DIR / "model" / "it_model"

LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} - {msg}")
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{ts} - {msg}\n")
    except Exception:
        pass

# Initialize State
current_language = "it"
try:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
            current_language = cfg.get("language", "it")
except Exception as e:
    log(f"Error loading language config: {e}")

log(f"Kala Daemon Starting. Default Language: {current_language}")

# Ambient Noise moving average for dynamic VAD
ambient_noise_level = 100.0

# TTS Process Tracker & Continuous Dialogue State
tts_process = None
continuous_mode_until = 0.0

def speak(text: str, lang: str, wait: bool = False, enable_followup: bool = True) -> None:
    """TTS reproduction via edge-tts (online neural), gtts (online standard) or espeak (fallback/offline)."""
    global tts_process, continuous_mode_until
    
    # Terminate currently playing speech
    if tts_process and tts_process.poll() is None:
        try:
            tts_process.terminate()
            tts_process.wait(timeout=0.3)
        except Exception:
            try:
                tts_process.kill()
            except Exception:
                pass

    log(f"Speaking: '{text}' in {lang}")
    
    mp3_path = "/tmp/kala_speech.mp3"
    played = False
    
    # 1. Try edge-tts (high-quality neural online voice)
    try:
        import asyncio, edge_tts, tempfile, os, threading
        voice = "it-IT-ElsaNeural" if lang == "it" else "en-US-JennyNeural"
        async def run_edge():
            comm = edge_tts.Communicate(text, voice)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
                await comm.save(tmp.name)
                return tmp.name
        audio_file = asyncio.run(run_edge())
        tts_process = subprocess.Popen(["mpg123", "-q", audio_file], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        played = True
        log("TTS (edge-tts): Success")
        def cleanup():
            global continuous_mode_until
            if tts_process:
                tts_process.wait()
            # Set continuous dialogue mode window (6s) after playback completes if enabled
            if enable_followup:
                continuous_mode_until = time.time() + 6.0
            try:
                os.remove(audio_file)
            except Exception:
                pass
        threading.Thread(target=cleanup, daemon=True).start()
    except Exception as e_edge:
        log(f"edge-tts failed ({e_edge}). Trying gTTS...")
        # 2. Try Google TTS (online standard)
        try:
            from gtts import gTTS
            tts = gTTS(text=text, lang=lang, slow=False)
            tts.save(mp3_path)
            tts_process = subprocess.Popen(["mpg123", "-q", mp3_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            played = True
            log("TTS (gTTS): Success")
            if enable_followup:
                continuous_mode_until = time.time() + 6.0
        except Exception as e_gtts:
            log(f"Google TTS failed ({e_gtts}). Falling back to espeak...")
            # 3. Try offline espeak fallback
            try:
                voice = "it+f2" if lang == "it" else "en+f2"
                tts_process = subprocess.Popen(["espeak", "-v", voice, "-s", "160", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                played = True
                log("TTS (espeak): Success (offline fallback)")
                if enable_followup:
                    continuous_mode_until = time.time() + 6.0
            except Exception as e_espeak:
                log(f"espeak failed ({e_espeak}). No TTS player available.")

    if wait and tts_process:
        try:
            tts_process.wait()
            if enable_followup:
                continuous_mode_until = time.time() + 6.0
        except Exception:
            pass

def stop_speak() -> None:
    """Instantly terminate current speech."""
    global tts_process
    if tts_process and tts_process.poll() is None:
        try:
            tts_process.terminate()
            tts_process.wait(timeout=0.2)
        except Exception:
            try:
                tts_process.kill()
            except Exception:
                pass
    # Bulletproof pkill to ensure audio stops instantly
    try:
        subprocess.Popen(["pkill", "-9", "mpg123"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.Popen(["pkill", "-9", "espeak"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

# Audio Device Search & HDMI Multi-Monitor Driver Filter
def find_working_stream() -> int | None:
    """Discovers and tests audio input devices for Voice Recognition.
    
    Audio Driver & Subsystem Architecture:
    --------------------------------------
    - Linux PipeWire / PulseAudio / ALSA audio architecture.
    - Uses PortAudio (via sounddevice) to interface with system input channels.
    
    Multi-Monitor / Dual-HDMI Filtering:
    ------------------------------------
    Filters out any ALSA/Pulse device containing 'hdmi' or 'monitor' in its name,
    preventing sounddevice from selecting HDMI output monitor capture streams as microphone inputs
    when running dual-monitor KDE setups.
    """
    if sd is None:
        log("sounddevice module is not available")
        return -1
    try:
        devices = sd.query_devices()
        log("Available audio devices:")
        for i, d in enumerate(devices):
            log(f"  [{i}]: {d['name']} (input ch: {d['max_input_channels']}, default SR: {d['default_samplerate']})")
            
        cand = []
        # Priority 1: Default, Pulse, PipeWire (excluding HDMI / Monitor capture loopbacks)
        for i, d in enumerate(devices):
            name_lower = d["name"].lower()
            if d["max_input_channels"] > 0 and "hdmi" not in name_lower and "monitor" not in name_lower:
                if "default" in name_lower or "pulse" in name_lower or "pipewire" in name_lower:
                    if i not in cand:
                        cand.append(i)
                        
        # Priority 2: Physical hardware inputs (e.g. sof-hda-dsp, hw, mic) excluding HDMI/Monitor
        for i, d in enumerate(devices):
            name_lower = d["name"].lower()
            if d["max_input_channels"] > 0 and "hdmi" not in name_lower and "monitor" not in name_lower:
                if i not in cand:
                    cand.append(i)

        log(f"Filtered microphone input candidates (excluding HDMI monitor streams): {cand}")

        for idx in cand:
            log(f"Testing input device [{idx}]: {devices[idx]['name']}")
            try:
                ts = sd.RawInputStream(
                    samplerate=16000,
                    blocksize=8000,
                    dtype="int16",
                    channels=1,
                    device=idx,
                )
                ts.close()
                log(f"--> Selected working input device: [{idx}] ({devices[idx]['name']})")
                return idx
            except Exception as e:
                log(f"--> Device [{idx}] failed: {e}")

        # Fallback to system default
        log("Trying system default input device...")
        try:
            ts = sd.RawInputStream(
                samplerate=16000,
                blocksize=8000,
                dtype="int16",
                channels=1,
            )
            ts.close()
            log("--> Default system device is working")
            return None
        except Exception as e:
            log(f"--> System default input failed: {e}")
    except Exception as e:
        log(f"Error during audio device scan: {e}")
    return -1

# Wake Word Recognition Logic with Strict Phonetic Filtering
WAKE_WORDS_IT = ["cala", "kala", "kalla", "calla", "chala", "quala", "koala", "calà"]
WAKE_WORDS_EN = ["kala", "cala", "kalla", "calla", "kayla", "koala"]

def is_wake_word(word: str, lang: str) -> bool:
    word = word.lower().strip()
    if not word or len(word) < 4:
        return False
    # Exclude common false positive Italian words
    if word in ["alla", "dalla", "dello", "delle", "della", "dalle", "qual", "quale", "quella", "questa", "calabria", "artefatto", "tua", "sua", "mio"]:
        return False
    targets = WAKE_WORDS_IT if lang == "it" else WAKE_WORDS_EN
    if word in targets:
        return True
    # Strict fuzzy matching check (similarity >= 0.88) to prevent false wake triggers
    for t in targets:
        if difflib.SequenceMatcher(None, word, t).ratio() >= 0.88:
            return True
    return False

# Amplitude calculation for Noise Gate and VAD
def get_amplitude(data_bytes: bytes) -> float:
    try:
        samples = struct.unpack(f"{len(data_bytes)//2}h", data_bytes)
        if not samples:
            return 0.0
        return sum(abs(s) for s in samples) / len(samples)
    except Exception:
        return 0.0

# Set system language
def set_language(lang: str) -> None:
    global current_language
    current_language = lang
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump({"language": lang}, f)
        log(f"Language configuration updated to: {lang}")
    except Exception as e:
        log(f"Error writing to config.json: {e}")

# Search for a file in standard directories
def find_file_in_folders(filename: str) -> pathlib.Path | None:
    search_dirs = [
        pathlib.Path.home(),
        pathlib.Path.home() / "Documenti",
        pathlib.Path.home() / "Documents",
        pathlib.Path.home() / "Downloads",
        pathlib.Path.home() / "Scaricati",
        pathlib.Path.home() / "Scrivania",
        pathlib.Path.home() / "Desktop"
    ]
    filename = filename.lower()
    for d in search_dirs:
        if d.exists() and d.is_dir():
            try:
                for item in d.iterdir():
                    if item.is_file() and filename in item.name.lower():
                        return item
            except Exception:
                pass
    return None

# Jarvis-style Folder Navigation
awaiting_subfolder_choice = False
subfolder_base_path = None

def open_folder_jarvis(folder_path: pathlib.Path, name: str) -> None:
    global awaiting_subfolder_choice, subfolder_base_path, current_language
    if not folder_path.exists() or not folder_path.is_dir():
        msg = f"La cartella {name} non esiste." if current_language == "it" else f"Folder {name} does not exist."
        speak(msg, current_language)
        return
        
    try:
        subprocess.Popen(["xdg-open", str(folder_path)])
        log(f"Opened folder: {folder_path}")
    except Exception as e:
        log(f"Error opening folder with xdg-open: {e}")

    try:
        subfolders = [f for f in os.listdir(folder_path) if os.path.isdir(folder_path / f)]
        if subfolders:
            listed = ", ".join(subfolders[:5])
            if current_language == "it":
                speak(f"Apro {name}. Sotto cartelle disponibili: {listed}. Quale vuoi aprire?", "it", wait=True)
            else:
                speak(f"Opening {name}. Available subfolders: {listed}. Which one would you like to open?", "en", wait=True)
            awaiting_subfolder_choice = True
            subfolder_base_path = folder_path
        else:
            msg = f"Apro {name}." if current_language == "it" else f"Opening {name}."
            speak(msg, current_language)
    except Exception as e:
        log(f"Error listing directory contents: {e}")
        msg = f"Apro {name}." if current_language == "it" else f"Opening {name}."
        speak(msg, current_language)

def find_and_open_folder(target: str) -> bool:
    """Intelligently locates and opens requested folders (including subfolders like 'build') on Scrivania / Desktop / Home / Downloads / Documents."""
    target_lower = target.lower()
    
    # Common locations to check
    base_dirs = [
        pathlib.Path.home() / "Scrivania",
        pathlib.Path.home() / "Desktop",
        pathlib.Path.home() / "Documenti",
        pathlib.Path.home() / "Documents",
        pathlib.Path.home() / "Downloads",
        pathlib.Path.home() / "Scaricati",
        pathlib.Path.home()
    ]
    
    # Extract potential folder words from target (e.g. "build" from "apri la cartella build sul desktop")
    words = [w.strip(",. '") for w in target_lower.split() if len(w) > 2 and w not in ["apri", "apre", "cartella", "folder", "sul", "sullo", "nella", "dello", "schermo", "desktop", "scrivania", "inferiore", "superiore", "secondario", "principale", "che", "trova", "si"]]
    
    # Search for matching subfolder in base_dirs first
    for b in base_dirs:
        if b.exists() and b.is_dir():
            try:
                for item in b.iterdir():
                    if item.is_dir():
                        item_name = item.name.lower()
                        for w in words:
                            if w == item_name or w in item_name:
                                log(f"Found target subfolder: {item}")
                                open_folder_jarvis(item, item.name)
                                return True
            except Exception:
                pass
                
    # Fallback check for base folder shortcuts (Scrivania, Desktop, Documenti, Downloads, Home)
    shortcuts = {
        "scrivania": pathlib.Path.home() / "Scrivania",
        "desktop": pathlib.Path.home() / "Desktop",
        "documenti": pathlib.Path.home() / "Documenti",
        "documents": pathlib.Path.home() / "Documents",
        "downloads": pathlib.Path.home() / "Downloads",
        "scaricati": pathlib.Path.home() / "Scaricati",
        "home": pathlib.Path.home()
    }
    for key, path in shortcuts.items():
        if key in target_lower and path.exists():
            log(f"Opening base folder shortcut: {path}")
            open_folder_jarvis(path, key)
            return True
            
    return False

def answer_subfolder_choice(txt_lower: str) -> None:
    global awaiting_subfolder_choice, subfolder_base_path, current_language
    if not awaiting_subfolder_choice or not subfolder_base_path:
        return
        
    awaiting_subfolder_choice = False
    base = subfolder_base_path
    subfolder_base_path = None
    
    txt_clean = txt_lower.strip()
    
    # Strip common prefixes like "apri la sottocartella", "apri la cartella", "apri", "open"
    prefixes = [
        "apri l' sottocartella ", "apri la sottocartella ", "apri la cartella ", "apri il sottoprogetto ",
        "apri sottocartella ", "apri cartella ", "apri ",
        "open subfolder ", "open folder ", "open "
    ]
    for p in prefixes:
        if txt_clean.startswith(p):
            txt_clean = txt_clean[len(p):].strip()
            break

    try:
        candidates = [f for f in os.listdir(base) if os.path.isdir(base / f)]
        chosen = None
        
        # Auto-match if only 1 subfolder exists or user says generic phrase like "questa cartella", "disponibile", "la prima"
        if candidates and (len(candidates) == 1 or any(w in txt_clean for w in ["disponibile", "questa", "quella", "prima", "si", "sì", "apri", "cartella"])):
            chosen = candidates[0]

        # 1. Exact or substring match on the cleaned input
        if not chosen:
            for cand in candidates:
                cand_l = cand.lower()
                if cand_l == txt_clean or txt_clean in cand_l or cand_l in txt_clean:
                    chosen = cand
                    break
                    
        # 2. Match by individual words/keywords
        if not chosen:
            txt_words = txt_clean.split()
            for cand in candidates:
                cand_l = cand.lower()
                cand_words = cand_l.replace("_", " ").replace("-", " ").split()
                for cw in cand_words:
                    if len(cw) >= 3 and cw in txt_words:
                        chosen = cand
                        break
                if chosen:
                    break
                    
        # 3. Fuzzy match fallback
        if not chosen:
            matches = difflib.get_close_matches(txt_clean, candidates, n=1, cutoff=0.4)
            if matches:
                chosen = matches[0]
                
        if chosen:
            full_path = base / chosen
            subprocess.Popen(["xdg-open", str(full_path)])
            log(f"Opened subfolder: {full_path}")
            msg = f"Apro {chosen}." if current_language == "it" else f"Opening {chosen}."
            speak(msg, current_language)
        else:
            msg = f"Non ho trovato la cartella {txt_clean}." if current_language == "it" else f"I couldn't find the folder {txt_clean}."
            speak(msg, current_language)
    except Exception as e:
        log(f"Error in subfolder selection: {e}")
        msg = f"Errore nell'apertura della cartella." if current_language == "it" else f"Error opening folder."
        speak(msg, current_language)

# Flatpak launcher utility
def launch_flatpak(app_name: str) -> bool:
    global current_language
    import subprocess
    try:
        res = subprocess.run(["flatpak", "list", "--columns=application,name"], capture_output=True, text=True, check=True)
        lines = res.stdout.strip().split("\n")
        app_name_clean = app_name.lower().replace(" ", "").strip()
        if not app_name_clean or len(app_name_clean) < 3 or app_name_clean in ["apri", "open", "cartella", "folder", "app"]:
            return False
        for line in lines:
            parts = line.split("\t")
            if len(parts) >= 2:
                app_id, name = parts[0].strip(), parts[1].strip()
                if app_name_clean in name.lower().replace(" ", "") or app_name_clean in app_id.lower():
                    log(f"Found Flatpak match: {name} ({app_id}) for search '{app_name}'")
                    subprocess.Popen(["flatpak", "run", app_id])
                    msg = f"Apro {name}" if current_language == "it" else f"Opening {name}"
                    speak(msg, current_language)
                    return True
    except Exception as e:
        log(f"Flatpak check failed: {e}")
    return False

# Web Search Scraper (DuckDuckGo Lite/HTML)
def search_web_results(query: str) -> str:
    import urllib.parse
    import requests
    import re
    try:
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
        }
        res = requests.get(url, headers=headers, timeout=6)
        if res.status_code != 200:
            return ""
        
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(res.text, "html.parser")
            snippets = [s.get_text().strip() for s in soup.find_all("a", class_="result__snippet")]
            if snippets:
                return "\n".join(snippets[:3])
        except Exception:
            # Fallback regex extraction of DuckDuckGo result snippets
            matches = re.findall(r'<a class="result__snippet"[^>]*>(.*?)</a>', res.text, re.DOTALL)
            snippets = []
            for m in matches:
                clean = re.sub(r'<[^>]+>', '', m).strip()
                if clean:
                    snippets.append(clean)
            if snippets:
                return "\n".join(snippets[:3])
    except Exception as e:
        log(f"Error searching web: {e}")
    return ""

# Conversation memory state
conversation_history = []

# Helper function: Query local Ollama model using the Chat API with Conversation Memory
def query_ollama(prompt: str) -> str:
    global conversation_history, current_language
    import requests
    
    # Check if this query benefits from web search
    search_context = ""
    prompt_lower = prompt.lower()
    
    it_triggers = ["chi è", "chi era", "cos'è", "cosa sono", "meteo", "notizie", "chi ha", "dove si trova", "quando", "quanti", "perché", "come funziona", "qual è", "quali sono", "cerca", "trova", "che giorno", "che ore", "quanti anni", "chi fu", "dov'è", "che giorno è"]
    en_triggers = ["who is", "who was", "what is", "what are", "weather", "news", "where is", "when", "why", "how does", "search", "find", "what day", "what time", "how old"]
    
    needs_search = False
    if current_language == "it" and any(t in prompt_lower for t in it_triggers):
        needs_search = True
    elif current_language == "en" and any(t in prompt_lower for t in en_triggers):
        needs_search = True
        
    if needs_search:
        search_query = prompt
        for prefix in ["kala cerca ", "kala mi dici ", "kala mi spieghi ", "kala ", "cerca ", "trova ", "mi dici ", "mi spieghi "]:
            if search_query.lower().startswith(prefix):
                search_query = search_query[len(prefix):].strip()
                break
        
        log(f"Triggering web search for: '{search_query}'")
        search_context = search_web_results(search_query)
        if search_context:
            log(f"Web search retrieved context: {len(search_context)} chars")
        else:
            log("Web search retrieved no results or failed.")
            
    models = ["dolphin-llama3:latest", "kali-agent:latest", "llama3:latest"]
    for model in models:
        try:
            log(f"Querying Ollama model '{model}' via chat API with prompt: '{prompt}'")
            
            # Calculate current system date and time dynamically
            import datetime
            now = datetime.datetime.now()
            days_it = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
            months_it = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
            current_date_it = f"{days_it[now.weekday()]} {now.day} {months_it[now.month-1]} {now.year}, ore {now.strftime('%H:%M')}"
            
            days_en = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            months_en = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
            current_date_en = f"{days_en[now.weekday()]}, {months_en[now.month-1]} {now.day}, {now.year} at {now.strftime('%I:%M %p')}"
            
            system_prompt = (
                "Il tuo nome è Kala. Sei un'assistente virtuale vocale intelligente ed amichevole (stile Jarvis) per Kali Linux. "
                "Rispondi in italiano in modo diretto, naturale e discorsivo, senza usare elenchi o markdown. "
                f"NOTA: La data e l'ora corrente del sistema dell'utente sono: {current_date_it}. "
                "IMPORTANTE: Tu PUOI vedere lo schermo dell'utente ed eseguire qualsiasi comando sul sistema (aprire cartelle, controllare il mouse, digitare testo, ecc.). "
                "Se l'utente ti chiede se vedi lo schermo, se puoi cliccare o fare qualcosa sul desktop, rispondi di sì ed incoraggialo ad usare comandi come 'clicca su [testo]', 'doppio click su [testo]', o 'cattura schermo'. "
                "Rispondi con un massimo di 2 o 3 frasi."
            )
            if current_language == "en":
                system_prompt = (
                    "Your name is Kala. You are a friendly and intelligent virtual assistant (Jarvis style) for Kali Linux. "
                    "Respond directly, naturally, and colloquially, without lists or markdown. "
                    f"NOTE: The user's current system date and time are: {current_date_en}. "
                    "IMPORTANT: You CAN see the user's screen and execute any command on the system (open folders, control the mouse, type text, etc.). "
                    "If the user asks if you see the screen or can click/do something on the desktop, say yes and encourage them to use commands like 'clicca su [testo]', 'doppio click su [testo]', or 'cattura schermo'. "
                    "Respond with a maximum of 2 or 3 sentences."
                )
            
            if search_context:
                if current_language == "it":
                    system_prompt += f"\n\nUsa le seguenti informazioni provenienti dal web per rispondere alla domanda dell'utente in modo aggiornato:\n{search_context}"
                else:
                    system_prompt += f"\n\nUse the following web search results to answer the user's question with up-to-date facts:\n{search_context}"
                    
            # Build messages payload with conversation history
            if len(conversation_history) > 6:
                conversation_history = conversation_history[-6:]
                
            messages_payload = [{"role": "system", "content": system_prompt}] + list(conversation_history) + [{"role": "user", "content": prompt}]
            
            response = requests.post(
                "http://localhost:11434/api/chat",
                json={
                    "model": model,
                    "messages": messages_payload,
                    "options": {
                        "temperature": 0.6,
                        "num_predict": 120
                    },
                    "stream": False
                },
                timeout=15
            )
            if response.status_code == 200:
                text = response.json().get("message", {}).get("content", "").strip()
                if text.lower().startswith("kala:"):
                    text = text[5:].strip()
                elif text.lower().startswith("dolphin:"):
                    text = text[8:].strip()
                text = text.replace("*", "").replace("`", "").replace("#", "")
                
                # Record in conversation memory
                conversation_history.append({"role": "user", "content": prompt})
                conversation_history.append({"role": "assistant", "content": text})
                
                return text
        except Exception as e:
            log(f"Ollama query for model '{model}' failed: {e}")
    return ""

# Helper function: Handle code generation via Ollama (using codestral preferred)
def handle_code_generation(prompt: str) -> bool:
    global current_language
    prompt_lower = prompt.lower()
    
    code_keywords_it = ["fai un codice", "scrivi un codice", "crea un codice", "genera codice", "puoi fare codice", "fai codice", "puoi fare codici", "fai codici"]
    code_keywords_en = ["write code", "generate code", "create code", "make code", "write a program", "generate a program"]
    
    is_code_request = False
    if current_language == "it" and any(k in prompt_lower for k in code_keywords_it):
        is_code_request = True
    elif current_language == "en" and any(k in prompt_lower for k in code_keywords_en):
        is_code_request = True
        
    if not is_code_request:
        return False
        
    speak("Sto generando il codice richiesto, attendi un momento..." if current_language == "it" else "Generating the requested code, please wait...", current_language)
    
    import requests
    models = ["codestral:latest", "dolphin-llama3:latest", "llama3:latest"]
    
    ext = "txt"
    if "python" in prompt_lower: ext = "py"
    elif "bash" in prompt_lower or "script shell" in prompt_lower or "sh " in prompt_lower: ext = "sh"
    elif "html" in prompt_lower: ext = "html"
    elif "javascript" in prompt_lower or " js " in prompt_lower: ext = "js"
    elif "cpp" in prompt_lower or "c++" in prompt_lower: ext = "cpp"
    elif " c " in prompt_lower: ext = "c"
    
    system_prompt = (
        "Sei un assistente programmatore. Genera il codice richiesto dall'utente. "
        "Fornisci il codice completo, corretto e pronto all'uso. Includi brevi commenti spiegativi nel codice. "
        "Fornisci solo il codice racchiuso all'interno di un blocco di codice markdown (es. ```python ... ```)."
    )
    if current_language == "en":
        system_prompt = (
            "You are a programming assistant. Generate the code requested by the user. "
            "Provide the complete, correct, and ready-to-use code with brief explanatory comments. "
            "Provide only the code enclosed in a markdown code block (e.g. ```python ... ```)."
        )
        
    code_text = ""
    for model in models:
        try:
            log(f"Querying Ollama model '{model}' for code generation...")
            response = requests.post(
                "http://localhost:11434/api/generate",
                json={
                    "model": model,
                    "prompt": f"{system_prompt}\n\nRichiesta: {prompt}\nRisposta:",
                    "stream": False
                },
                timeout=30
            )
            if response.status_code == 200:
                code_text = response.json().get("response", "").strip()
                break
        except Exception as e:
            log(f"Ollama code query for model '{model}' failed: {e}")
            
    if not code_text:
        speak("Scusami, si è verificato un errore durante la generazione del codice." if current_language == "it" else "Sorry, an error occurred during code generation.", current_language)
        return True
        
    extracted_code = ""
    if "```" in code_text:
        parts = code_text.split("```")
        for i in range(1, len(parts), 2):
            block = parts[i]
            lines = block.splitlines()
            if lines:
                if lines[0].strip() in ["python", "bash", "sh", "html", "javascript", "js", "cpp", "c", "css", "json"]:
                    extracted_code = "\n".join(lines[1:])
                else:
                    extracted_code = block
                break
    
    if not extracted_code:
        extracted_code = code_text
        
    doc_dir = pathlib.Path.home() / "Documenti"
    if not doc_dir.exists():
        doc_dir = pathlib.Path.home() / "Documents"
    if not doc_dir.exists():
        doc_dir = pathlib.Path.home()
        
    file_path = doc_dir / f"codice_generato.{ext}"
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(extracted_code)
        log(f"Saved generated code to {file_path}")
        subprocess.Popen(["xdg-open", str(file_path)])
        
        if current_language == "it":
            speak(f"Ho generato il codice e l'ho salvato in Documenti come codice_generato punto {ext}, aprendolo nell'editor.", "it")
        else:
            speak(f"I generated the code and saved it to Documents as generated_code dot {ext}, opening it in the editor.", "en")
    except Exception as e:
        log(f"Error saving generated code: {e}")
        speak("Codice generato, ma non sono riuscito a salvarlo." if current_language == "it" else "Code generated, but I failed to save it.", current_language)
    return True

# Helper function: Click on text located anywhere on screen using OCR.space API
def click_on_text(target_text: str, double_click: bool = False) -> bool:
    global current_language
    img_path = "/tmp/kala_ocr_screenshot.png"
    if not pyautogui:
        return False
    try:
        pyautogui.screenshot(img_path)
    except Exception as e:
        log(f"OCR click screenshot failed: {e}")
        return False
        
    import requests
    url = "https://api.ocr.space/parse/image"
    try:
        log(f"Sending screenshot to OCR.space to find '{target_text}'...")
        payload = {
            "apikey": "helloworld",
            "language": "ita" if current_language == "it" else "eng",
            "isOverlayRequired": True
        }
        with open(img_path, "rb") as f:
            files = {"file": f}
            response = requests.post(url, data=payload, files=files, timeout=12)
            
        if response.status_code != 200:
            log(f"OCR.space API error: HTTP {response.status_code}")
            return False
            
        data = response.json()
        results = data.get("ParsedResults", [])
        if not results:
            log("OCR.space returned no results")
            return False
            
        overlay = results[0].get("TextOverlay", {})
        lines = overlay.get("Lines", [])
        
        target_words = target_text.lower().split()
        if not target_words:
            return False
            
        log(f"Analyzing {len(lines)} lines of screen text...")
        for line in lines:
            line_text = line.get("LineText", "").lower()
            if target_text.lower() in line_text:
                words_list = line.get("Words", [])
                for i in range(len(words_list) - len(target_words) + 1):
                    match = True
                    for j in range(len(target_words)):
                        word_in_line = words_list[i + j].get("WordText", "").lower()
                        word_clean = "".join(c for c in word_in_line if c.isalnum())
                        target_clean = "".join(c for c in target_words[j] if c.isalnum())
                        if target_clean not in word_clean and word_clean not in target_clean:
                            match = False
                            break
                    if match:
                        matched_segment = words_list[i:i+len(target_words)]
                        lefts = [w.get("Left", 0) for w in matched_segment]
                        tops = [w.get("Top", 0) for w in matched_segment]
                        widths = [w.get("Width", 0) for w in matched_segment]
                        heights = [w.get("Height", 0) for w in matched_segment]
                        
                        x_start = min(lefts)
                        y_start = min(tops)
                        x_end = max(lefts[k] + widths[k] for k in range(len(lefts)))
                        y_end = max(tops[k] + heights[k] for k in range(len(tops)))
                        
                        center_x = (x_start + x_end) // 2
                        center_y = (y_start + y_end) // 2
                        
                        log(f"OCR match found at ({center_x}, {center_y})")
                        pyautogui.moveTo(center_x, center_y, duration=0.5)
                        if double_click:
                            pyautogui.doubleClick()
                            confirm = f"Doppio click effettuato su {target_text}" if current_language == "it" else f"Double clicked on {target_text}"
                        else:
                            pyautogui.click()
                            confirm = f"Cliccato su {target_text}" if current_language == "it" else f"Clicked on {target_text}"
                        speak(confirm, current_language)
                        return True
        log(f"OCR: '{target_text}' not found on screen.")
        return False
    except Exception as e:
        log(f"Error during OCR click: {e}")
        return False

# Transcription Correction Utility
def correct_transcription_errors(text: str) -> str:
    # 1. Clean wake words / phonetic mishearings at the beginning of command
    wake_misheard = ["scala", "gala", "galà", "sala", "tala", "cora", "cara", "casa", "culla", "clara", "guarda", "solar", "solare", "caso", "d'uso", "l'uso", "l'uso."]
    words = text.split()
    if words and words[0].lower().strip(".,?!") in wake_misheard:
        text = " ".join(words[1:]).strip()
        
    # 2. Correct common typos/phonetic errors case-insensitively
    corrections = {
        "galastop": "kala stop",
        "kalastop": "kala stop",
        "calastop": "kala stop",
        "gala stop": "kala stop",
        "scala stop": "kala stop",
        "gala basta": "kala basta",
        "scala basta": "kala basta",
        "bravo browser": "brave browser",
        "bravi browser": "brave browser",
        "breve browser": "brave browser",
        "grave browser": "brave browser",
        "brave browser": "brave-browser",
        "guazzab": "whatsapp",
        "wattsapp": "whatsapp",
        "watsup": "whatsapp",
        "zap zap": "zapzap",
        "zapzap": "zapzap",
        "v il c": "vlc",
        "vlc sala": "vlc",
        "editore": "editor",
        "brauser": "browser",
        "massimizza la finestra di browser": "massimizza la finestra del browser",
        "massimizza finestra browser": "massimizza la finestra del browser"
    }
    
    text_lower = text.lower()
    for wrong, right in corrections.items():
        if wrong in text_lower:
            text = text.replace(wrong, right)
            text = text.replace(wrong.title(), right)
            text = text.replace(wrong.upper(), right.upper())
            
    return text

last_spoken_text = ""

# Command Handling Logic
def handle_command(txt: str) -> None:
    global current_language, awaiting_subfolder_choice, subfolder_base_path, continuous_mode_until, last_spoken_text
    txt_lower = txt.lower().strip()
    log(f"Command text: '{txt_lower}'")

    # Immediate Speech Stop / Interruption
    if any(x in txt_lower for x in ["kala stop", "galastop", "kalastop", "gala stop", "scala stop", "stop", "basta", "zitta", "zitto", "ferma", "fermati", "silenzio", "shh"]):
        log(f"Instant speech stop requested by command: '{txt_lower}'")
        stop_speak()
        continuous_mode_until = 0.0
        return

    # Self-Echo Detection: ignore if command is Kala repeating her own recent TTS output
    if last_spoken_text and len(txt_lower) > 8:
        last_clean = last_spoken_text.lower().strip(".,?!")
        if txt_lower in last_clean or last_clean in txt_lower:
            log(f"Self-echo detected ('{txt_lower}' matching recent assistant speech). Ignoring.")
            return

    # Subfolder Choice state: answer pending question first
    if awaiting_subfolder_choice:
        answer_subfolder_choice(txt_lower)
        return

    # Voice Shutdown
    if txt_lower in ["spegni", "arresta", "arrestati", "shutdown", "exit", "spegni kala", "arresta kala"]:
        msg = "Arresto l'assistente Kala. Arrivederci!" if current_language == "it" else "Shutting down Kala assistant. Goodbye!"
        speak(msg, current_language, wait=True)
        log("Shutdown requested by voice command.")
        os._exit(0)

    # Language Switch Command
    if "lingua" in txt_lower or "language" in txt_lower:
        if "inglese" in txt_lower or "english" in txt_lower:
            set_language("en")
            speak("Default language set to English.", "en")
            return
        elif "italiano" in txt_lower or "italian" in txt_lower:
            set_language("it")
            speak("Lingua predefinita impostata in italiano.", "it")
            return

    # Subfolder Choice state
    if awaiting_subfolder_choice:
        answer_subfolder_choice(txt_lower)
        return

    # Text-Clicking commands on screen (Jarvis capability to "see" and click the desktop)
    is_click_request = False
    is_double = False
    target_click_text = ""
    
    if current_language == "it":
        if txt_lower.startswith("doppio click su ") or txt_lower.startswith("doppio clic su "):
            is_click_request = True
            is_double = True
            target_click_text = txt[16:].strip()
        elif txt_lower.startswith("doppio click ") or txt_lower.startswith("doppio clic "):
            is_click_request = True
            is_double = True
            target_click_text = txt[13:].strip()
        elif txt_lower.startswith("clicca su ") or txt_lower.startswith("click su "):
            is_click_request = True
            target_click_text = txt[10:].strip()
        elif txt_lower.startswith("clicca ") or txt_lower.startswith("click "):
            words = txt_lower.split()
            if len(words) > 1:
                is_click_request = True
                target_click_text = txt[len(words[0])+1:].strip()
    else: # English
        if txt_lower.startswith("double click on "):
            is_click_request = True
            is_double = True
            target_click_text = txt[16:].strip()
        elif txt_lower.startswith("double click "):
            is_click_request = True
            is_double = True
            target_click_text = txt[13:].strip()
        elif txt_lower.startswith("click on "):
            is_click_request = True
            target_click_text = txt[9:].strip()
        elif txt_lower.startswith("click "):
            words = txt_lower.split()
            if len(words) > 1:
                is_click_request = True
                target_click_text = txt[len(words[0])+1:].strip()

    if is_click_request and target_click_text:
        # Avoid overriding relative movement coordinates or general click commands
        if target_click_text.lower() not in ["destra", "sinistra", "alto", "basso", "sopra", "sotto", "right", "left", "up", "down", "destro", "sinistro"]:
            speak("Sto cercando l'elemento sullo schermo..." if current_language == "it" else "Searching for the element on screen...", current_language)
            success = click_on_text(target_click_text, is_double)
            if not success:
                speak("Non sono riuscita a trovare l'elemento sullo schermo." if current_language == "it" else "I could not find that element on the screen.", current_language)
            return

    # Code Generation Commands (Ollama)
    if handle_code_generation(txt):
        return

    # Greetings and Basic Conversation
    greetings_it = {
        "ciao": "Ciao! Come posso aiutarti oggi?",
        "buongiorno": "Buongiorno! Sono pronta ad aiutarti.",
        "buonasera": "Buonasera! Come posso esserti utile?",
        "grazie": "Prego! È un piacere aiutarti.",
        "come stai": "Sto benissimo, grazie! E tu come stai?",
        "chi sei": "Sono Kala, la tua assistente virtuale intelligente.",
        "sei pronta": "Sempre pronta e ai tuoi comandi.",
        "sei pronto": "Sempre pronta e ai tuoi comandi.",
        "ci sei": "Sì, sono qui! Come posso aiutarti?",
    }
    
    greetings_en = {
        "hello": "Hello! How can I help you today?",
        "hi": "Hi there! What can I do for you?",
        "good morning": "Good morning! Ready to assist you.",
        "good evening": "Good evening! How can I help?",
        "thank you": "You're welcome! Happy to help.",
        "thanks": "You're welcome!",
        "how are you": "I'm doing great, thank you! How are you?",
        "who are you": "I am Kala, your personal virtual assistant.",
        "are you ready": "Always ready for your commands.",
        "are you there": "Yes, I am here! What do you need?",
    }

    greetings = greetings_it if current_language == "it" else greetings_en
    for greet, reply in greetings.items():
        if greet in txt_lower:
            speak(reply, current_language)
            return

    # System Status / Telemetry
    is_telemetry = False
    if current_language == "it":
        if any(x in txt_lower for x in ["stato del sistema", "telemetria", "come sta il computer", "stato sistema"]):
            is_telemetry = True
    else:
        if any(x in txt_lower for x in ["system status", "telemetry", "how is the computer", "pc status"]):
            is_telemetry = True

    if is_telemetry:
        if psutil:
            cpu_usage = psutil.cpu_percent(interval=0.1)
            ram_usage = psutil.virtual_memory().percent
            disk_usage = psutil.disk_usage('/').percent
            battery = psutil.sensors_battery()
            battery_msg = ""
            if battery:
                plugged = "in carica" if battery.power_plugged else "non in carica"
                if current_language == "it":
                    battery_msg = f", batteria al {battery.percent}% ({plugged})"
                else:
                    plugged_en = "charging" if battery.power_plugged else "discharging"
                    battery_msg = f", battery at {battery.percent}% ({plugged_en})"
                    
            if current_language == "it":
                status_text = f"Processore al {cpu_usage}%, memoria utilizzata al {ram_usage}%, spazio disco utilizzato al {disk_usage}%{battery_msg}. Sistema stabile."
            else:
                status_text = f"CPU usage is {cpu_usage}%, memory is {ram_usage}%, disk space is {disk_usage}%{battery_msg}. All systems nominal."
        else:
            status_text = "Telemetria non disponibile." if current_language == "it" else "Telemetry module not available."
        speak(status_text, current_language)
        return

    # Keyboard Control: Type Text
    is_typing = False
    to_type = ""
    click_search_first = False
    
    if "scrivi sulla barra di ricerca e scrivi" in txt_lower:
        is_typing = True
        click_search_first = True
        idx = txt_lower.find("scrivi sulla barra di ricerca e scrivi") + len("scrivi sulla barra di ricerca e scrivi")
        to_type = txt[idx:].strip()
    elif "digita sulla barra di ricerca e digita" in txt_lower:
        is_typing = True
        click_search_first = True
        idx = txt_lower.find("digita sulla barra di ricerca e digita") + len("digita sulla barra di ricerca e digita")
        to_type = txt[idx:].strip()
    elif current_language == "it":
        for prefix in ["scrivi ", "digita "]:
            if txt_lower.startswith(prefix):
                is_typing = True
                to_type = txt[len(prefix):].strip()
                break
    else:
        for prefix in ["type ", "write "]:
            if txt_lower.startswith(prefix):
                is_typing = True
                to_type = txt[len(prefix):].strip()
                break

    if is_typing and to_type:
        # Strip quotes if they were spoken or transcribed literally
        if (to_type.startswith('"') and to_type.endswith('"')) or (to_type.startswith("'") and to_type.endswith("'")):
            to_type = to_type[1:-1].strip()
            
        # Check if user requested a command to be typed by description
        to_type_lower = to_type.lower()
        if to_type_lower.startswith("il comando per") or to_type_lower.startswith("un comando per") or to_type_lower.startswith("il comando di"):
            speak("Sto elaborando il comando terminale..." if current_language == "it" else "Formulating terminal command...", current_language)
            try:
                import requests
                system_prompt = (
                    "Sei una utility di traduzione da linguaggio naturale a comando terminale per Kali Linux. "
                    "Fornisci come risposta SOLO ed ESCLUSIVAMENTE il comando shell richiesto, senza spiegazioni, senza markdown e senza commenti."
                )
                response = requests.post(
                    "http://localhost:11434/api/chat",
                    json={
                        "model": "dolphin-llama3:latest",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": f"Fornisci il comando per: {to_type}"}
                        ],
                        "options": {"temperature": 0.1, "num_predict": 40},
                        "stream": False
                    },
                    timeout=10
                )
                if response.status_code == 200:
                    cmd_result = response.json().get("message", {}).get("content", "").strip()
                    cmd_result = cmd_result.replace("`", "").replace("bash", "").replace("sh", "").strip()
                    log(f"Translated command description '{to_type}' to shell command: '{cmd_result}'")
                    to_type = cmd_result
            except Exception as e:
                log(f"Command translation failed: {e}")
            
        if pyautogui:
            if click_search_first:
                speak("Sto cercando la barra di ricerca..." if current_language == "it" else "Searching for the search bar...", current_language)
                found = False
                for s_term in ["Cerca", "Search", "Cerca o inserisci", "Search or type"]:
                    if click_on_text(s_term, double_click=False):
                        found = True
                        time.sleep(0.3)
                        break
                if not found:
                    log("Search bar text not found, typing at current cursor position.")
            
            pyautogui.write(to_type, interval=0.03)
            log(f"Typed: '{to_type}'")
            confirm = f"Scritto {to_type}" if current_language == "it" else f"Typed {to_type}"
            speak(confirm, current_language)
        else:
            speak("Libreria di digitazione non disponibile." if current_language == "it" else "Keyboard library not available.", current_language)
        return

    # Keyboard Control: Key Presses
    is_key = False
    key_to_press = None
    if current_language == "it":
        if "premi" in txt_lower or txt_lower in ["invio", "cancella", "spazio", "tab"]:
            is_key = True
            if "invio" in txt_lower: key_to_press = "enter"
            elif "cancella" in txt_lower or "backspace" in txt_lower: key_to_press = "backspace"
            elif "spazio" in txt_lower: key_to_press = "space"
            elif "tab" in txt_lower: key_to_press = "tab"
    else:
        if "press" in txt_lower or txt_lower in ["enter", "backspace", "space", "tab"]:
            is_key = True
            if "enter" in txt_lower: key_to_press = "enter"
            elif "backspace" in txt_lower: key_to_press = "backspace"
            elif "space" in txt_lower: key_to_press = "space"
            elif "tab" in txt_lower: key_to_press = "tab"

    if is_key and key_to_press:
        if pyautogui:
            pyautogui.press(key_to_press)
            log(f"Pressed key: {key_to_press}")
            speak("Fatto" if current_language == "it" else "Done", current_language)
        else:
            speak("Controllo tastiera non disponibile." if current_language == "it" else "Keyboard control not available.", current_language)
        return

    # Mouse Absolute Movement
    is_mouse_move = False
    coords = None
    if "mouse" in txt_lower and any(w in txt_lower for w in ["sposta", "muovi", "move"]):
        words = txt_lower.split()
        numbers = []
        for w in words:
            w_clean = "".join(c for c in w if c.isdigit())
            if w_clean:
                numbers.append(int(w_clean))
        if len(numbers) >= 2:
            is_mouse_move = True
            coords = (numbers[0], numbers[1])

    if is_mouse_move and coords:
        if pyautogui:
            pyautogui.moveTo(coords[0], coords[1], duration=0.4)
            log(f"Moved mouse absolute to: {coords}")
            confirm = f"Mouse spostato a {coords[0]} {coords[1]}" if current_language == "it" else f"Moved mouse to {coords[0]} {coords[1]}"
            speak(confirm, current_language)
        else:
            speak("Controllo mouse non disponibile." if current_language == "it" else "Mouse control not available.", current_language)
        return

    # Mouse Relative Movement
    is_mouse_rel = False
    dx, dy = 0, 0
    dist = 100
    
    words = txt_lower.split()
    numbers = [int("".join(c for c in w if c.isdigit())) for w in words if "".join(c for c in w if c.isdigit())]
    if numbers:
        dist = numbers[0]

    if "mouse" in txt_lower:
        if "destra" in txt_lower or "right" in txt_lower:
            is_mouse_rel = True
            dx = dist
        elif "sinistra" in txt_lower or "left" in txt_lower:
            is_mouse_rel = True
            dx = -dist
        elif "alto" in txt_lower or "sopra" in txt_lower or "up" in txt_lower:
            is_mouse_rel = True
            dy = -dist
        elif "sotto" in txt_lower or "basso" in txt_lower or "down" in txt_lower:
            is_mouse_rel = True
            dy = dist

    if is_mouse_rel:
        if pyautogui:
            pyautogui.moveRel(dx, dy, duration=0.3)
            log(f"Moved mouse relative: {dx}, {dy}")
            speak("Fatto" if current_language == "it" else "Done", current_language)
        else:
            speak("Controllo mouse non disponibile." if current_language == "it" else "Mouse control not available.", current_language)
        return

    # Mouse Clicks
    if "doppio click" in txt_lower or "double click" in txt_lower:
        if pyautogui:
            pyautogui.doubleClick()
            log("Double click executed")
            speak("Fatto" if current_language == "it" else "Done", current_language)
        else:
            speak("Mouse non disponibile." if current_language == "it" else "Mouse not available.", current_language)
        return
    elif "click destro" in txt_lower or "right click" in txt_lower or "tasto destro" in txt_lower:
        if pyautogui:
            pyautogui.rightClick()
            log("Right click executed")
            speak("Fatto" if current_language == "it" else "Done", current_language)
        else:
            speak("Mouse non disponibile." if current_language == "it" else "Mouse not available.", current_language)
        return
    elif "clicca" in txt_lower or "click" in txt_lower:
        if pyautogui:
            pyautogui.click()
            log("Left click executed")
            speak("Fatto" if current_language == "it" else "Done", current_language)
        else:
            speak("Mouse non disponibile." if current_language == "it" else "Mouse not available.", current_language)
        return

    # Scroll Up / Down
    if "scorri in alto" in txt_lower or "scroll up" in txt_lower or "vai su" in txt_lower:
        if pyautogui:
            pyautogui.scroll(12)
            log("Scrolled up")
            speak("Fatto" if current_language == "it" else "Done", current_language)
        return
    elif "scorri in basso" in txt_lower or "scroll down" in txt_lower or "vai giù" in txt_lower:
        if pyautogui:
            pyautogui.scroll(-12)
            log("Scrolled down")
            speak("Fatto" if current_language == "it" else "Done", current_language)
        return

    # Open Existing Screenshot Image
    if any(x in txt_lower for x in ["apri", "mostra", "vedi", "visualizza", "guarda"]) and "screenshot" in txt_lower:
        img_path = pathlib.Path.home() / "screenshot.png"
        if img_path.exists():
            try:
                subprocess.Popen(["xdg-open", str(img_path)])
                log(f"Opened existing screenshot image: {img_path}")
                msg = "Apro lo screenshot salvato." if current_language == "it" else "Opening saved screenshot."
                speak(msg, current_language)
            except Exception as e:
                log(f"Failed to open screenshot: {e}")
                speak("Impossibile aprire lo screenshot." if current_language == "it" else "Failed to open screenshot.", current_language)
        else:
            msg = "Nessuno screenshot trovato nella home." if current_language == "it" else "No screenshot found in home."
            speak(msg, current_language)
        return

    # Take New Screenshot
    if any(x in txt_lower for x in ["cattura schermo", "cattura lo schermo", "cattura dello schermo", "fai uno screenshot", "cattura uno screenshot", "scatta screenshot", "prendi uno screenshot"]) or (txt_lower == "screenshot"):
        img_path = pathlib.Path.home() / "screenshot.png"
        done = False
        if pyautogui:
            try:
                pyautogui.screenshot(str(img_path))
                done = True
            except Exception as e:
                log(f"PyAutoGUI screenshot failed: {e}")
        if not done and mss:
            try:
                with mss.mss() as sct:
                    sct.shot(output=str(img_path))
                done = True
            except Exception as e:
                log(f"mss screenshot failed: {e}")
        if done:
            log(f"Screenshot saved to {img_path}")
            msg = "Screenshot salvato nella tua home." if current_language == "it" else "Screenshot saved to your home."
        else:
            msg = "Impossibile scattare lo screenshot." if current_language == "it" else "Failed to capture screenshot."
        speak(msg, current_language)
        return

    # Browser Navigation / Web Search
    is_nav = False
    url_to_open = None
    
    if "vai su" in txt_lower or "vai a" in txt_lower or "apri il browser e vai su" in txt_lower:
        is_nav = True
        target_site = txt_lower
        for pref in ["apri il browser e vai su ", "apri il browser e vai a ", "vai su ", "vai a "]:
            if target_site.startswith(pref):
                target_site = target_site[len(pref):].strip()
                break
        target_site = target_site.replace(" ", "")
        if not "." in target_site:
            site_mappings = {
                "google": "https://www.google.com",
                "youtube": "https://www.youtube.com",
                "facebook": "https://www.facebook.com",
                "wikipedia": "https://it.wikipedia.org",
                "github": "https://github.com",
                "gmail": "https://mail.google.com"
            }
            url_to_open = site_mappings.get(target_site, f"https://www.google.com/search?q={target_site}")
        else:
            url_to_open = f"https://{target_site}" if not target_site.startswith("http") else target_site
            
    elif "cerca" in txt_lower and "su google" in txt_lower:
        is_nav = True
        query = txt_lower.replace("cerca", "").replace("su google", "").strip()
        url_to_open = f"https://www.google.com/search?q={query.replace(' ', '+')}"
        
    if is_nav and url_to_open:
        log(f"Navigating to URL: {url_to_open}")
        browser_path = shutil.which("brave-browser") or shutil.which("firefox") or shutil.which("google-chrome")
        if browser_path:
            subprocess.Popen([browser_path, url_to_open])
        else:
            subprocess.Popen(["xdg-open", url_to_open])
        msg = f"Navigo su {url_to_open}" if current_language == "it" else f"Navigating to {url_to_open}"
        speak(msg, current_language)
        return

    # Terminal command execution
    is_terminal_cmd = False
    cmd_to_run = ""
    
    if "apri il terminale ed esegui" in txt_lower or "esegui nel terminale" in txt_lower or "esegui in terminale" in txt_lower:
        is_terminal_cmd = True
        for pref in ["apri il terminale ed esegui ", "esegui nel terminale ", "esegui in terminale "]:
            if txt_lower.startswith(pref):
                cmd_to_run = txt[len(pref):].strip()
                break
    elif "aggiorna" in txt_lower and ("sistema" in txt_lower or "terminale" in txt_lower):
        is_terminal_cmd = True
        cmd_to_run = "sudo apt update && sudo apt upgrade"
        
    if is_terminal_cmd and cmd_to_run:
        if pyautogui:
            exec_path = shutil.which("xfce4-terminal") or shutil.which("xterm")
            if exec_path:
                subprocess.Popen([exec_path])
                time.sleep(1.0) # wait for window focus
                pyautogui.write(cmd_to_run, interval=0.03)
                pyautogui.press("enter")
                log(f"Executed terminal command: {cmd_to_run}")
                msg = f"Eseguo {cmd_to_run} nel terminale" if current_language == "it" else f"Running {cmd_to_run} in terminal"
                speak(msg, current_language)
            else:
                speak("Terminale non trovato." if current_language == "it" else "Terminal not found.", current_language)
        else:
            speak("Controllo tastiera non disponibile." if current_language == "it" else "Keyboard control not available.", current_language)
        return

    # Open App / Folder / File
    if "apri" in txt_lower or "open" in txt_lower:
        parts = txt_lower.split()
        verb = "apri" if "apri" in parts else "open"
        idx = parts.index(verb)
        target = " ".join(parts[idx + 1:]).strip()
        
        for art in ["il ", "lo ", "la ", "gli ", "le ", "i ", "the ", "a ", "an "]:
            if target.startswith(art):
                target = target[len(art):].strip()

        # Common shortcuts and applications
        app_mappings = {
            "browser": "firefox",
            "firefox": "firefox",
            "chrome": "google-chrome",
            "chromium": "chromium",
            "brave": "brave-browser",
            "terminale": "xfce4-terminal",
            "terminal": "xfce4-terminal",
            "console": "xfce4-terminal",
            "calcolatrice": "galculator",
            "calculator": "galculator",
            "editor": "mousepad",
            "blocco note": "mousepad",
            "notepad": "mousepad",
            "editor di testo": "mousepad",
            "text editor": "mousepad",
            "whatsapp": "zapzap",
            "zap zap": "zapzap",
            "zapzap": "zapzap",
            "vlc": "vlc",
            "wireshark": "wireshark"
        }
        app_name = target
        for key, val in app_mappings.items():
            if key in target.lower():
                app_name = val
                break
        
        # Intelligently locate and open subfolders (e.g. 'build') or base folder shortcuts
        if find_and_open_folder(target):
            return

        # Check executable binary
        bin_paths = {
            "firefox": "/usr/bin/firefox",
            "chrome": "/usr/bin/google-chrome",
            "chromium": "/usr/bin/chromium",
            "brave": "/usr/bin/brave-browser",
            "wireshark": "/usr/bin/wireshark",
            "burp": "/usr/bin/burpsuite",
            "maltego": "/usr/bin/maltego"
        }
        
        exec_path = bin_paths.get(app_name)
        if not exec_path:
            exec_path = shutil.which(app_name)
            
        if exec_path:
            try:
                subprocess.Popen([exec_path])
                log(f"Launched program: {app_name}")
                msg = f"Apro {app_name}" if current_language == "it" else f"Opening {app_name}"
                speak(msg, current_language)
            except Exception as e:
                log(f"Error launching program {app_name}: {e}")
                speak("Errore nell'avvio dell'applicazione" if current_language == "it" else "Error launching application", current_language)
            return

        # Check Flatpak
        if launch_flatpak(app_name):
            return

        # Check if it's a file
        file_found = find_file_in_folders(target)
        if file_found:
            try:
                subprocess.Popen(["xdg-open", str(file_found)])
                log(f"Opened file: {file_found}")
                msg = f"Apro il file {file_found.name}" if current_language == "it" else f"Opening file {file_found.name}"
                speak(msg, current_language)
            except Exception as e:
                log(f"Error opening file {file_found}: {e}")
                speak("Errore nell'apertura del file" if current_language == "it" else "Error opening file", current_language)
            return

        # Not found
        msg = f"Non ho trovato l'applicazione o il file {target}." if current_language == "it" else f"I couldn't find the application or file {target}."
        speak(msg, current_language)
        return

    # Close App
    if "chiudi" in txt_lower or "close" in txt_lower:
        parts = txt_lower.split()
        verb = "chiudi" if "chiudi" in parts else "close"
        idx = parts.index(verb)
        target = " ".join(parts[idx + 1:]).strip()
        
        for art in ["il ", "lo ", "la ", "gli ", "le ", "i ", "the "]:
            if target.startswith(art):
                target = target[len(art):].strip()
                
        # Check for "everything" / "all" / "tutto" / "tutte"
        if any(x in target.lower() for x in ["tutto", "tutte le finestre", "tutto quello", "everything", "all windows"]):
            if pyautogui:
                # Close the active window using Alt+F4
                pyautogui.hotkey('alt', 'f4')
                log("Closed active window via Alt+F4")
                speak("Chiudo la finestra attiva" if current_language == "it" else "Closing active window", current_language)
            return
                
        proc_mappings = {
            "browser": "firefox",
            "firefox": "firefox",
            "chrome": "chrome",
            "chromium": "chromium",
            "brave": "brave-browser",
            "terminale": "xfce4-terminal",
            "terminal": "xfce4-terminal",
            "calcolatrice": "galculator",
            "calculator": "galculator",
            "editor": "mousepad",
            "mousepad": "mousepad",
            "gedit": "gedit",
            "vlc": "vlc",
            "wireshark": "wireshark",
            "whatsapp": "zapzap",
            "zap zap": "zapzap",
            "zapzap": "zapzap"
        }
        
        proc_name = target
        # Sort proc keys by length descending to match longer specific proc patterns first
        sorted_proc_keys = sorted(proc_mappings.keys(), key=len, reverse=True)
        for key in sorted_proc_keys:
            if key in target.lower():
                proc_name = proc_mappings[key]
                break
        try:
            subprocess.Popen(["pkill", "-f", proc_name])
            log(f"Killed process pattern: {proc_name}")
            msg = f"Chiudo {target}" if current_language == "it" else f"Closing {target}"
            speak(msg, current_language)
        except Exception as e:
            log(f"Error killing process {target}: {e}")
            msg = f"Impossibile chiudere {target}" if current_language == "it" else f"Unable to close {target}"
            speak(msg, current_language)
        return

    # Conversational Chat Fallback (using Local Ollama dolphin-llama3/llama3 models)
    ollama_reply = query_ollama(txt)
    if ollama_reply:
        speak(ollama_reply, current_language)
        return

    # Ultimate fallback response
    msg = "Comando non riconosciuto." if current_language == "it" else "Command not recognized."
    speak(msg, current_language)

# Queue of audio chunks
audio_queue = queue.Queue()

def audio_callback(indata, frames, time_, status):
    if status:
        log(f"Audio callback status error: {status}")
    audio_queue.put(bytes(indata))

last_vosk_command_text = ""

def process_command_buffer(buffer: list):
    global current_language, last_vosk_command_text
    
    # 1. Combine buffer blocks into a single raw audio bytes object
    raw_audio = b"".join(buffer)
    
    # 2. Try online Google Speech Recognition first
    text = ""
    if sr:
        r = sr.Recognizer()
        audio_data = sr.AudioData(raw_audio, 16000, 2)
        lang_code = "it-IT" if current_language == "it" else "en-US"
        try:
            log(f"Sending audio to Google Speech API ({lang_code})...")
            text = r.recognize_google(audio_data, language=lang_code)
            log(f"Google transcription: '{text}'")
        except sr.UnknownValueError:
            log("Google API: speech unrecognized. Checking local Vosk fallback...")
        except sr.RequestError as e:
            log(f"Google API network error ({e}). Checking local Vosk fallback...")
        except Exception as e:
            log(f"Google API error ({e}). Checking local Vosk fallback...")
            
    # 3. Fallback to local Vosk if Google returned empty or unrecognized speech
    if not text.strip():
        if last_vosk_command_text.strip():
            text = last_vosk_command_text.strip()
            log(f"Using instant Vosk command fallback: '{text}'")
            last_vosk_command_text = ""
        else:
            try:
                temp_rec = KaldiRecognizer(model, 16000)
                temp_rec.AcceptWaveform(raw_audio)
                res = json.loads(temp_rec.Result())
                text = res.get("text", "")
                log(f"Vosk offline buffer transcription: '{text}'")
            except Exception as e:
                log(f"Vosk fallback extraction failed: {e}")
            
    # 4. Process transcribed command
    if text.strip():
        corrected_text = correct_transcription_errors(text)
        log(f"Corrected command text: '{corrected_text}'")
        handle_command(corrected_text)
    else:
        log("No speech recognized. Staying silent.")

# Load PulseAudio/Pipewire Acoustic Echo Cancellation (AEC) module with explicit physical mic binding
def load_echo_cancellation():
    """Configures PulseAudio / PipeWire Acoustic Echo Cancellation (AEC).
    
    Audio Driver & Subsystem Architecture:
    --------------------------------------
    1. PipeWire / PulseAudio: Linux sound server framework.
    2. ALSA (Advanced Linux Sound Architecture): Low-level kernel sound driver API.
    3. PortAudio: Audio I/O library for Python sounddevice.
    
    Dual-Monitor / HDMI Conflict Prevention:
    -----------------------------------------
    When dual HDMI monitors (or multi-display KDE Plasma desktop) are active, PipeWire/PulseAudio
    creates monitor streams (e.g. `hdmi-stereo.monitor` or `HDMI1__sink.monitor`). If echo-cancel
    binds to a monitor stream, it captures silence/speaker sound instead of voice input!
    
    This function explicitly locates real physical microphones, sets master input gain to 100%,
    and binds `module-echo-cancel` directly to the physical microphone source.
    """
    try:
        import subprocess, time
        
        # Scan system sources for physical microphones (excluding monitor streams and virtual AEC sources)
        sources_res = subprocess.run(["pactl", "list", "sources", "short"], capture_output=True, text=True)
        physical_mics = []
        for line in sources_res.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                src_name = parts[1]
                src_lower = src_name.lower()
                if ".monitor" not in src_lower and "echo-cancel" not in src_lower:
                    physical_mics.append(src_name)
                    
        # Select best physical mic candidate (prefer names with 'mic', 'input', 'analog', 'skl', 'sof')
        master_mic = None
        for m in physical_mics:
            m_lower = m.lower()
            if "mic" in m_lower or "input" in m_lower or "analog" in m_lower or "skl" in m_lower or "sof" in m_lower:
                master_mic = m
                break
        if not master_mic and physical_mics:
            master_mic = physical_mics[0]
            
        log(f"Audio Driver - Physical Microphones Detected: {physical_mics}")
        log(f"Audio Driver - Selected Master Physical Mic: {master_mic}")
        
        # Unmute all physical microphones and set gain to 100%
        for m in physical_mics:
            subprocess.run(["pactl", "set-source-mute", m, "no"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["pactl", "set-source-volume", m, "65536"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) # 65536 = 100%
            
        # If module-echo-cancel is active on wrong source, reload it
        res = subprocess.run(["pactl", "list", "modules"], capture_output=True, text=True)
        if "module-echo-cancel" in res.stdout:
            subprocess.run(["pactl", "unload-module", "module-echo-cancel"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.5)
            
        log("Loading PulseAudio module-echo-cancel for acoustic echo cancellation...")
        cmd = ["pactl", "load-module", "module-echo-cancel", "source_name=echo-cancel-source"]
        if master_mic:
            cmd.append(f"master_source={master_mic}")
        else:
            cmd.append("use_master_device=yes")
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)
            
        # Set echo-cancel source as default recording device and set 100% volume
        subprocess.run(["pactl", "set-default-source", "echo-cancel-source"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["pactl", "set-source-mute", "echo-cancel-source", "no"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["pactl", "set-source-volume", "echo-cancel-source", "65536"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("Echo-cancel source configured as default recording device with 100% volume.")
    except Exception as e:
        log(f"Failed to configure audio drivers / echo-cancellation: {e}")

# Main Daemon Runner
def run_assistant():
    global current_language, model, continuous_mode_until, last_vosk_command_text
    
    # Load echo cancellation to clean microphone audio of speaker output
    load_echo_cancellation()
    
    # 1. Check audio device
    device_index = find_working_stream()
    if device_index == -1:
        log("Could not find any working audio input device. Exiting.")
        sys.exit(1)
        
    # 2. Verify and load local Vosk model (auto-download if missing)
    if not MODEL_PATH.exists():
        log(f"Vosk local model not found at {MODEL_PATH}. Attempting automatic download...")
        try:
            import urllib.request, zipfile
            MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
            zip_tmp = "/tmp/vosk-model-small-it-0.22.zip"
            url = "https://alphacephei.com/vosk/models/vosk-model-small-it-0.22.zip"
            urllib.request.urlretrieve(url, zip_tmp)
            with zipfile.ZipFile(zip_tmp, 'r') as zip_ref:
                zip_ref.extractall(MODEL_PATH.parent)
            extracted_dir = MODEL_PATH.parent / "vosk-model-small-it-0.22"
            if extracted_dir.exists():
                extracted_dir.rename(MODEL_PATH)
            if os.path.exists(zip_tmp):
                os.remove(zip_tmp)
            log("Vosk model successfully downloaded and installed.")
        except Exception as e_dl:
            log(f"Failed to auto-download Vosk model: {e_dl}")
            sys.exit(1)
        
    try:
        model = Model(str(MODEL_PATH))
        recognizer = KaldiRecognizer(model, 16000)
        log("Vosk local model successfully loaded.")
    except Exception as e:
        log(f"Error loading Vosk model: {e}")
        sys.exit(1)

    # 3. Start Recording Stream
    if sd is None:
        log("sounddevice is not installed. Exiting.")
        sys.exit(1)
        
    try:
        stream = sd.RawInputStream(
            samplerate=16000,
            blocksize=2000,
            dtype="int16",
            channels=1,
            device=device_index,
            callback=audio_callback
        )
    except Exception as e:
        log(f"Error initiating RawInputStream: {e}")
        sys.exit(1)

    with stream:
        log("Kala Daemon fully active and listening.")
        speak("Kala avviata e pronta." if current_language == "it" else "Kala started and ready.", current_language, wait=True, enable_followup=False)
        
        global ambient_noise_level
        mode = "wake" # "wake" or "command"
        command_buffer = []
        last_speech_time = 0
        has_spoken = False
        recording_start_time = 0
        
        # Flush initial queue
        while not audio_queue.empty():
            audio_queue.get()
            
        last_hb = time.time()
        
        while True:
            try:
                # Read audio block
                try:
                    block = audio_queue.get(timeout=0.1)
                except queue.Empty:
                    # check if we are in command recording and have a timeout
                    if mode == "command":
                        curr_time = time.time()
                        # Fast VAD timeout: 2.5s if no speech started, or 0.7s silence after speech
                        if not has_spoken and (curr_time - recording_start_time > 2.5):
                            log("Command timeout: no voice detected.")
                            mode = "wake"
                            recognizer.Reset()
                            while not audio_queue.empty():
                                audio_queue.get()
                        elif has_spoken and (curr_time - last_speech_time > 0.7):
                            log("VAD: silence detected (0.7s), processing command immediately...")
                            buf_to_process = list(command_buffer)
                            threading.Thread(target=process_command_buffer, args=(buf_to_process,), daemon=True).start()
                            mode = "wake"
                            recognizer.Reset()
                            while not audio_queue.empty():
                                audio_queue.get()
                    
                    # Heartbeat
                    if time.time() - last_hb >= 30:
                        log("Heartbeat - Daemon listening")
                        last_hb = time.time()
                    continue
                
                curr_time = time.time()
                
                if mode == "wake":
                    # Auto-transition if continuous conversation window is active (6s after speaking) or subfolder choice
                    if time.time() < continuous_mode_until or awaiting_subfolder_choice:
                        is_speaking = (tts_process and tts_process.poll() is None)
                        if not is_speaking:
                            log("Continuous conversation mode active: automatically listening for follow-up reply...")
                            mode = "command"
                            command_buffer = []
                            has_spoken = False
                            recording_start_time = curr_time
                            last_speech_time = curr_time
                            while not audio_queue.empty():
                                audio_queue.get()
                            continue

                    is_speaking = (tts_process and tts_process.poll() is None)
                    
                    # Feed raw audio block directly to Vosk so zeroing out doesn't starve the speech recognizer
                    block_to_feed = block
                    
                    if is_speaking:
                        # Feed the audio block to let the recognizer compile partial speech
                        recognizer.AcceptWaveform(block_to_feed)
                        partial = json.loads(recognizer.PartialResult())
                        ptext = partial.get("partial", "").lower()
                        
                        # Check stop commands: stop speech, stay in wake mode
                        if any(x in ptext for x in ["stop", "galastop", "kalastop", "gala stop", "kala stop", "scala stop", "ferma", "zitt", "basta", "quiet", "shh", "silenzio"]):
                            log(f"Speech interrupted via partial stop command: '{ptext}'")
                            stop_speak()
                            continuous_mode_until = 0.0
                            recognizer.Reset()
                            while not audio_queue.empty():
                                audio_queue.get()
                            continue
                            
                        # Otherwise continue to avoid self-triggering from assistant's own voice
                        continue
                    
                    if recognizer.AcceptWaveform(block_to_feed):
                        result = json.loads(recognizer.Result())
                        text = result.get("text", "").strip()
                        if text:
                            log(f"🎙️ [Vosk Hearing (full)]: '{text}'")
                        words = text.split()
                        wake_idx = -1
                        for i, w in enumerate(words):
                            if is_wake_word(w, current_language):
                                wake_idx = i
                                break
                        if wake_idx != -1:
                            extracted_cmd = " ".join(words[wake_idx + 1:]).strip()
                            log(f"✨ WAKE WORD MATCHED in full text: '{text}'! (Extracted command: '{extracted_cmd}')")
                            mode = "command"
                            command_buffer = [block]
                            has_spoken = bool(extracted_cmd)
                            last_vosk_command_text = extracted_cmd
                            recording_start_time = time.time()
                            last_speech_time = time.time()
                    else:
                        partial = json.loads(recognizer.PartialResult())
                        ptext = partial.get("partial", "").strip()
                        if ptext:
                            log(f"🎙️ [Vosk Hearing (partial)]: '{ptext}'")
                        pwords = ptext.split()
                        wake_idx = -1
                        for i, w in enumerate(pwords):
                            if is_wake_word(w, current_language):
                                wake_idx = i
                                break
                        if wake_idx != -1:
                            extracted_cmd = " ".join(pwords[wake_idx + 1:]).strip()
                            log(f"✨ WAKE WORD MATCHED in partial text: '{ptext}'! (Extracted command: '{extracted_cmd}')")
                            mode = "command"
                            command_buffer = [block]
                            has_spoken = bool(extracted_cmd)
                            last_vosk_command_text = extracted_cmd
                            recording_start_time = time.time()
                            last_speech_time = time.time()
                                
                elif mode == "command":
                    is_speaking = (tts_process and tts_process.poll() is None)
                    if is_speaking:
                        # Flush audio blocks while assistant is speaking so assistant's voice is never recorded as a command
                        while not audio_queue.empty():
                            audio_queue.get()
                        continue

                    command_buffer.append(block)
                    amp = get_amplitude(block)
                    # Adjusted VAD thresholds for better sensitivity
                    speech_threshold = max(150.0, ambient_noise_level + 30.0)
                    if amp > speech_threshold:
                        if not has_spoken and (curr_time - recording_start_time > 0.15):
                            has_spoken = True
                            log("VAD: speech started")
                        if has_spoken:
                            last_speech_time = curr_time

                    # VAD: shorter silence detection (0.5s) and lower max duration (4s)
                    if not has_spoken and (curr_time - recording_start_time > 2.5):
                        log("Command timeout: no voice detected.")
                        mode = "wake"
                        recognizer.Reset()
                        while not audio_queue.empty():
                            audio_queue.get()
                    elif has_spoken and (curr_time - last_speech_time > 0.5 or curr_time - recording_start_time > 4.0):
                        log("VAD: silence or max duration reached, processing command...")
                        buf_to_process = list(command_buffer)
                        threading.Thread(target=process_command_buffer, args=(buf_to_process,), daemon=True).start()
                        mode = "wake"
                        recognizer.Reset()
                        while not audio_queue.empty():
                            audio_queue.get()
                            
            except Exception as e:
                log(f"Error in main daemon loop: {e}")
                time.sleep(1)

if __name__ == "__main__":
    try:
        run_assistant()
    except KeyboardInterrupt:
        log("Kala Daemon terminated by user.")
        sys.exit(0)
