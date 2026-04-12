# test_audio.py — run with: python test_audio.py
import sounddevice as sd
import numpy as np
import whisper
import time

SAMPLE_RATE = 16000
CHUNK = 1280  # 80ms
RECORD_SECONDS = 4

print("=== ATLAS Audio Hardware Test ===\n")

# Test 1: List devices
print("--- Available audio devices ---")
print(sd.query_devices())
print()

# Test 2: Check default input
try:
    default_input = sd.query_devices(kind='input')
    print(f"Default input device: {default_input['name']}")
    print(f"Max input channels: {default_input['max_input_channels']}")
    print()
except Exception as e:
    print(f"ERROR: No default input device found: {e}")
    print("Fix: Set your microphone as the default recording device in Windows Sound settings")
    exit(1)

# Test 3: Raw mic capture — energy check
print(f"--- Recording {RECORD_SECONDS}s of audio (speak now) ---")
try:
    recording = sd.rec(
        int(RECORD_SECONDS * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype='int16'
    )
    sd.wait()
    energy = np.abs(recording).mean()
    peak = np.abs(recording).max()
    print(f"Mean energy: {energy:.1f}  (expected >100 if mic is working)")
    print(f"Peak value:  {peak}  (expected >500 if you spoke)")
    if energy < 50:
        print("WARNING: Very low energy. Check: mic plugged in? Not muted? Correct device selected?")
    else:
        print("Mic capture: OK")
    print()
except Exception as e:
    print(f"ERROR capturing audio: {e}")
    exit(1)

# Test 4: Whisper transcription of what we just recorded
print("--- Whisper transcription test ---")
try:
    model = whisper.load_model("tiny")
    audio_float = recording.flatten().astype(np.float32) / 32768.0
    result = model.transcribe(audio_float, language='en', fp16=False)
    text = result.get('text', '').strip()
    print(f"Transcribed: '{text}'")
    if text:
        print("Whisper: OK")
    else:
        print("WARNING: Empty transcription. Was anything spoken during recording?")
    print()
except Exception as e:
    print(f"ERROR in Whisper: {e}")
    print("Fix: pip install openai-whisper")

# Test 5: openWakeWord detection
print("--- openWakeWord detection test ---")
print("Say 'hey atlas' 3 times in the next 10 seconds...")
try:
    from openwakeword.model import Model
    oww = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
    detections = 0
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                        dtype='int16', blocksize=CHUNK) as stream:
        for i in range(int(SAMPLE_RATE * 10 / CHUNK)):
            chunk, _ = stream.read(CHUNK)
            audio_np = np.frombuffer(chunk, dtype=np.int16)
            pred = oww.predict(audio_np)
            for name, score in pred.items():
                if score > 0.1:
                    print(f"  Score: {score:.3f} {'<-- DETECTED' if score > 0.35 else ''}")
                if score > 0.35:
                    detections += 1
    print(f"Total detections (threshold 0.35): {detections}")
    if detections == 0:
        print("WARNING: No detections. Say 'hey atlas' clearly.")
    else:
        print("openWakeWord: OK")
except Exception as e:
    print(f"ERROR in openWakeWord: {e}")

print("\n=== Test complete ===")
