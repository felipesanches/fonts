#!/bin/bash
# Monitor build_registry.json for status changes and announce via piper TTS
# Usage: ./announce_results.sh [poll_interval_seconds]

PIPER=/home/fsanches/.local/share/piper/piper
MODEL=/home/fsanches/.local/share/piper/en_US-lessac-medium.onnx
REGISTRY=/mnt/shared/google/fonts/build_tools/build_registry.json
INTERVAL=${1:-10}
STATE_FILE=/tmp/announce_results_state.json

say() {
    echo "$1" | $PIPER -m $MODEL --output_raw 2>/dev/null | aplay -r 22050 -f S16_LE -t raw -c 1 2>/dev/null
}

# Capture initial state
python3 -c "
import json
reg = json.load(open('$REGISTRY'))
state = {}
for name, entry in reg['families'].items():
    if isinstance(entry, dict):
        state[name] = entry.get('reproducible_build', 'unknown')
json.dump(state, open('$STATE_FILE', 'w'))
print(f'Monitoring {len(state)} families. Poll interval: ${INTERVAL}s')
"

while true; do
    sleep $INTERVAL

    result=$(python3 -c "
import json
try:
    old = json.load(open('$STATE_FILE'))
except:
    old = {}
reg = json.load(open('$REGISTRY'))
new_state = {}
new_builds = []
new_identical = []
total_building = 0
total_identical = 0
for name, entry in reg['families'].items():
    if isinstance(entry, dict):
        status = entry.get('reproducible_build', 'unknown')
        new_state[name] = status
        if status not in ('build-failure', None, 'unknown'):
            total_building += 1
        if status == 'yes':
            total_identical += 1
        old_status = old.get(name, 'unknown')
        if old_status == 'build-failure' and status not in ('build-failure', None, 'unknown'):
            new_builds.append(name)
            if status == 'yes':
                new_identical.append(name)
json.dump(new_state, open('$STATE_FILE', 'w'))
if new_builds:
    pct_building = total_building / 1266 * 100
    pct_identical = total_identical / 1266 * 100
    msg = f'ANNOUNCE:{len(new_builds)} new families building correct-lee! {pct_building:.1f} percent building.'
    if new_identical:
        msg += f' {len(new_identical)} new byte-identical! {pct_identical:.1f} percent identical.'
    print(msg)
    for f in new_builds:
        print(f'  {f}: {new_state[f]}')
" 2>/dev/null)

    if echo "$result" | grep -q "^ANNOUNCE:"; then
        msg=$(echo "$result" | grep "^ANNOUNCE:" | cut -d: -f2-)
        say "$msg"
    fi

    if [ -n "$result" ]; then
        echo "[$(date '+%H:%M:%S')] $result"
    fi
done
