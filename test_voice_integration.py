# test_voice_integration.py — run with: python test_voice_integration.py
import sys
import time
import numpy as np

print("=== ATLAS Voice Integration Test ===\n")

# Test 1: voice.py imports and Whisper loads
print("--- Test 1: voice.py module load ---")
try:
    import voice
    voice.warmup_model()
    if voice._whisper_model:
        print("Whisper loaded: OK")
    else:
        print("ERROR: Whisper model is None — check openai-whisper install")
        sys.exit(1)
except Exception as e:
    print(f"ERROR importing voice.py: {e}")
    sys.exit(1)

# Test 2: transcribe_from_array with synthetic audio
print("\n--- Test 2: transcribe_from_array with silence ---")
try:
    silence = np.zeros(16000 * 2, dtype=np.int16)
    result = voice.transcribe_from_array(silence)
    print(f"Silence result: '{result}' (expected empty or near-empty)")
    print("transcribe_from_array: OK")
except Exception as e:
    print(f"ERROR: {e}")

# Test 3: wake_word.py module load
print("\n--- Test 3: wake_word.py module load ---")
try:
    import wake_word
    print(f"openWakeWord available: {wake_word.is_available()}")
    if not wake_word.is_available():
        print("WARNING: pip install openwakeword")
except Exception as e:
    print(f"ERROR importing wake_word.py: {e}")

# Test 4: speak + stop_speaking (no audio device needed for this)
print("\n--- Test 4: speak() and stop_speaking() ---")
try:
    voice.speak("testing")
    time.sleep(0.3)
    voice.stop_speaking()
    print("speak/stop: OK (check for brief audio or silent pass)")
except Exception as e:
    print(f"WARNING: {e} (edge-tts may not be installed)")

# Test 5: PTT listener starts and stops cleanly
print("\n--- Test 5: PTT start/stop (no key press needed) ---")
try:
    voice.start_ptt_listener()
    time.sleep(0.5)
    voice.stop_ptt_listener()
    time.sleep(0.3)
    print("PTT start/stop: OK")
except Exception as e:
    print(f"ERROR: {e}")

# Test 6: Wake word listener starts and stops cleanly
print("\n--- Test 6: Wake word start/stop ---")
try:
    if wake_word.is_available():
        wake_word.start_wake_word_listener()
        time.sleep(1.0)
        print(f"Listening: {wake_word.is_listening()}")
        wake_word.stop_wake_word_listener()
        time.sleep(0.5)
        print(f"Stopped: {not wake_word.is_listening()}")
        print("Wake word start/stop: OK")
    else:
        print("SKIPPED: openWakeWord not available")
except Exception as e:
    print(f"ERROR: {e}")

print("\n=== Integration test complete ===")
print("If all tests passed: run python test_audio.py for live hardware test")
