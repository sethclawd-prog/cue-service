#!/bin/zsh
# Start the Cue service on 127.0.0.1:8731 (exposed to the tailnet as https://seth-cosmo-studio.tail2f894f.ts.net:8445).
# Needs ANTHROPIC_API_KEY in the environment (or ~/.anthropic_key).
cd "$(dirname "$0")"
[ -z "$ANTHROPIC_API_KEY" ] && [ -f ~/.anthropic_key ] && export ANTHROPIC_API_KEY=$(cat ~/.anthropic_key)
# Email handed to free-download gates by the Mac-side fetch (gates.py), unless the app sends one.
export CUE_GATE_EMAIL="${CUE_GATE_EMAIL:-}"
exec .venv/bin/python service.py "${1:-8731}"
