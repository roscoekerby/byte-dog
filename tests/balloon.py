"""Real-user thrash simulation: inflate RAM to a target percent and hold.

Run ByteDog first, then:
    python tests/balloon.py --target 76          # trigger WARN alert
    python tests/balloon.py --target 86          # trigger auto-SUSPEND (balloon freezes)
    python tests/balloon.py --target 93          # trigger auto-KILL (balloon dies = success)

The balloon becomes the top memory hog, so ByteDog's escalation acts on it,
not on your real apps. Ctrl+C releases everything instantly.

Notes for the 86% (suspend) test:
- Once suspended, the balloon cannot receive Ctrl+C. Use ByteDog's "Resume All"
  button first, then Ctrl+C.
- Suspension keeps the memory held, so if you hold above 85% for another 30s
  the guardian will suspend the NEXT biggest hog (likely a Chrome tab). Keep
  --hold short or resume promptly.
"""
import argparse
import time

import psutil

CHUNK_MB = 512
SAFETY_CEILING_PCT = 95.0


def inflate(target_pct: float, hold_s: float) -> None:
    chunks = []
    print(f"Inflating to {target_pct:.0f}% RAM (chunk={CHUNK_MB} MB). Ctrl+C to release.")
    try:
        while True:
            pct = psutil.virtual_memory().percent
            if pct >= min(target_pct, SAFETY_CEILING_PCT):
                break
            chunks.append(bytearray(CHUNK_MB * 1024 * 1024))  # touch pages
            print(f"  RAM {pct:5.1f}%  (held: {len(chunks) * CHUNK_MB / 1024:.1f} GB)", flush=True)
        print(f"Target reached ({psutil.virtual_memory().percent:.1f}%). "
              f"Holding {hold_s:.0f}s — watch ByteDog react.")
        time.sleep(hold_s)
    except KeyboardInterrupt:
        pass
    finally:
        chunks.clear()
        print(f"Released. RAM now {psutil.virtual_memory().percent:.1f}%")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=float, default=76.0,
                        help='RAM percent to inflate to (default 76 = WARN tier)')
    parser.add_argument('--hold', type=float, default=60.0,
                        help='seconds to hold at target (default 60)')
    args = parser.parse_args()
    inflate(args.target, args.hold)
