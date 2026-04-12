#!/usr/bin/env python3
"""
Self-test: Verify Porcupine → OpenWakeWord migration.
Tests config, imports, and basic flow.
"""

import sys
import json
from pathlib import Path

# Test 1: Config keys
print("[1/5] Checking config.json keys...")
config_path = Path("config.json")
with open(config_path) as f:
    config = json.load(f)

required_keys = ["wake_word_enabled", "wake_word_threshold", "wake_word_model"]
missing = [k for k in required_keys if k not in config]
old_keys = ["porcupine_key", "wake_word"]

found_old = [k for k in old_keys if k in config]
if missing or found_old:
    print(f"  ✗ FAIL: Missing {missing}, Found old keys {found_old}")
    sys.exit(1)

print(f"  ✓ PASS: All new keys present, old keys removed")
print(f"    - wake_word_enabled: {config.get('wake_word_enabled')}")
print(f"    - wake_word_threshold: {config.get('wake_word_threshold')}")
print(f"    - wake_word_model: {config.get('wake_word_model')}")

# Test 2: Settings defaults
print("\n[2/5] Checking settings.py defaults...")
try:
    import settings
    defaults = settings.DEFAULT_CONFIG
    
    for k in required_keys:
        if k not in defaults:
            print(f"  ✗ FAIL: Missing {k} in DEFAULT_CONFIG")
            sys.exit(1)
    
    for k in old_keys:
        if k in defaults:
            print(f"  ✗ FAIL: Old key {k} still in DEFAULT_CONFIG")
            sys.exit(1)
    
    print(f"  ✓ PASS: Settings defaults correct")
except Exception as e:
    print(f"  ✗ FAIL: {e}")
    sys.exit(1)

# Test 3: OpenWakeWord imports
print("\n[3/5] Checking OpenWakeWord imports...")
try:
    from openwakeword.model import Model
    import numpy as np
    import sounddevice
    print(f"  ✓ PASS: OpenWakeWord and dependencies importable")
except Exception as e:
    print(f"  ✗ FAIL: {e}")
    sys.exit(1)

# Test 4: wake_word.py exports
print("\n[4/5] Checking wake_word.py exports...")
try:
    import wake_word
    
    exports = [
        ("start_wake_word_listener", callable),
        ("stop_wake_word_listener", callable),
        ("is_listening", callable),
    ]
    
    for name, check in exports:
        attr = getattr(wake_word, name, None)
        if attr is None or not check(attr):
            print(f"  ✗ FAIL: Missing or invalid {name}")
            sys.exit(1)
    
    print(f"  ✓ PASS: All exports present and callable")
except Exception as e:
    print(f"  ✗ FAIL: {e}")
    sys.exit(1)

# Test 5: No pvporcupine references
print("\n[5/5] Checking for Porcupine references...")
try:
    with open("wake_word.py") as f:
        code = f.read()
    
    if "pvporcupine" in code or "_PORCUPINE" in code:
        print(f"  ✗ FAIL: Still has pvporcupine references")
        sys.exit(1)
    
    if "_OWW_MODEL" not in code:
        print(f"  ✗ FAIL: No _OWW_MODEL found")
        sys.exit(1)
    
    if "from openwakeword.model import Model" not in code:
        print(f"  ✗ FAIL: OpenWakeWord import not found")
        sys.exit(1)
    
    print(f"  ✓ PASS: Porcupine removed, OpenWakeWord integrated")
except Exception as e:
    print(f"  ✗ FAIL: {e}")
    sys.exit(1)

print("\n" + "="*60)
print("✓ ALL TESTS PASSED - Porcupine → OpenWakeWord migration successful!")
print("="*60)
print("\nNext steps:")
print("  1. Set wake_word_enabled: true in config.json")
print("  2. Run: atlas")
print("  3. Listener will auto-start with 'hey_atlas' phrase (mapped to backend model)")
print("  4. No API key required!")
