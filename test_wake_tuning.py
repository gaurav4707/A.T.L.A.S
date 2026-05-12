"""Wake-word tuning helper for ATLAS using live score output and threshold sweep."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
from openwakeword.model import Model

SAMPLE_RATE = 16000
CHUNK = 1280


def parse_args() -> argparse.Namespace:
    """Parse CLI args for wake-word tuning helper."""
    parser = argparse.ArgumentParser(description="ATLAS wake-word tuning helper")
    parser.add_argument("--seconds", type=int, default=15, help="Capture window in seconds")
    parser.add_argument("--model", type=str, default="hey_atlas", help="Wake phrase model name")
    parser.add_argument("--print-floor", type=float, default=0.05, help="Print live score when above this value")
    parser.add_argument("--apply", action="store_true", help="Write suggested threshold to config.json")
    parser.add_argument("--config", type=str, default="config.json", help="Config file path to update")
    return parser.parse_args()


def _apply_threshold_to_config(config_path: str, threshold: float) -> None:
    """Write wake_word_threshold into the target JSON config file."""
    path = Path(config_path)
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    data["wake_word_threshold"] = round(float(threshold), 2)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def main() -> None:
    """Capture wake-word scores and suggest threshold settings."""
    args = parse_args()

    thresholds = [0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.5, 0.6]
    backend_model = "hey_jarvis" if args.model == "hey_atlas" else args.model
    model = Model(wakeword_models=[backend_model], inference_framework="onnx")

    scores: list[float] = []
    print("=== ATLAS Wake Tuning ===")
    print(f"Model: {args.model}")
    print(f"Duration: {args.seconds}s")
    print("Say 'hey atlas' naturally 6-10 times during the capture window.")
    print()

    start = time.time()
    chunks = max(1, int(SAMPLE_RATE * args.seconds / CHUNK))

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=CHUNK) as stream:
        for index in range(chunks):
            chunk, _ = stream.read(CHUNK)
            audio_np = chunk.reshape(-1).astype(np.int16, copy=False)

            pred = model.predict(audio_np)
            score = float(max(pred.values())) if pred else 0.0
            scores.append(score)

            if score >= args.print_floor:
                elapsed = time.time() - start
                print(f"t={elapsed:5.2f}s score={score:.3f}")

            if index % 25 == 0 and index > 0:
                elapsed = time.time() - start
                print(f"...capturing ({elapsed:4.1f}s)")

    if not scores:
        print("No scores captured.")
        return

    peak = max(scores)
    mean = float(np.mean(scores))
    p95 = float(np.percentile(scores, 95))

    print("\n--- Summary ---")
    print(f"Peak score: {peak:.3f}")
    print(f"Mean score: {mean:.3f}")
    print(f"95th pct:  {p95:.3f}")

    print("\n--- Threshold Sweep ---")
    for threshold in thresholds:
        hits = sum(1 for value in scores if value >= threshold)
        print(f"threshold={threshold:0.2f} hits={hits}")

    suggested = min(0.8, max(0.2, p95 + 0.08))
    print("\nSuggested wake_word_threshold:", f"{suggested:.2f}")

    if args.apply:
        try:
            _apply_threshold_to_config(args.config, suggested)
            print(f"Applied wake_word_threshold={suggested:.2f} to {args.config}")
        except Exception as exc:
            print(f"Failed to apply threshold to {args.config}: {exc}")
    else:
        print("Run with --apply to write this value into config.json automatically.")

    print("Retest with atlas --status + live command runs.")


if __name__ == "__main__":
    main()
