#!/usr/bin/env python3
"""nanoGPT-style data prep for the kbots corpus.

Reads a corpus.txt (from scripts/export_training_data.py) and writes train.bin /
val.bin (uint16 token ids) that nanoGPT's train.py consumes. Default tokenizer is
GPT-2 BPE (tiktoken); pass --char for a character-level toy (also writes meta.pkl).

Copy the output dir into a nanoGPT clone's data/ (e.g. nanoGPT/data/kbots/) and
train. Needs numpy, plus tiktoken for BPE — install only when training. See
docs/TRAINING.md.
"""
import argparse
import pickle
from pathlib import Path


def _encode_char(data: str, train: str, val: str, out: Path):
    chars = sorted(set(data))
    stoi = {c: i for i, c in enumerate(chars)}
    meta = {"vocab_size": len(chars), "stoi": stoi, "itos": {i: c for c, i in stoi.items()}}
    with open(out / "meta.pkl", "wb") as f:
        pickle.dump(meta, f)
    return [stoi[c] for c in train], [stoi[c] for c in val]


def _encode_bpe(train: str, val: str):
    try:
        import tiktoken
    except ImportError:
        raise SystemExit("Install tiktoken for BPE tokenizing: pip install tiktoken (or use --char)") from None
    enc = tiktoken.get_encoding("gpt2")
    return enc.encode_ordinary(train), enc.encode_ordinary(val)


def main():
    ap = argparse.ArgumentParser(description="Prepare the kbots corpus for nanoGPT")
    ap.add_argument("--corpus", default="data/training/corpus.txt")
    ap.add_argument("--out", default="data/training/nanogpt")
    ap.add_argument("--char", action="store_true", help="char-level (else GPT-2 BPE)")
    ap.add_argument("--val-frac", type=float, default=0.1)
    args = ap.parse_args()

    try:
        import numpy as np
    except ImportError:
        raise SystemExit("Install numpy first: pip install numpy") from None

    data = Path(args.corpus).read_text(errors="replace")
    if not data.strip():
        raise SystemExit(f"{args.corpus} is empty — export some turns first "
                         "(scripts/export_training_data.py).")
    split = int(len(data) * (1 - args.val_frac))
    train_data, val_data = data[:split], data[split:]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if args.char:
        train_ids, val_ids = _encode_char(data, train_data, val_data, out)
    else:
        train_ids, val_ids = _encode_bpe(train_data, val_data)

    np.array(train_ids, dtype=np.uint16).tofile(out / "train.bin")
    np.array(val_ids, dtype=np.uint16).tofile(out / "val.bin")
    print(f"train {len(train_ids):,} tokens, val {len(val_ids):,} tokens → {out}")
    print("Copy this dir into nanoGPT/data/kbots/ and train (see docs/TRAINING.md).")


if __name__ == "__main__":
    main()
