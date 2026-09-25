#!/bin/zsh
# Nightly crate run: find new mixes for the taste in ~/Mixtapes/crate/taste.json, fetch what is offered for download, push to the phone if it is reachable.
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH" CUE_GATE_EMAIL="${CUE_GATE_EMAIL:-$(cat ~/.cue_gate_email 2>/dev/null)}"
.venv/bin/python crate.py discover --terms 25
.venv/bin/python crate.py fetch --max 30
