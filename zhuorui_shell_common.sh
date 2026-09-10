#!/usr/bin/env bash

# Shared helpers for the Linux launch and control scripts.

find_python() {
    local project_root=$1
    local candidate
    for candidate in \
        "$project_root/.venv/bin/python" \
        "$project_root/.venv/bin/python3" \
        "${HOME:-}/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python" \
        "${HOME:-}/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
    do
        if [[ -n $candidate && -x $candidate ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    if command -v python3 >/dev/null 2>&1; then
        command -v python3
        return 0
    fi
    if command -v python >/dev/null 2>&1; then
        command -v python
        return 0
    fi
    printf '%s\n' "Could not find Python 3. Install it or create $project_root/.venv." >&2
    return 1
}

valid_pid() {
    [[ ${1:-} =~ ^[0-9]+$ ]] && (( 1 <= 10#$1 ))
}

process_is_running() {
    local pid=$1
    kill -0 "$pid" 2>/dev/null
}

process_command_contains() {
    local pid=$1
    local expected=$2
    [[ -r /proc/$pid/cmdline ]] || return 1
    tr '\0' '\n' < "/proc/$pid/cmdline" | grep -Fqx -- "$expected"
}

tracked_process_matches() {
    local pid=$1
    local metadata_path=$2
    local expected_script=$3
    local python_exe=$4

    process_is_running "$pid" || return 1
    process_command_contains "$pid" "$expected_script" || return 1
    [[ -r $metadata_path && -r /proc/$pid/stat && -r /proc/stat ]] || return 1

    "$python_exe" - "$pid" "$metadata_path" <<'PY'
import datetime
import json
import os
import sys

pid = int(sys.argv[1])
metadata_path = sys.argv[2]
try:
    with open(metadata_path, "r", encoding="utf-8-sig") as handle:
        metadata = json.load(handle)
    if int(metadata.get("pid")) != pid:
        raise ValueError("PID mismatch")
    recorded = datetime.datetime.fromisoformat(
        str(metadata["started_utc"]).replace("Z", "+00:00")
    ).timestamp()
    with open(f"/proc/{pid}/stat", "r", encoding="ascii") as handle:
        process_stat = handle.read()
    fields_after_name = process_stat[process_stat.rfind(")") + 2 :].split()
    start_ticks = int(fields_after_name[19])
    with open("/proc/stat", "r", encoding="ascii") as handle:
        boot_epoch = next(
            int(line.split()[1]) for line in handle if line.startswith("btime ")
        )
    actual = boot_epoch + start_ticks / os.sysconf("SC_CLK_TCK")
except (IndexError, KeyError, OSError, StopIteration, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
raise SystemExit(0 if abs(recorded - actual) <= 30 else 1)
PY
}

stop_process_group() {
    local pid=$1
    local deadline
    local pgid
    pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')

    if [[ $pgid == "$pid" ]]; then
        kill -TERM -- "-$pid" 2>/dev/null || return 1
    else
        kill -TERM "$pid" 2>/dev/null || return 1
    fi

    deadline=$((SECONDS + 10))
    while process_is_running "$pid" && (( SECONDS < deadline )); do
        sleep 0.2
    done
    if process_is_running "$pid"; then
        if [[ $pgid == "$pid" ]]; then
            kill -KILL -- "-$pid" 2>/dev/null || return 1
        else
            kill -KILL "$pid" 2>/dev/null || return 1
        fi
    fi
}
