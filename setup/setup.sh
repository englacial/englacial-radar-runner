#!/bin/bash
# System setup for the radar manager: applies the changes outside this
# repository that are listed in setup/SYSTEM_CHANGES.md.
#
#   setup/setup.sh --dry-run          show what would change (no root needed)
#   sudo setup/setup.sh               apply (run it inside tmux: a dropped SSH
#                                     session must not interrupt it)
#   setup/check.py                    verify afterwards (and after the reboot)
#
# Safe to run again, also after an interrupted run: every step looks at the
# current state first and does nothing if it is already right. It stops at the
# first failure. Files are copied from setup/files/ (same path as on the
# system) through a temporary file and a rename, so a file is never left half
# written.
#
# It never touches the network configuration or SSH, installs or removes no
# package, never restarts or stops a running service of this project, and
# never reboots. The only Tailscale change is switching off its automatic
# self-update. Changes to logind and journald take effect at the next reboot.
#
# Optional steps, off unless asked for (read the risks in SYSTEM_CHANGES.md):
#   --nvme-by-label     mount /mnt/nvme by filesystem label instead of UUID (edits /etc/fstab)
#   --hold-packages     hold the kernel, firmware, bootloader, gpsd and tailscale packages
#
# For tests only: with DESTDIR=<dir> files go under <dir> and root is not needed.
set -Eeuo pipefail
umask 022

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
FILES=$REPO/setup/files
SERVICE_USER=englacial
UNIT_REPO=/home/englacial/englacial-radar-runner     # WorkingDirectory in systemd/radar-power.service
DESTDIR=${DESTDIR:-}
[ -n "$DESTDIR" ] || PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

DRY_RUN=0
OPT_NVME=0
OPT_HOLD=0
CHANGES=0
CHANGED=0               # set by put_file: 1 if the file was (or would be) written
RELOAD_NEEDED=0
REBOOT_REASONS=()
CURRENT_STEP="start"
TMP_FILE=""

usage() { sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --nvme-by-label) OPT_NVME=1 ;;
        --hold-packages) OPT_HOLD=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "setup: unknown option: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

# ---------------------------------------------------------------- helpers

log()  { printf '%s\n' "$*"; }
step() { CURRENT_STEP=$1; log ""; log "== $1"; }
ok()   { log "  ok       $*"; }
note() { log "  note     $*"; }
die()  { log "" >&2; log "setup: FAILED in step \"$CURRENT_STEP\": $*" >&2
         log "setup: nothing after this point was done. Fix the cause and run it again." >&2; exit 1; }
trap '[ "$BASH_SUBSHELL" -gt 0 ] || die "command failed (line $LINENO): $BASH_COMMAND"' ERR
trap '[ -z "$TMP_FILE" ] || rm -f "$TMP_FILE"' EXIT

change() {      # announce one change
    CHANGES=$((CHANGES + 1))
    if [ "$DRY_RUN" -eq 1 ]; then log "  WOULD    $*"; else log "  CHANGE   $*"; fi
}

run() {         # run a command that changes the system (only printed in a dry run)
    change "run: $*"
    [ "$DRY_RUN" -eq 1 ] || "$@" || die "command failed: $*"
}

need_reboot() { REBOOT_REASONS+=("$1"); }

put_file() {    # put_file SOURCE DESTINATION MODE: copy if missing or different; sets CHANGED
    local src=$1 dst=$DESTDIR$2 mode=$3 want have
    CHANGED=0
    [ -f "$src" ] || die "missing in the repository: $src"
    want="${mode#0} root root"
    [ -z "$DESTDIR" ] || want="${mode#0}"
    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
        have=$(stat -c '%a %U %G' "$dst")
        [ -z "$DESTDIR" ] || have=$(stat -c '%a' "$dst")
        if [ "$have" = "$want" ]; then
            ok "$2 is in place"
            return 0
        fi
        change "$2: owner and mode $have -> $want"
    elif [ -e "$dst" ]; then
        change "replace $2 (differs from ${src#"$REPO"/}):"
        diff -u "$dst" "$src" | sed -n '3,$p' | sed 's/^/             /' || true
    else
        change "install $2 (from ${src#"$REPO"/})"
    fi
    CHANGED=1
    [ "$DRY_RUN" -eq 0 ] || return 0
    install -d -m 0755 "$(dirname "$dst")"
    TMP_FILE=$(mktemp "$(dirname "$dst")/.radar-setup.XXXXXX")     # same directory: the rename is atomic
    cat "$src" > "$TMP_FILE"
    chmod "$mode" "$TMP_FILE"
    [ -n "$DESTDIR" ] || chown root:root "$TMP_FILE"
    sync "$TMP_FILE"
    mv -f "$TMP_FILE" "$dst"
    TMP_FILE=""
}

install_file() { put_file "$FILES/$1" "/$1" 0644; }    # a drop-in file from setup/files/

unit_known() { [ -n "$(systemctl list-unit-files --no-legend "$1" 2>/dev/null)" ]; }

mask_unit() {   # stop a unit and make it impossible to start; undone by "systemctl unmask"
    local unit=$1 enabled active
    if ! unit_known "$unit"; then
        ok "$unit does not exist on this system"
        return 0
    fi
    enabled=$(systemctl is-enabled "$unit" 2>/dev/null || true)
    active=$(systemctl is-active "$unit" 2>/dev/null || true)
    if [ "$enabled" = masked ] && [ "$active" != active ]; then
        ok "$unit is masked and not running"
    else
        log "           $unit is ${enabled:-unknown}, ${active:-unknown}"
        run systemctl mask --now "$unit"
        # A timer masked while running is left "failed" (unit to trigger vanished)
        # until the next boot; clear that so systemd does not report "degraded".
        [ "$DRY_RUN" -eq 1 ] || systemctl reset-failed "$unit" 2>/dev/null || true
    fi
}

logind_property() {
    busctl get-property org.freedesktop.login1 /org/freedesktop/login1 \
        org.freedesktop.login1.Manager "$1" 2>/dev/null | cut -d' ' -f2- || true
}

installed() { [ "$(dpkg-query -W -f='${db:Status-Status}' "$1" 2>/dev/null || true)" = installed ]; }

reload_systemd() {     # only if a file that systemd reads was changed in this run
    [ "$RELOAD_NEEDED" -eq 1 ] || return 0
    run systemctl daemon-reload
    RELOAD_NEEDED=0
}

# ---------------------------------------------------------------- steps

preflight() {
    step "Checks before starting"
    [ -d "$FILES" ] || die "$FILES is missing"
    for tool in systemctl systemd-tmpfiles busctl dpkg-query cmp diff stat install mktemp; do
        command -v "$tool" >/dev/null || die "required command not found: $tool"
    done
    if [ "$DRY_RUN" -eq 1 ]; then
        note "dry run: nothing will be changed"
    elif [ -z "$DESTDIR" ] && [ "$(id -u)" -ne 0 ]; then
        die "must be run as root: sudo setup/setup.sh (or --dry-run to look first)"
    fi
    [ -n "$DESTDIR" ] || [ -d /run/systemd/system ] || die "systemd is not running here"
    id "$SERVICE_USER" >/dev/null 2>&1 || die "user $SERVICE_USER does not exist; create it first"
    ok "repository $REPO, service user $SERVICE_USER"
}

# (d) Directories: root's (power service) and, inside them, "user" for the
# services that run as englacial. Neither writes in the other's directory.
step_directories() {
    step "Directories under /run/radar-runner, /var/lib/radar-runner, /etc/radar-runner"
    local conf=etc/tmpfiles.d/radar-runner.conf kind path mode user group want have apply=0
    install_file "$conf"
    while read -r kind path mode user group _; do
        [ "$kind" = d ] || continue
        want="${mode#0} $user $group"
        have=$(stat -c '%a %U %G' "$DESTDIR$path" 2>/dev/null || echo "missing")
        if [ -n "$DESTDIR" ]; then
            want="${mode#0}"
            have=$(stat -c '%a' "$DESTDIR$path" 2>/dev/null || echo "missing")
        fi
        if [ "$have" = "$want" ]; then
            ok "$path is $want"
        else
            log "           $path is $have, should be $want"
            apply=1
        fi
    done < <(grep -v '^[[:space:]]*#' "$FILES/$conf")
    [ "$apply" -eq 0 ] || run systemd-tmpfiles --create "$DESTDIR/$conf"
}

# (g) The UPS wake-up contact presses the power button when power returns.
step_power_key() {
    step "logind: ignore the power key (UPS wake-up contact)"
    install_file etc/systemd/logind.conf.d/ups.conf
    local now
    now=$(logind_property HandlePowerKey)
    if [ "$now" = '"ignore"' ]; then
        ok "logind is using HandlePowerKey=ignore"
    else
        note "logind is using HandlePowerKey=${now:-unknown}: the file takes effect at the next reboot"
        need_reboot "logind: HandlePowerKey=ignore"
    fi
}

# (a) Shutdown delay: undo the 30 s that unattended-upgrades allows inhibitors.
step_inhibit_delay() {
    step "logind: shutdown inhibitor delay back to 5 s"
    install_file etc/systemd/logind.conf.d/zz-radar-runner.conf
    local now
    now=$(logind_property InhibitDelayMaxUSec)
    if [ "$now" = 5000000 ]; then
        ok "logind is using InhibitDelayMaxSec=5"
    else
        note "logind is using InhibitDelayMaxUSec=${now:-unknown}: the file takes effect at the next reboot"
        need_reboot "logind: InhibitDelayMaxSec=5"
    fi
}

# (a) Automatic updates: apt, unattended-upgrades, snap, firmware metadata.
step_auto_updates() {
    step "Automatic updates off"
    local unit state held
    for unit in apt-daily.service apt-daily-upgrade.service; do
        state=$(systemctl is-active "$unit" 2>/dev/null || true)
        case "$state" in
            active|activating|deactivating)
                die "$unit is running an automatic apt job right now. Wait until
       'systemctl is-active $unit' says inactive, then run setup again." ;;
        esac
    done
    install_file etc/apt/apt.conf.d/99zz-radar-runner-no-auto-updates
    mask_unit apt-daily.timer
    mask_unit apt-daily-upgrade.timer
    # Its only job is to finish upgrades at shutdown. It holds the shutdown
    # delay lock that the power service has to work around.
    mask_unit unattended-upgrades.service
    mask_unit fwupd-refresh.timer
    # Runs "rpi-eeprom-update -s -a" at every boot: it would flash a newer
    # bootloader by itself as soon as a package upgrade brought one.
    mask_unit rpi-eeprom-update.service
    if ! command -v snap >/dev/null; then
        ok "snapd is not installed"
    else
        held=$(snap get system refresh.hold 2>/dev/null || true)
        [ -n "$held" ] || held=$(snap refresh --time 2>/dev/null | sed -n 's/^hold: *//p' || true)
        if [ "$held" = forever ]; then
            ok "snap automatic refresh is held forever"
        else
            log "           snap automatic refresh is ${held:+held only until }${held:-not held}"
            run snap refresh --hold
            [ "$DRY_RUN" -eq 1 ] || snap refresh --time 2>&1 | sed 's/^/           /' || true
        fi
    fi
}

# (a) Tailscale updates and restarts itself when its AutoUpdate preference is
# on. Nothing else about Tailscale is read or changed.
tailscale_auto_update() {
    tailscale debug prefs 2>/dev/null | tr -d ' \t\n' | grep -o '"AutoUpdate":{[^}]*}' || true
}

step_tailscale() {
    step "Tailscale: automatic self-update off"
    local prefs
    if ! command -v tailscale >/dev/null; then
        ok "Tailscale is not installed"
        return 0
    fi
    prefs=$(tailscale_auto_update)
    case "$prefs" in
        *'"Apply":true'*)
            log "           Tailscale has $prefs"
            run tailscale set --auto-update=false
            [ "$DRY_RUN" -eq 0 ] || return 0
            prefs=$(tailscale_auto_update)
            case "$prefs" in
                *'"Apply":true'*) die "Tailscale still has $prefs after 'tailscale set --auto-update=false'" ;;
            esac
            ok "Tailscale now has ${prefs:-no AutoUpdate preference}" ;;
        "")
            note "cannot read Tailscale's preferences (is tailscaled running and logged in?): nothing changed."
            note "once it runs: tailscale set --auto-update=false   (setup/check.py shows the value)" ;;
        *)
            ok "Tailscale automatic update is off ($prefs)" ;;
    esac
}

# (a) needrestart: after an apt run, list services that need a restart but
# never restart them by itself (Ubuntu's default restarts them, ssh and
# tailscaled included).
step_needrestart() {
    step "needrestart: list only"
    if [ ! -e "$DESTDIR/etc/needrestart/needrestart.conf" ]; then
        ok "needrestart is not installed"
        return 0
    fi
    install_file etc/needrestart/conf.d/radar-runner.conf
}

# (b) Journal on the SD card: stays persistent, with explicit limits.
step_journal() {
    step "Journal size limits"
    install_file etc/systemd/journald.conf.d/radar-runner.conf
    if [ "$CHANGED" -eq 1 ]; then
        note "journald reads the new limits at the next reboot"
        need_reboot "journald limits"
    fi
}

# (c) Hardware watchdog through systemd.
step_watchdog() {
    step "Hardware watchdog"
    install_file etc/systemd/system.conf.d/radar-runner-watchdog.conf
    [ "$CHANGED" -eq 0 ] || RELOAD_NEEDED=1
    [ -n "$DESTDIR" ] || [ -e /sys/class/watchdog/watchdog0 ] ||
        note "no watchdog device (/sys/class/watchdog/watchdog0): the setting will have no effect"
}

watchdog_report() {     # after the daemon-reload at the end
    local now sys=/sys/class/watchdog/watchdog0
    now=$(systemctl show -p RuntimeWatchdogUSec --value 2>/dev/null || true)
    if [ "$now" = 30s ]; then
        ok "systemd is using RuntimeWatchdogSec=30s"
    elif [ "$DRY_RUN" -eq 1 ]; then
        note "systemd is using RuntimeWatchdogUSec=${now:-unknown} now; 30s is expected after the daemon-reload, or else after the reboot"
    else
        note "systemd is using RuntimeWatchdogUSec=${now:-unknown}: the watchdog starts at the next reboot"
        need_reboot "hardware watchdog"
    fi
    if [ -z "$DESTDIR" ] && [ -r "$sys/state" ]; then
        note "watchdog device: $(cat "$sys/state"), timeout $(cat "$sys/timeout") s, time left $(cat "$sys/timeleft") s (must not be near 0 when active)"
    fi
}

# (f) The power service, as in power/README.md. Never changes whether it is
# armed, and never restarts or stops it. If the unit is not enabled (a new
# system, or an earlier run that was interrupted), it is enabled and started.
step_power_service() {
    step "Power service unit and config"
    local unit=/etc/systemd/system/radar-power.service config=/etc/radar-runner/power.toml
    local enabled active
    [ "$REPO" = "$UNIT_REPO" ] || [ -n "$DESTDIR" ] ||
        die "the unit file expects this repository at $UNIT_REPO, but it is at $REPO"
    if [ -e "$DESTDIR$config" ]; then
        ok "$config exists and is left as it is ($(grep -E '^armed[[:space:]]*=' "$DESTDIR$config" | head -1 || true))"
    else
        put_file "$REPO/config/power.toml" "$config" 0644
        note "new config is monitor-only (armed = false); arming is a separate, deliberate step"
    fi
    put_file "$REPO/systemd/radar-power.service" "$unit" 0644
    [ "$CHANGED" -eq 0 ] || RELOAD_NEEDED=1
    enabled=$(systemctl is-enabled radar-power.service 2>/dev/null || true)
    active=$(systemctl is-active radar-power.service 2>/dev/null || true)
    if [ "$CHANGED" -eq 1 ] && [ "$active" = active ]; then
        note "the running service keeps the old unit until it is restarted (next reboot)"
        need_reboot "radar-power unit file changed"
    fi
    case "$enabled" in
        enabled)
            if [ "$active" = active ]; then
                ok "radar-power.service is enabled and running (not restarted)"
            else
                note "radar-power.service is enabled but ${active:-not running}: left as it is."
                note "to start it: systemctl start radar-power"
            fi ;;
        masked)
            note "radar-power.service is masked: left as it is" ;;
        *)
            log "           radar-power.service is ${enabled:-not enabled}, ${active:-not running}"
            reload_systemd
            run systemctl enable --now radar-power.service ;;
    esac
}

# ---------------------------------------------------------------- optional steps

# OPTIONAL (--nvme-by-label). Edits /etc/fstab, which is not a drop-in file.
# Only the source field of the /mnt/nvme line is changed, and only if that
# line has "nofail", so that a missing drive cannot stop the boot.
optional_nvme_by_label() {
    step "OPTIONAL: mount /mnt/nvme by label instead of UUID"
    local fstab=$DESTDIR/etc/fstab mnt=/mnt/nvme spec opts dev uuid label count
    count=$(awk -v m="$mnt" '$1 !~ /^#/ && $2 == m' "$fstab" | wc -l)
    [ "$count" -eq 1 ] || die "expected exactly one line for $mnt in /etc/fstab, found $count"
    spec=$(awk -v m="$mnt" '$1 !~ /^#/ && $2 == m {print $1}' "$fstab")
    opts=$(awk -v m="$mnt" '$1 !~ /^#/ && $2 == m {print $4}' "$fstab")
    case "$spec" in
        LABEL=*) ok "$mnt is already mounted by label ($spec)"; return 0 ;;
        UUID=*) ;;
        *) die "the $mnt line in /etc/fstab starts with $spec; expected UUID=..." ;;
    esac
    case ",$opts," in
        *,nofail,*) ;;
        *) die "the $mnt line in /etc/fstab has no 'nofail' option; not touching it" ;;
    esac
    dev=$(findmnt -n -o SOURCE "$mnt" 2>/dev/null || true)
    [ -n "$dev" ] || die "$mnt is not mounted, so the drive and its label cannot be checked"
    uuid=$(lsblk -no UUID "$dev")
    label=$(lsblk -no LABEL "$dev")
    [ "UUID=$uuid" = "$spec" ] || die "the drive mounted at $mnt ($dev, UUID $uuid) is not the one in /etc/fstab ($spec)"
    [ -n "$label" ] || die "$dev has no filesystem label. Set one first, for example: e2label $dev radar-data"
    [[ "$label" =~ ^[A-Za-z0-9_.-]+$ ]] || die "the label '$label' has characters that are unsafe in /etc/fstab"
    count=$(lsblk -rno LABEL | grep -cxF -- "$label" || true)
    [ "$count" -eq 1 ] || die "$count filesystems have the label '$label'; it must be unique"
    change "/etc/fstab: $spec -> LABEL=$label on the $mnt line (backup: /etc/fstab.before-radar-runner)"
    [ "$DRY_RUN" -eq 0 ] || return 0
    TMP_FILE=$(mktemp "$(dirname "$fstab")/.radar-setup.XXXXXX")
    awk -v m="$mnt" -v old="$spec" -v new="LABEL=$label" \
        '$1 == old && $2 == m { sub(/^[^[:space:]]+/, new) } { print }' "$fstab" > "$TMP_FILE"
    [ "$(diff "$fstab" "$TMP_FILE" | grep -c '^>' || true)" -eq 1 ] || die "unexpected result when editing fstab; nothing changed"
    findmnt --verify --tab-file "$TMP_FILE" >/dev/null || die "the new fstab does not pass 'findmnt --verify'; nothing changed"
    chmod 0644 "$TMP_FILE"
    [ -e "$fstab.before-radar-runner" ] || cp -p "$fstab" "$fstab.before-radar-runner"
    sync "$TMP_FILE"
    mv -f "$TMP_FILE" "$fstab"
    TMP_FILE=""
    note "the drive stays mounted; the new line is used from the next reboot"
    need_reboot "fstab: /mnt/nvme by label"
}

# OPTIONAL (--hold-packages). "apt upgrade" then skips these packages until
# "apt-mark unhold". Automatic updates are off anyway; this guards manual ones.
optional_hold_packages() {
    step "OPTIONAL: hold kernel, firmware, bootloader, gpsd and tailscale packages"
    local pkg held
    held=$(apt-mark showhold)
    for pkg in linux-image-raspi linux-headers-raspi linux-raspi "linux-image-$(uname -r)" \
               "linux-modules-$(uname -r)" linux-firmware-raspi rpi-eeprom flash-kernel \
               gpsd gpsd-clients libgps30t64 chrony tailscale; do
        if ! installed "$pkg"; then
            ok "$pkg is not installed"
        elif grep -qxF -- "$pkg" <<<"$held"; then
            ok "$pkg is held ($(dpkg-query -W -f='${Version}' "$pkg"))"
        else
            run apt-mark hold "$pkg"
        fi
    done
}

# ---------------------------------------------------------------- main

preflight
step_directories
step_power_key
step_inhibit_delay
step_auto_updates
step_tailscale
step_needrestart
step_journal
step_watchdog
step_power_service
[ "$OPT_NVME" -eq 0 ] || optional_nvme_by_label
[ "$OPT_HOLD" -eq 0 ] || optional_hold_packages

step "Finish"
# Always, also when this run changed no file: an earlier run may have been
# interrupted after installing a file and before systemd was told about it.
if [ "$RELOAD_NEEDED" -eq 1 ]; then
    reload_systemd
elif [ "$DRY_RUN" -eq 1 ]; then
    note "a real run always ends with: systemctl daemon-reload"
else
    log "  run      systemctl daemon-reload   (every run, in case an earlier run was interrupted)"
    systemctl daemon-reload || die "command failed: systemctl daemon-reload"
fi
watchdog_report

log ""
if [ "$CHANGES" -eq 0 ]; then
    log "setup: nothing to change."
elif [ "$DRY_RUN" -eq 1 ]; then
    log "setup: dry run: $CHANGES change(s) would be made. Nothing was changed."
else
    log "setup: $CHANGES change(s) made."
fi
if [ "${#REBOOT_REASONS[@]}" -gt 0 ]; then
    log "setup: a reboot is needed for:"
    printf '         - %s\n' "${REBOOT_REASONS[@]}"
fi
log "setup: verify with: setup/check.py"
