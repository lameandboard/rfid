#!/bin/bash
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <https://www.gnu.org/licenses/>.
#
# Copyright (c) 2026 lameandboard

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_EXTRAS="${REPO_DIR}/extras"
REPO_CFG="${REPO_DIR}/config"

KLIPPER_DIR="${KLIPPER_DIR:-${HOME}/klipper}"
AFC_DIR="${AFC_DIR:-${HOME}/AFC-Klipper-Add-On}"
# Happy Hare may be installed as Happy-Hare or Happy_Hare; prefer Happy-Hare
if [[ -z "${HH_DIR:-}" ]]; then
    if [[ -d "${HOME}/Happy-Hare" ]]; then
        HH_DIR="${HOME}/Happy-Hare"
    else
        HH_DIR="${HOME}/Happy_Hare"
    fi
fi
CONFIG_DIR="${CONFIG_DIR:-${HOME}/printer_data/config}"

KLIPPER_EXTRAS="${KLIPPER_DIR}/klippy/extras"

LIVE_RFID="${KLIPPER_EXTRAS}/rfid.py"
LIVE_MFRC="${KLIPPER_EXTRAS}/mfrc522.py"
LIVE_PN532="${KLIPPER_EXTRAS}/pn532.py"
LIVE_PARSER="${KLIPPER_EXTRAS}/rfid_tag_parser.py"
LIVE_LANE="${KLIPPER_EXTRAS}/AFC_lane.py"

UPSTREAM_LANE="${AFC_DIR}/extras/AFC_lane.py"
LOCAL_LANE="${REPO_EXTRAS}/AFC_lane.py"

MOONRAKER_CONF="${MOONRAKER_CONF:-${CONFIG_DIR}/moonraker.conf}"
HOOK_LOG="${HOME}/printer_data/logs/rfid_hook.log"
LIVE_RFID_CFG_DIR="${CONFIG_DIR}/rfid"
PRINTER_CFG="${CONFIG_DIR}/printer.cfg"

# Moonraker API base URL for restart calls
MOONRAKER_URL="${MOONRAKER_URL:-http://localhost:7125}"

TS="$(date +%Y%m%d_%H%M%S)"

# Marker embedded in every git hook written by this installer.
# Uninstall uses it to confirm ownership before removing a hook.
HOOK_MARKER="# RFID installer hook"

NO_RESTART=0
NO_HOOKS=0
NO_AFC=0
UPDATE=0
FORCE=0
UNINSTALL=0
PURGE_REPO=0
BRANCH=""

info()  { printf '[INFO] %s\n' "$*"; }
warn()  { printf '[WARN] %s\n' "$*" >&2; }
fail()  { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

usage() {
cat <<'EOF'
Usage: bash install.sh [options]

Options:
  --klipper-dir PATH      Klipper repo path (default: ~/klipper)
  --afc-dir PATH          AFC repo path (default: ~/AFC-Klipper-Add-On)
  --hh-dir PATH           Happy Hare repo path (default: ~/Happy-Hare, falling back to ~/Happy_Hare)
  --config-dir PATH       printer_data/config path (default: ~/printer_data/config)
  --moonraker-conf PATH   moonraker.conf path
  --moonraker-url URL     Moonraker base URL (default: http://localhost:7125)
  --update                git pull RFID repo, then rerun install
  --force                 re-clone RFID repo in place, then rerun install
  -b, --branch BRANCH     RFID repo branch to install from (default: current/main)
  --no-restart            do not restart klipper/moonraker
  --no-hooks              do not install AFC repo git hooks
  --no-afc                skip all AFC integration steps
  --uninstall             reverse what install.sh did (preserves logs and repo dir)
  --purge-repo            used with --uninstall: also delete the RFID repo dir after uninstall
  -h, --help              show help
EOF
}

current_branch() {
    git -C "${REPO_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || printf 'main'
}

backup_file() {
    local f="$1"
    if [[ -e "$f" || -L "$f" ]]; then
        local bak="${f}.${TS}.bak"
        cp -aP "$f" "$bak"
        info "Backed up: $f -> $bak"
    fi
}

remove_path_if_exists() {
    local p="$1"
    [[ -n "$p" ]] || return 0
    if [[ -L "$p" || -e "$p" ]]; then
        backup_file "$p"
        rm -rf "$p"
    fi
}

ensure_parent_dir() {
    mkdir -p "$(dirname "$1")"
}

force_symlink() {
    local src="$1"
    local dst="$2"
    ensure_parent_dir "$dst"
    remove_path_if_exists "$dst"
    ln -s "$src" "$dst"
    info "Symlinked: $dst -> $src"
}

restart_via_moonraker() {
    if ! command -v curl >/dev/null 2>&1; then
        warn "curl not found; cannot restart services via Moonraker API"
        return 1
    fi

    # 1. Restart Klipper service (not firmware restart)
    info "Restarting Klipper service..."
    curl -fsS --connect-timeout 5 --max-time 10 \
        -X POST "${MOONRAKER_URL}/machine/services/restart?service=klipper" \
        >/dev/null 2>&1 || { warn "Klipper service restart request failed"; return 1; }

    # 2. Wait for Klipper to come back online (poll /printer/info)
    info "Waiting for Klipper to come back online..."
    local attempts=0
    local max_attempts=30
    until curl -fsS --connect-timeout 3 --max-time 5 \
            "${MOONRAKER_URL}/printer/info" >/dev/null 2>&1; do
        attempts=$(( attempts + 1 ))
        if [[ "${attempts}" -ge "${max_attempts}" ]]; then
            warn "Klipper did not come back online after ${max_attempts}s; continuing anyway"
            break
        fi
        sleep 1
    done
    info "Klipper is back online (or timed out)"

    # 3. Now restart Moonraker service
    info "Restarting Moonraker service..."
    curl -fsS --connect-timeout 5 --max-time 10 \
        -X POST "${MOONRAKER_URL}/machine/services/restart?service=moonraker" \
        >/dev/null 2>&1 || warn "Moonraker service restart request failed (non-fatal)"

    # 4. Wait for Moonraker to come back online
    info "Waiting for Moonraker to come back online..."
    attempts=0
    until curl -fsS --connect-timeout 3 --max-time 5 \
            "${MOONRAKER_URL}/server/info" >/dev/null 2>&1; do
        attempts=$(( attempts + 1 ))
        if [[ "${attempts}" -ge "${max_attempts}" ]]; then
            warn "Moonraker did not come back online after ${max_attempts}s; skipping firmware restart"
            return 0
        fi
        sleep 1
    done
    info "Moonraker is back online"

    # 5. Send firmware restart
    info "Sending firmware restart..."
    curl -fsS --connect-timeout 5 --max-time 10 \
        -X POST "${MOONRAKER_URL}/printer/restart" \
        >/dev/null 2>&1 || warn "Firmware restart request failed (non-fatal)"
    info "Firmware restart sent"

    return 0
}

checkout_branch() {
    local branch="$1"
    [[ -d "${REPO_DIR}/.git" ]] || fail "REPO_DIR is not a git repo: ${REPO_DIR}"
    info "Fetching origin..."
    git -C "${REPO_DIR}" fetch origin

    info "Checking out branch: ${branch}"
    if git -C "${REPO_DIR}" show-ref --verify --quiet "refs/heads/${branch}"; then
        git -C "${REPO_DIR}" checkout "${branch}" || fail "Failed to checkout local branch: ${branch}"
    elif git -C "${REPO_DIR}" show-ref --verify --quiet "refs/remotes/origin/${branch}"; then
        git -C "${REPO_DIR}" checkout -b "${branch}" --track "origin/${branch}" || fail "Failed to create tracking branch for origin/${branch}"
    else
        fail "Branch '${branch}' does not exist locally or on origin"
    fi

    if git -C "${REPO_DIR}" show-ref --verify --quiet "refs/remotes/origin/${branch}"; then
        git -C "${REPO_DIR}" pull --ff-only origin "${branch}" || warn "ff-only pull failed for ${branch}; continuing"
    else
        warn "Remote origin/${branch} not found; skipping pull"
    fi
    info "Now on branch: $(git -C "${REPO_DIR}" rev-parse --abbrev-ref HEAD)"
}

install_rfid_cfg() {
    [[ -d "${REPO_CFG}" ]] || fail "RFID config dir not found: ${REPO_CFG}"
    mkdir -p "${LIVE_RFID_CFG_DIR}"

    for src in "${REPO_CFG}"/*.cfg; do
        [[ -f "$src" ]] || continue
        local fname
        fname="$(basename "$src")"

        # Install the Happy Hare config (vivid_hh.cfg) only when HH is detected;
        # install the AFC config (vivid.cfg) only when HH is NOT detected.
        # All other .cfg files are always installed.
        if [[ "$fname" == "vivid_hh.cfg" && "${HH_AVAILABLE}" -eq 0 ]]; then
            info "Skipped (Happy Hare not detected): ${src}"
            continue
        fi
        if [[ "$fname" == "vivid.cfg" && "${HH_AVAILABLE}" -eq 1 ]]; then
            info "Skipped (Happy Hare detected, using vivid_hh.cfg instead): ${src}"
            continue
        fi

        local dst="${LIVE_RFID_CFG_DIR}/${fname}"
        if [[ -e "$dst" ]]; then
            info "Skipped (already exists): ${dst}"
        else
            cp "$src" "$dst"
            info "Copied: ${src} -> ${dst}"
        fi
    done
}

inject_printer_cfg_include() {
    [[ -f "${PRINTER_CFG}" ]] || { warn "printer.cfg not found: ${PRINTER_CFG}, skipping include injection"; return 0; }

    grep -qF '[include rfid/*.cfg]' "${PRINTER_CFG}" && { info "printer.cfg already contains [include rfid/*.cfg]"; return 0; }

    backup_file "${PRINTER_CFG}"

    python3 - "${PRINTER_CFG}" <<'PYEOF'
from pathlib import Path
import sys

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)

include_line = "[include rfid/*.cfg]\n"

# Insert after the last existing [include ...] line, or prepend if none found
last_include_idx = None
for i, line in enumerate(lines):
    if line.strip().startswith("[include "):
        last_include_idx = i

if last_include_idx is not None:
    lines.insert(last_include_idx + 1, include_line)
else:
    lines.insert(0, include_line)

path.write_text("".join(lines), encoding="utf-8")
PYEOF

    info "Injected [include rfid/*.cfg] into ${PRINTER_CFG}"
}

ensure_gpl_lane_header() {
    # Ensures LOCAL_LANE begins with a short GPL provenance header.
    # Idempotent: if the header is already present, does nothing.
    python3 - "${LOCAL_LANE}" <<'PYEOF'
from pathlib import Path
import sys

path = Path(sys.argv[1])
src = path.read_text(encoding="utf-8")

header_first_line = "# Derived from ArmoredTurtle/AFC-Klipper-Add-On, file: extras/AFC_lane.py"
if src.startswith(header_first_line):
    raise SystemExit(0)

header = (
    "# Derived from ArmoredTurtle/AFC-Klipper-Add-On, file: extras/AFC_lane.py\n"
    "# Original Copyright (C) 2024 Armored Turtle\n"
    "# Modifications Copyright (C) 2026 lameandboard\n"
    "#\n"
    "# This file may be distributed under the terms of the GNU GPLv3 license.\n"
    "\n"
)

path.write_text(header + src, encoding="utf-8")
PYEOF
}

patch_local_lane_python() {
    backup_file "${LOCAL_LANE}"
    cp "${UPSTREAM_LANE}" "${LOCAL_LANE}"
    info "Copied upstream AFC_lane.py -> ${LOCAL_LANE}"

    # Add GPL provenance header to our derived/modified copy (idempotent)
    ensure_gpl_lane_header

    python3 - "${LOCAL_LANE}" <<'PYEOF'
from pathlib import Path
import sys

path = Path(sys.argv[1])
src = path.read_text(encoding="utf-8")

PREP_MARKER = '# --- RFID event hook: afc:lane_prep_start ---'
LOADED_MARKER = '# --- RFID event hook: afc:lane_loaded ---'
LOADED_DEBUG = 'self.logger.debug(f"RFID: sent afc:lane_loaded for lane={self.name}")'


def fail(msg):
    raise SystemExit(msg)


def detect_newline(text):
    return '\r\n' if '\r\n' in text else '\n'


NL = detect_newline(src)


def split_lines_keep(text):
    return text.splitlines(keepends=True)


def leading_ws(s):
    return s[:len(s) - len(s.lstrip(' \t'))]


def find_line_indexes(lines, needle):
    return [i for i, line in enumerate(lines) if needle in line]


def has_marker_near(lines, idx, marker, window=10):
    start = max(0, idx - window)
    end = min(len(lines), idx + window + 1)
    return any(marker in lines[j] for j in range(start, end))


lines = split_lines_keep(src)

# 1) Insert afc:lane_prep_start immediately before self.unit_obj.prep_load(self)
prep_hits = find_line_indexes(lines, 'self.unit_obj.prep_load(self)')
if not prep_hits:
    fail('Could not locate prep hook insertion point in AFC_lane.py')
prep_idx = prep_hits[0]
if not has_marker_near(lines, prep_idx, PREP_MARKER):
    indent = leading_ws(lines[prep_idx])
    prep_block = [
        f'{indent}{PREP_MARKER}{NL}',
        f'{indent}try:{NL}',
        f'{indent}    self.printer.send_event("afc:lane_prep_start", self){NL}',
        f'{indent}except Exception:{NL}',
        f'{indent}    self.logger.debug("afc:lane_prep_start send_event failed", exc_info=True){NL}',
    ]
    lines = lines[:prep_idx] + prep_block + lines[prep_idx:]

# 2) Insert afc:lane_loaded after self.set_loaded() inside prep_callback.
# Find self.set_loaded() that is followed by self._post_prep_user_macro() within 5 lines.
set_loaded_hits = find_line_indexes(lines, 'self.set_loaded()')
spool_idx = None
for idx in set_loaded_hits:
    window_end = min(len(lines), idx + 6)
    if any('self._post_prep_user_macro()' in lines[j] for j in range(idx, window_end)):
        spool_idx = idx
        break
if spool_idx is None:
    fail('Could not locate self.set_loaded() followed by self._post_prep_user_macro() in AFC_lane.py')
if not has_marker_near(lines, spool_idx, LOADED_MARKER):
    indent = leading_ws(lines[spool_idx])
    loaded_block = [
        f'{indent}{LOADED_MARKER}{NL}',
        f'{indent}try:{NL}',
        f'{indent}    self.printer.send_event("afc:lane_loaded", self){NL}',
        f'{indent}except Exception:{NL}',
        f'{indent}    self.logger.debug("afc:lane_loaded send_event failed", exc_info=True){NL}',
        f'{indent}{LOADED_DEBUG}{NL}',
    ]
    lines = lines[:spool_idx+1] + loaded_block + lines[spool_idx+1:]

out = ''.join(lines)

for marker in (PREP_MARKER, LOADED_MARKER, LOADED_DEBUG):
    if marker not in out:
        fail(f'Missing marker after patch: {marker}')

path.write_text(out, encoding='utf-8')
PYEOF

    grep -q "afc:lane_prep_start" "${LOCAL_LANE}" || fail "Missing afc:lane_prep_start hook after patch"
    grep -q "afc:lane_loaded" "${LOCAL_LANE}" || fail "Missing afc:lane_loaded hook after patch"
    info "Patched local AFC_lane.py successfully"
}

update_moonraker_block() {
    [[ -f "${MOONRAKER_CONF}" ]] || { warn "Moonraker config not found: ${MOONRAKER_CONF}"; return 0; }

    backup_file "${MOONRAKER_CONF}"

    python3 - "${MOONRAKER_CONF}" "${REPO_DIR}" "$(current_branch)" <<'PYEOF'
import re
import sys
from pathlib import Path

conf_path = Path(sys.argv[1])
repo_path = Path(sys.argv[2]).resolve()
branch = sys.argv[3]

text = conf_path.read_text(encoding="utf-8")

section = f"""
[update_manager RFID]
type: git_repo
channel: dev
path: {repo_path}
origin: https://github.com/lameandboard/rfid.git
primary_branch: {branch}
managed_services: klipper
info_tags: desc=RFID for AFC / Klipper
""".strip() + "\n"

pattern = re.compile(
    r'^\[update_manager RFID\]\n.*?(?=^\[[^\n]+\]|\Z)',
    flags=re.MULTILINE | re.DOTALL
)

if pattern.search(text):
    text = pattern.sub(section + '\n', text)
else:
    if text and not text.endswith('\n'):
        text += '\n'
    text += '\n' + section + '\n'

conf_path.write_text(text, encoding="utf-8")
PYEOF

    info "Updated Moonraker update_manager block"
}

install_git_hooks() {
    local hook_dir="${AFC_DIR}/.git/hooks"
    [[ -d "${hook_dir}" ]] || fail "Git hook dir not found: ${hook_dir}"

    cat > "${hook_dir}/post-merge" <<EOF
#!/usr/bin/env bash
${HOOK_MARKER}
set -e
LOG_FILE="${HOOK_LOG}"
{
  echo "===== \$(date) post-merge ====="
  bash "${REPO_DIR}/install.sh" --no-restart --no-hooks \
    --klipper-dir "${KLIPPER_DIR}" \
    --afc-dir "${AFC_DIR}" \
    --config-dir "${CONFIG_DIR}" \
    --moonraker-url "${MOONRAKER_URL}"
  curl -fsS --connect-timeout 5 --max-time 10 \
    -X POST "${MOONRAKER_URL}/machine/services/restart?service=klipper" >/dev/null 2>&1 || true
  _attempts=0
  until curl -fsS --connect-timeout 3 --max-time 5 \
      "${MOONRAKER_URL}/printer/info" >/dev/null 2>&1; do
    _attempts=\$(( _attempts + 1 ))
    [[ "\${_attempts}" -lt 30 ]] || break
    sleep 1
  done
  curl -fsS --connect-timeout 5 --max-time 10 \
    -X POST "${MOONRAKER_URL}/machine/services/restart?service=moonraker" >/dev/null 2>&1 || true
  _attempts=0
  _moonraker_ready=0
  while [[ "\${_attempts}" -lt 30 ]]; do
    if curl -fsS --connect-timeout 3 --max-time 5 \
        "${MOONRAKER_URL}/server/info" >/dev/null 2>&1; then
      _moonraker_ready=1
      break
    fi
    _attempts=\$(( _attempts + 1 ))
    sleep 1
  done
  if [[ "\${_moonraker_ready}" -eq 1 ]]; then
    curl -fsS --connect-timeout 5 --max-time 10 \
      -X POST "${MOONRAKER_URL}/printer/restart" >/dev/null 2>&1 || true
  fi
} >> "\$LOG_FILE" 2>&1
EOF

    cat > "${hook_dir}/post-checkout" <<EOF
#!/usr/bin/env bash
${HOOK_MARKER}
set -e
LOG_FILE="${HOOK_LOG}"
{
  echo "===== \$(date) post-checkout ====="
  bash "${REPO_DIR}/install.sh" --no-restart --no-hooks \
    --klipper-dir "${KLIPPER_DIR}" \
    --afc-dir "${AFC_DIR}" \
    --config-dir "${CONFIG_DIR}" \
    --moonraker-url "${MOONRAKER_URL}"
  curl -fsS --connect-timeout 5 --max-time 10 \
    -X POST "${MOONRAKER_URL}/machine/services/restart?service=klipper" >/dev/null 2>&1 || true
  _attempts=0
  until curl -fsS --connect-timeout 3 --max-time 5 \
      "${MOONRAKER_URL}/printer/info" >/dev/null 2>&1; do
    _attempts=\$(( _attempts + 1 ))
    [[ "\${_attempts}" -lt 30 ]] || break
    sleep 1
  done
  curl -fsS --connect-timeout 5 --max-time 10 \
    -X POST "${MOONRAKER_URL}/machine/services/restart?service=moonraker" >/dev/null 2>&1 || true
  _attempts=0
  until curl -fsS --connect-timeout 3 --max-time 5 \
      "${MOONRAKER_URL}/server/info" >/dev/null 2>&1; do
    _attempts=\$(( _attempts + 1 ))
    [[ "\${_attempts}" -lt 30 ]] || break
    sleep 1
  done
  curl -fsS --connect-timeout 5 --max-time 10 \
    -X POST "${MOONRAKER_URL}/printer/restart" >/dev/null 2>&1 || true
} >> "\$LOG_FILE" 2>&1
EOF

    chmod +x "${hook_dir}/post-merge" "${hook_dir}/post-checkout"
    info "Installed AFC git hooks in ${hook_dir}"
}

do_uninstall() {
    info "=== Uninstall step 1: RFID extras symlinks ==="
    local spoolman_live="${KLIPPER_EXTRAS}/spoolman_client.py"
    for live_path in "${LIVE_RFID}" "${LIVE_MFRC}" "${LIVE_PN532}" "${LIVE_PARSER}" "${spoolman_live}"; do
        local base
        base="$(basename "${live_path}")"
        local repo_src="${REPO_EXTRAS}/${base}"
        if [[ -L "${live_path}" ]]; then
            local target
            target="$(readlink -f "${live_path}" 2>/dev/null || true)"
            local expected
            expected="$(readlink -f "${repo_src}" 2>/dev/null || true)"
            if [[ -n "${expected}" && "${target}" == "${expected}" ]]; then
                rm -f "${live_path}"
                info "Removed symlink: ${live_path}"
            else
                warn "Skipping ${live_path} — symlink points elsewhere (${target}); leaving untouched"
            fi
        elif [[ -e "${live_path}" ]]; then
            warn "Skipping ${live_path} — exists but is not a symlink; remove manually if needed"
        else
            info "Not present (already removed): ${live_path}"
        fi
    done

    info "=== Uninstall step 2: AFC_lane.py symlink ==="
    if [[ -L "${LIVE_LANE}" ]]; then
        local target
        target="$(readlink -f "${LIVE_LANE}" 2>/dev/null || true)"
        local expected
        expected="$(readlink -f "${LOCAL_LANE}" 2>/dev/null || true)"
        if [[ -n "${expected}" && "${target}" == "${expected}" ]]; then
            rm -f "${LIVE_LANE}"
            info "Removed symlink: ${LIVE_LANE}"
            if [[ -f "${UPSTREAM_LANE}" ]]; then
                cp "${UPSTREAM_LANE}" "${LIVE_LANE}"
                info "Restored ${LIVE_LANE} from upstream: ${UPSTREAM_LANE}"
            else
                warn "Upstream AFC_lane.py not found at ${UPSTREAM_LANE}; symlink removed but not restored"
            fi
        else
            warn "Skipping ${LIVE_LANE} — symlink points elsewhere (${target}); leaving untouched"
        fi
    elif [[ -e "${LIVE_LANE}" ]]; then
        warn "Skipping ${LIVE_LANE} — exists but is not a symlink; remove manually if needed"
    else
        info "Not present (already removed): ${LIVE_LANE}"
    fi

    info "=== Uninstall step 3: AFC git hooks ==="
    local hook_dir="${AFC_DIR}/.git/hooks"
    if [[ -d "${hook_dir}" ]]; then
        for hook in post-merge post-checkout; do
            local hf="${hook_dir}/${hook}"
            if [[ -f "${hf}" ]]; then
                if grep -qF "${HOOK_MARKER}" "${hf}"; then
                    rm -f "${hf}"
                    info "Removed AFC git hook: ${hf}"
                else
                    warn "Skipping ${hf} — RFID hook marker not found; may belong to another tool"
                fi
            else
                info "Hook not present (already removed): ${hf}"
            fi
        done
    else
        info "AFC hook dir not found (${hook_dir}); skipping hook removal"
    fi

    info "=== Uninstall step 4: Moonraker update_manager block ==="
    if [[ -f "${MOONRAKER_CONF}" ]]; then
        if grep -qF '[update_manager RFID]' "${MOONRAKER_CONF}"; then
            backup_file "${MOONRAKER_CONF}"
            python3 - "${MOONRAKER_CONF}" <<'PYEOF'
import re
import sys
from pathlib import Path

conf_path = Path(sys.argv[1])
text = conf_path.read_text(encoding="utf-8")

# Remove the [update_manager RFID] block and the blank line that typically
# precedes it, leaving the rest of the file intact.
pattern = re.compile(
    r'\n?\[update_manager RFID\]\n.*?(?=\n\[|\Z)',
    flags=re.MULTILINE | re.DOTALL,
)
text = pattern.sub('', text)

# Collapse any runs of 3+ newlines down to two (one blank line).
text = re.sub(r'\n{3,}', '\n\n', text)

conf_path.write_text(text, encoding="utf-8")
print('[INFO] Removed [update_manager RFID] block from moonraker.conf')
PYEOF
        else
            info "moonraker.conf: [update_manager RFID] block not found; skipping"
        fi
    else
        info "Moonraker config not found: ${MOONRAKER_CONF}; skipping"
    fi

    info "=== Uninstall step 5: printer.cfg include line ==="
    if [[ -f "${PRINTER_CFG}" ]]; then
        if grep -qF '[include rfid/*.cfg]' "${PRINTER_CFG}"; then
            backup_file "${PRINTER_CFG}"
            python3 - "${PRINTER_CFG}" <<'PYEOF'
from pathlib import Path
import sys

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
lines = [l for l in lines if '[include rfid/*.cfg]' not in l]
path.write_text("".join(lines), encoding="utf-8")
print('[INFO] Removed [include rfid/*.cfg] from printer.cfg')
PYEOF
        else
            info "printer.cfg: [include rfid/*.cfg] not found; skipping"
        fi
    else
        info "printer.cfg not found: ${PRINTER_CFG}; skipping"
    fi

    info "=== Uninstall step 6: RFID config directory ==="
    if [[ -d "${LIVE_RFID_CFG_DIR}" ]]; then
        rm -rf "${LIVE_RFID_CFG_DIR}"
        info "Removed RFID config dir: ${LIVE_RFID_CFG_DIR}"
    else
        info "RFID config dir not found (already removed): ${LIVE_RFID_CFG_DIR}"
    fi

    info "=== Uninstall step 7: UID cache ==="
    local cache_file="${HOME}/RFID/cache/rfid_uid_cache.json"
    if [[ -f "${cache_file}" ]]; then
        rm -f "${cache_file}"
        info "Removed cache file: ${cache_file}"
    else
        info "Cache file not found (already removed): ${cache_file}"
    fi
    for d in "${HOME}/RFID/cache" "${HOME}/RFID"; do
        if [[ -d "$d" && -z "$(ls -A "$d" 2>/dev/null)" ]]; then
            rmdir "$d"
            info "Removed empty directory: $d"
        fi
    done

    cat <<EOF

===== RFID uninstall complete =====

Removed (where present):
  Klipper extra symlinks: rfid.py, mfrc522.py, pn532.py, rfid_tag_parser.py, spoolman_client.py
  AFC_lane.py symlink (restored from upstream AFC if available)
  AFC git hooks (only if owned by this installer)
  [update_manager RFID] block from moonraker.conf
  [include rfid/*.cfg] line from printer.cfg
  RFID config directory: ${LIVE_RFID_CFG_DIR}
  UID cache: ${HOME}/RFID/cache/rfid_uid_cache.json

Preserved (not touched):
  Log files (rfid.log, rfid_hook.log and any rotated backups)
  Python packages (pycryptodome, cbor2)
  RFID repo: ${REPO_DIR}
    -> re-run with --purge-repo to delete it too

EOF

    if [[ "${PURGE_REPO}" -eq 1 ]]; then
        info "=== Uninstall step 8: purging repo directory ==="
        warn "About to permanently delete: ${REPO_DIR}"
        warn "Press Ctrl-C within 5 seconds to abort."
        sleep 5
        rm -rf "${REPO_DIR}"
        # The script has just deleted itself; exit cleanly without further file access.
        exit 0
    fi
}


while [[ $# -gt 0 ]]; do
    case "$1" in
        --klipper-dir)
            [[ $# -ge 2 ]] || fail "--klipper-dir requires a value"
            KLIPPER_DIR="$2"
            KLIPPER_EXTRAS="${KLIPPER_DIR}/klippy/extras"
            LIVE_RFID="${KLIPPER_EXTRAS}/rfid.py"
            LIVE_MFRC="${KLIPPER_EXTRAS}/mfrc522.py"
            LIVE_PN532="${KLIPPER_EXTRAS}/pn532.py"
            LIVE_PARSER="${KLIPPER_EXTRAS}/rfid_tag_parser.py"
            LIVE_LANE="${KLIPPER_EXTRAS}/AFC_lane.py"
            shift 2
            ;;
        --afc-dir)
            [[ $# -ge 2 ]] || fail "--afc-dir requires a value"
            AFC_DIR="$2"
            UPSTREAM_LANE="${AFC_DIR}/extras/AFC_lane.py"
            shift 2
            ;;
        --hh-dir)
            [[ $# -ge 2 ]] || fail "--hh-dir requires a value"
            HH_DIR="$2"
            shift 2
            ;;
        --config-dir)
            [[ $# -ge 2 ]] || fail "--config-dir requires a value"
            CONFIG_DIR="$2"
            MOONRAKER_CONF="${CONFIG_DIR}/moonraker.conf"
            CONFIG_BASE="$(dirname "$CONFIG_DIR")"
            HOOK_LOG="${CONFIG_BASE}/logs/rfid_hook.log"
            mkdir -p "$(dirname "$HOOK_LOG")"
            LIVE_RFID_CFG_DIR="${CONFIG_DIR}/rfid"
            PRINTER_CFG="${CONFIG_DIR}/printer.cfg"
            shift 2
            ;;
        --moonraker-conf)
            [[ $# -ge 2 ]] || fail "--moonraker-conf requires a value"
            MOONRAKER_CONF="$2"
            shift 2
            ;;
        --moonraker-url)
            [[ $# -ge 2 ]] || fail "--moonraker-url requires a value"
            MOONRAKER_URL="$2"
            shift 2
            ;;
        --update)
            UPDATE=1
            shift
            ;;
        --force)
            FORCE=1
            shift
            ;;
        --uninstall)
            UNINSTALL=1
            shift
            ;;
        --purge-repo)
            PURGE_REPO=1
            shift
            ;;
        --no-restart)
            NO_RESTART=1
            shift
            ;;
        --no-hooks)
            NO_HOOKS=1
            shift
            ;;
        --no-afc)
            NO_AFC=1
            shift
            ;;
        -b|--branch)
            [[ $# -ge 2 ]] || fail "--branch requires a value"
            BRANCH="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "Unknown argument: $1"
            ;;
    esac
done

if [[ "${UNINSTALL}" -eq 1 ]]; then
    # Detect AFC for uninstall (same logic used during install).
    AFC_AVAILABLE=0
    if [[ "${NO_AFC}" -ne 1 && -d "${AFC_DIR}" && -f "${UPSTREAM_LANE}" ]]; then
        AFC_AVAILABLE=1
    fi
    do_uninstall
    exit 0
fi

if [[ "${FORCE}" -eq 1 ]]; then
    repo_url="https://github.com/lameandboard/rfid.git"
    parent_dir="$(dirname "${REPO_DIR}")"
    repo_name="$(basename "${REPO_DIR}")"
    branch="${BRANCH:-$(current_branch)}"
    info "--force specified: re-cloning ${repo_url} (${branch})"
    cd "${parent_dir}"
    rm -rf "${repo_name}"
    git clone -b "${branch}" "${repo_url}" "${repo_name}"
    forwarded=()
    for arg in "${ORIGINAL_ARGS[@]}"; do
        [[ "$arg" == "--force" ]] || forwarded+=("$arg")
    done
    exec bash "${parent_dir}/${repo_name}/install.sh" "${forwarded[@]}"
fi

if [[ "${UPDATE}" -eq 1 ]]; then
    [[ -d "${REPO_DIR}/.git" ]] || fail "${REPO_DIR} is not a git repository"
    info "Pulling latest RFID changes..."
    git -C "${REPO_DIR}" pull --ff-only
    forwarded=()
    for arg in "${ORIGINAL_ARGS[@]}"; do
        [[ "$arg" == "--update" ]] || forwarded+=("$arg")
    done
    exec bash "${REPO_DIR}/install.sh" "${forwarded[@]}"
fi

[[ -d "${KLIPPER_DIR}" ]] || fail "Klipper dir not found: ${KLIPPER_DIR}"
[[ -d "${KLIPPER_EXTRAS}" ]] || fail "Klipper extras dir not found: ${KLIPPER_EXTRAS}"
[[ -d "${REPO_EXTRAS}" ]] || fail "Repo extras dir not found: ${REPO_EXTRAS}"
[[ -d "${REPO_CFG}" ]] || fail "Repo config dir not found: ${REPO_CFG}"
[[ -f "${REPO_EXTRAS}/rfid.py" ]] || fail "Missing ${REPO_EXTRAS}/rfid.py"
[[ -f "${REPO_EXTRAS}/mfrc522.py" ]] || fail "Missing ${REPO_EXTRAS}/mfrc522.py"
[[ -f "${REPO_EXTRAS}/pn532.py" ]] || fail "Missing ${REPO_EXTRAS}/pn532.py"
[[ -f "${REPO_EXTRAS}/rfid_tag_parser.py" ]] || fail "Missing ${REPO_EXTRAS}/rfid_tag_parser.py"

# Detect AFC (optional)
AFC_AVAILABLE=0
if [[ "${NO_AFC}" -eq 1 ]]; then
    info "AFC integration disabled via --no-afc"
elif [[ -d "${AFC_DIR}" && -f "${UPSTREAM_LANE}" ]]; then
    AFC_AVAILABLE=1
    info "AFC detected: ${AFC_DIR}"
else
    info "AFC not found — skipping all AFC steps (AFC is optional)"
fi

# Detect Happy Hare (optional) — check for ~/Happy-Hare, ~/Happy_Hare, or ~/klipper_mmu
HH_AVAILABLE=0
if [[ -d "${HH_DIR}" ]]; then
    HH_AVAILABLE=1
    info "Happy Hare detected: ${HH_DIR}"
elif [[ -d "${HOME}/Happy-Hare" ]]; then
    HH_AVAILABLE=1
    HH_DIR="${HOME}/Happy-Hare"
    info "Happy Hare detected: ${HH_DIR}"
elif [[ -d "${HOME}/Happy_Hare" ]]; then
    HH_AVAILABLE=1
    HH_DIR="${HOME}/Happy_Hare"
    info "Happy Hare detected: ${HH_DIR}"
elif [[ -d "${HOME}/klipper_mmu" ]]; then
    HH_AVAILABLE=1
    HH_DIR="${HOME}/klipper_mmu"
    info "Happy Hare detected: ${HH_DIR}"
else
    info "Happy Hare not found — will install AFC config template"
fi

info "Using Klipper extras: ${KLIPPER_EXTRAS}"
if [[ "${AFC_AVAILABLE}" -eq 1 ]]; then
    info "Using AFC repo:       ${AFC_DIR}"
    info "Using upstream lane:  ${UPSTREAM_LANE}"
    info "Using local lane:     ${LOCAL_LANE}"
    info "Using live lane:      ${LIVE_LANE}"
fi
if [[ "${HH_AVAILABLE}" -eq 1 ]]; then
    info "Using Happy Hare dir: ${HH_DIR}"
    info "RFID config template: vivid_hh.cfg (Happy Hare, slots= 0-based)"
else
    info "RFID config template: vivid.cfg (AFC, lanes= 1-based)"
fi
info "Using Moonraker conf: ${MOONRAKER_CONF}"
info "Using Moonraker URL:  ${MOONRAKER_URL}"
info "Using RFID branch:    ${BRANCH:-$(current_branch)}"
info "Using RFID cfg dir:   ${REPO_CFG} -> ${LIVE_RFID_CFG_DIR} (copy-on-first-install)"

if [[ -n "${BRANCH}" ]]; then
    info "=== Step 0: checkout branch ${BRANCH} ==="
    checkout_branch "${BRANCH}"
fi

info "=== Step 1: install RFID extras ==="
force_symlink "${REPO_EXTRAS}/rfid.py" "${LIVE_RFID}"
force_symlink "${REPO_EXTRAS}/mfrc522.py" "${LIVE_MFRC}"
force_symlink "${REPO_EXTRAS}/pn532.py" "${LIVE_PN532}"
force_symlink "${REPO_EXTRAS}/rfid_tag_parser.py" "${LIVE_PARSER}"
force_symlink "${REPO_EXTRAS}/spoolman_client.py" "${KLIPPER_EXTRAS}/spoolman_client.py"

info "=== Step 1b: install optional Python dependencies ==="
# pycryptodome — required for Bambu Lab MIFARE key derivation (HKDF).
# cbor2         — required for OpenPrintTag CBOR payloads.
# Both are optional: the RFID module degrades gracefully when absent.
PYTHON="${KLIPPY_ENV:-${HOME}/klippy-env}/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
    PYTHON="$(command -v python3 2>/dev/null || true)"
fi
if [[ -z "${PYTHON}" || ! -x "${PYTHON}" ]]; then
    warn "No Python interpreter found — skipping optional package installation"
    PYTHON=""
fi
install_py_pkg() {
    local pkg="$1"
    local import_name="${2:-${pkg//-/_}}"
    if [[ -z "${PYTHON}" ]]; then
        warn "  ${pkg}: skipped (no Python interpreter)"
        return
    fi
    if "${PYTHON}" -c "import ${import_name}" 2>/dev/null; then
        info "  ${pkg}: already installed"
    else
        info "  ${pkg}: installing..."
        if "${PYTHON}" -m pip install --quiet "${pkg}"; then
            info "  ${pkg}: installed"
        else
            warn "  ${pkg}: installation failed — feature requiring it will be disabled"
        fi
    fi
}
install_py_pkg pycryptodome Crypto
install_py_pkg cbor2

info "=== Step 2: refresh and patch local AFC_lane.py ==="
if [[ "${AFC_AVAILABLE}" -eq 1 ]]; then
    patch_local_lane_python
else
    info "Skipping AFC_lane.py patch (AFC not installed)"
fi

info "=== Step 3: force live AFC_lane.py to RFID copy ==="
if [[ "${AFC_AVAILABLE}" -eq 1 ]]; then
    force_symlink "${LOCAL_LANE}" "${LIVE_LANE}"
else
    info "Skipping AFC_lane.py symlink (AFC not installed)"
fi

info "=== Step 4: install RFID config dir and printer.cfg include ==="
install_rfid_cfg
inject_printer_cfg_include

info "=== Step 5: update Moonraker updater block ==="
update_moonraker_block

info "=== Step 6: install AFC git hooks ==="
if [[ "${NO_HOOKS}" -eq 0 && "${AFC_AVAILABLE}" -eq 1 ]]; then
    install_git_hooks
elif [[ "${AFC_AVAILABLE}" -eq 0 ]]; then
    info "Skipping git hook install (AFC not installed)"
else
    info "Skipping git hook install (--no-hooks)"
fi

info "=== Step 7: verify ==="
if [[ "${AFC_AVAILABLE}" -eq 1 ]]; then
    grep -q 'afc:lane_prep_start' "${LOCAL_LANE}" || fail 'Final verify failed: missing afc:lane_prep_start hook'
    grep -q 'afc:lane_loaded' "${LOCAL_LANE}" || fail 'Final verify failed: missing afc:lane_loaded hook'
    [[ "$(readlink -f "${LIVE_LANE}")" == "$(readlink -f "${LOCAL_LANE}")" ]] || fail 'Final verify failed: live lane symlink still wrong'
fi
[[ -d "${LIVE_RFID_CFG_DIR}" ]] || fail 'Final verify failed: rfid config dir not found'

if [[ "${NO_RESTART}" -eq 0 ]]; then
    info "=== Step 8: restarting via Moonraker ==="
    restart_via_moonraker || warn "Could not restart via Moonraker API"
else
    info "Skipping service restarts (--no-restart)"
fi

cat <<EOF

===== RFID install complete =====

Installed live extras:
  ${LIVE_RFID} -> ${REPO_EXTRAS}/rfid.py
  ${LIVE_MFRC} -> ${REPO_EXTRAS}/mfrc522.py
  ${LIVE_PN532} -> ${REPO_EXTRAS}/pn532.py
  ${LIVE_PARSER} -> ${REPO_EXTRAS}/rfid_tag_parser.py

Optional Python packages (for Bambu Lab and OpenPrintTag support):
  pycryptodome — Bambu Lab MIFARE key derivation (HKDF)
  cbor2        — OpenPrintTag CBOR payload decoding

RFID config dir:
  ${LIVE_RFID_CFG_DIR}/
  (*.cfg files copied from ${REPO_CFG} on first install; existing files are never overwritten)
  Config template installed: $( [[ "${HH_AVAILABLE}" -eq 1 ]] && printf 'vivid_hh.cfg (Happy Hare, slots= 0-based)' || printf 'vivid.cfg (AFC, lanes= 1-based)' )

printer.cfg include:
  ${PRINTER_CFG}: [include rfid/*.cfg]

EOF

if [[ "${AFC_AVAILABLE}" -eq 1 ]]; then
cat <<EOF
Lane patch flow:
  upstream AFC copy: ${UPSTREAM_LANE}
  local patched copy: ${LOCAL_LANE}
  live Klipper file: ${LIVE_LANE}

EOF
else
    printf 'AFC: not installed — lane patch, symlink, and git hook steps were skipped\n\n'
fi

cat <<EOF
RFID config:
  ${LIVE_RFID_CFG_DIR}/ (real directory, copy-on-first-install)

printer.cfg:
  [include rfid/*.cfg] injected (or already present)

EOF

if [[ "${AFC_AVAILABLE}" -eq 1 ]]; then
cat <<EOF
Git hooks:
  ${AFC_DIR}/.git/hooks/post-merge
  ${AFC_DIR}/.git/hooks/post-checkout

Hook log:
  ${HOOK_LOG}

EOF
fi
printf 'RFID branch:\n  %s\n\n' \
    "${BRANCH:-$(git -C "${REPO_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'unknown')}"
