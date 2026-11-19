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
# It never touches the network configuration or SSH, never restarts or stops a
# running service of this project, and never reboots. The only Tailscale
# change is switching off its automatic self-update. Changes to logind and
# journald take effect at the next reboot.
#
# One step installs packages (the last one, so that everything else is done
# when there is no internet): chrony and fake-hwclock, which replace
# systemd-timesyncd. It refuses if apt would remove or upgrade anything else,
# and puts systemd-timesyncd back if chrony cannot be made to run. That step
# restarts chrony, and the gpsd step restarts gpsd, when their settings change.
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
UNIT_REPO=/home/englacial/englacial-radar-runner     # WorkingDirectory in the unit files under systemd/
DATA_MOUNT=/mnt/nvme                                 # telemetry.data_mount in config/radar-runner.yaml
DATA_ROOT=/mnt/nvme/radar-runner                     # telemetry.data_root
DESTDIR=${DESTDIR:-}
OLA_PORT_DEFAULT=/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0     # telemetry.ola.port
if [ -n "$DESTDIR" ]; then OLA_PORT=${OLA_PORT:-$OLA_PORT_DEFAULT}; else OLA_PORT=$OLA_PORT_DEFAULT; fi   # tests point it elsewhere
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

usage() { sed -n '2,31p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

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
    for tool in systemctl systemd-tmpfiles busctl dpkg-query apt-get findmnt ps find readlink cmp diff stat install mktemp; do
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

# (h) gpsd: owns the receiver by name and keeps reading it without clients
# (chrony needs that). Ubuntu's hotplug path (udev, gpsdctl) is left as it is.
step_gpsd() {
    step "gpsd: owns the GPS receiver and runs without clients"
    local enabled active args auto
    if ! installed gpsd; then
        note "gpsd is not installed: nothing done. Install gpsd and gpsd-clients, then run setup again."
        return 0
    fi
    local changed=0 device_unit wants
    install_file etc/systemd/system/gpsd.service.d/radar-runner.conf
    [ "$CHANGED" -eq 0 ] || { RELOAD_NEEDED=1; changed=1; }
    # Ubuntu's hotplug path off: with both, the receiver was opened twice at boot (see the file).
    install_file "etc/systemd/system/gpsdctl@.service.d/radar-runner.conf"
    [ "$CHANGED" -eq 0 ] || { RELOAD_NEEDED=1; changed=1; }
    # The receiver's device unit starts gpsd when the receiver appears.
    device_unit=$(sed -n 's/^BindsTo=//p' "$FILES/etc/systemd/system/gpsd.service.d/radar-runner.conf")
    wants="/etc/systemd/system/$device_unit.wants/gpsd.service"
    if [ -L "$DESTDIR$wants" ]; then
        ok "the receiver's device unit wants gpsd.service"
    else
        # A plain link: "systemctl add-wants" refuses device units, which have no unit file.
        run install -d -m 0755 "$DESTDIR/etc/systemd/system/$device_unit.wants"
        run ln -sfn /usr/lib/systemd/system/gpsd.service "$DESTDIR$wants"
        RELOAD_NEEDED=1; changed=1
    fi
    auto=$(sed -n 's/^USBAUTO=//p' "$DESTDIR/etc/default/gpsd" 2>/dev/null | tr -d '"' | tail -1)
    if [ "$auto" = true ]; then
        ok "/etc/default/gpsd has USBAUTO=\"true\" (a replugged receiver is added by udev and gpsdctl)"
    else
        note "/etc/default/gpsd has USBAUTO=${auto:-unset}: a receiver plugged in after gpsd started will not be"
        note "picked up. The package default is USBAUTO=\"true\"; this script does not edit that file."
    fi
    enabled=$(systemctl is-enabled gpsd.service 2>/dev/null || true)
    if [ "$enabled" = enabled ]; then
        ok "gpsd.service is enabled (runs from boot)"
    else
        log "           gpsd.service is ${enabled:-not enabled}"
        reload_systemd
        run systemctl enable gpsd.service
    fi
    active=$(systemctl is-active gpsd.service 2>/dev/null || true)
    args=$(ps -C gpsd -o args= 2>/dev/null || true)
    case "$changed:$active:$args" in
        0:active:*" -n "*) ok "gpsd is running with -n ($args)" ;;
        *)
            log "           gpsd is ${active:-not running}${args:+: $args}"
            reload_systemd
            # Its clients (the telemetry service, chrony) reconnect by themselves.
            run systemctl restart gpsd.service ;;
    esac
    # Once per boot: check the receiver's UART1 NMEA setting and correct it (setup/gps_receiver.py).
    put_file "$REPO/systemd/radar-gps-receiver.service" /etc/systemd/system/radar-gps-receiver.service 0644
    [ "$CHANGED" -eq 0 ] || RELOAD_NEEDED=1
    if [ "$(systemctl is-enabled radar-gps-receiver.service 2>/dev/null || true)" = enabled ]; then
        ok "radar-gps-receiver.service is enabled (runs once per boot, after gpsd)"
    else
        reload_systemd
        run systemctl enable radar-gps-receiver.service
    fi
}

ola_port_holder() {     # "PID command" of a process that has the OLA's serial port open; nothing if none
    local dev link pid
    dev=$(readlink -f "$OLA_PORT" 2>/dev/null || true)
    [ -n "$dev" ] && [ -e "$dev" ] || return 0
    link=$(find /proc/[0-9]*/fd -lname "$dev" 2>/dev/null | head -1 || true)
    [ -n "$link" ] || return 0
    pid=${link#/proc/}
    pid=${pid%%/*}
    printf '%s %s\n' "$pid" "$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
}

# (i) The telemetry service: the unit, and its directory on the data drive.
# Like the power service, a running service is never restarted or stopped.
step_telemetry_service() {
    step "Telemetry service unit and data directory"
    local unit=/etc/systemd/system/radar-telemetry.service enabled active have holder
    [ "$REPO" = "$UNIT_REPO" ] || [ -n "$DESTDIR" ] ||
        die "the unit file expects this repository at $UNIT_REPO, but it is at $REPO"
    [ -n "$DESTDIR" ] || [ -x "$REPO/.venv/bin/python" ] ||
        die "the Python environment is missing: run setup/build_venv.sh as $SERVICE_USER first"
    # The data drive's top directory belongs to root; the service writes as $SERVICE_USER.
    if [ -z "$(findmnt -n -o SOURCE "$DATA_MOUNT" 2>/dev/null || true)" ]; then
        note "$DATA_MOUNT is not mounted: $DATA_ROOT cannot be made now. Until it exists the sensor logs"
        note "go to the fallback directory on the SD card. Run setup again when the drive is there."
    else
        have=$(stat -c '%U:%G' "$DESTDIR$DATA_ROOT" 2>/dev/null || echo missing)
        if [ "$have" = "$SERVICE_USER:$SERVICE_USER" ] || { [ -n "$DESTDIR" ] && [ "$have" != missing ]; }; then
            ok "$DATA_ROOT belongs to $SERVICE_USER"
        elif [ "$have" = missing ]; then
            if [ -n "$DESTDIR" ]; then
                run install -d -m 0755 "$DESTDIR$DATA_ROOT"
            else
                run install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_ROOT"
            fi
        else
            log "           $DATA_ROOT belongs to $have"
            run chown "$SERVICE_USER:$SERVICE_USER" "$DATA_ROOT"
        fi
    fi
    put_file "$REPO/systemd/radar-telemetry.service" "$unit" 0644
    [ "$CHANGED" -eq 0 ] || RELOAD_NEEDED=1
    enabled=$(systemctl is-enabled radar-telemetry.service 2>/dev/null || true)
    active=$(systemctl is-active radar-telemetry.service 2>/dev/null || true)
    if [ "$CHANGED" -eq 1 ] && [ "$active" = active ]; then
        note "the running service keeps the old unit until it is restarted: systemctl restart radar-telemetry"
    fi
    case "$enabled" in
        enabled)
            if [ "$active" = active ]; then
                ok "radar-telemetry.service is enabled and running (not restarted)"
            else
                note "radar-telemetry.service is enabled but ${active:-not running}: left as it is."
                note "to start it: systemctl start radar-telemetry"
            fi ;;
        masked)
            note "radar-telemetry.service is masked: left as it is" ;;
        *)
            log "           radar-telemetry.service is ${enabled:-not enabled}, ${active:-not running}"
            # Starting it opens the OLA's port, which resets the OLA under whatever is using it now
            # (a bench run of pa_guard.py watching the amplifier): never do that behind its back.
            holder=$(ola_port_holder)
            [ -z "$holder" ] || die "the OLA's serial port ($OLA_PORT) is in use by process $holder
       The telemetry service was not enabled or started: starting it would reset the OLA under that program.
       Stop that program first (a bench run: let it finish, or stop it as radar-testbench describes), then run setup again."
            note "it takes over the OLA's serial port: the bench tools pa_guard.py and pa_run.py cannot run"
            note "while it does (radar_runner/telemetry/README.md: \"Bench work\")"
            reload_systemd
            run systemctl enable --now radar-telemetry.service ;;
    esac
}

# (k) The radar manager (stage 4): its unit and the radarctl command.
# The unit is installed but NEVER enabled or started here: the first start is
# done by hand with the radar under supervision (radar_runner/manager/README.md,
# "First start with the real radar"). A running manager is never restarted or
# stopped. The power service's hooks are printed, not applied.
step_manager() {
    step "Radar manager unit and radarctl (installed; not enabled, not started)"
    local unit=/etc/systemd/system/radar-manager.service link=/usr/local/bin/radarctl
    local target=$UNIT_REPO/bin/radarctl enabled active have
    [ "$REPO" = "$UNIT_REPO" ] || [ -n "$DESTDIR" ] ||
        die "the unit file expects this repository at $UNIT_REPO, but it is at $REPO"
    put_file "$REPO/systemd/radar-manager.service" "$unit" 0644
    [ "$CHANGED" -eq 0 ] || RELOAD_NEEDED=1
    enabled=$(systemctl is-enabled radar-manager.service 2>/dev/null || true)
    active=$(systemctl is-active radar-manager.service 2>/dev/null || true)
    if [ "$CHANGED" -eq 1 ] && [ "$active" = active ]; then
        note "the running manager keeps the old unit until it is restarted: systemctl restart radar-manager"
        note "(a restart stops a running radar)"
    fi
    have=$(readlink "$DESTDIR$link" 2>/dev/null || true)
    if [ "$have" = "$target" ]; then
        ok "$link points to $target"
    else
        [ -z "$have" ] || log "           $link points to $have"
        [ -z "$have" ] && [ -e "$DESTDIR$link" ] && die "$link exists and is not a link: remove it or move it away"
        change "link $link -> $target"
        if [ "$DRY_RUN" -eq 0 ]; then
            install -d -m 0755 "$(dirname "$DESTDIR$link")"
            ln -sfn "$target" "$DESTDIR$link"
        fi
    fi
    case "$enabled/$active" in
        enabled/active) ok "radar-manager.service is enabled and running (not restarted)" ;;
        *) note "radar-manager.service is ${enabled:-not enabled}, ${active:-not running}: left as it is."
           note "It is NOT enabled and NOT started by this script. The first start is done by hand, with the"
           note "radar under supervision: radar_runner/manager/README.md, \"First start with the real radar\"." ;;
    esac
    note "The power service's hooks are not changed by this script. When the manager has passed its first"
    note "start, put these lines under [hooks] in /etc/radar-runner/power.toml and restart radar-power:"
    cat <<EOF
             stop_radar = ["/usr/bin/setpriv", "--reuid=$SERVICE_USER", "--regid=$SERVICE_USER", "--init-groups", "--reset-env",
                           "/usr/bin/python3", "-B", "-E", "-s", "$UNIT_REPO/radar_runner/manager/radarctl.py",
                           "stop", "--interrupt", "--wait", "--timeout", "4"]
             stop_radar_timeout_s = 5.0
             amp_safe = ["/usr/bin/setpriv", "--reuid=$SERVICE_USER", "--regid=$SERVICE_USER", "--init-groups", "--reset-env",
                         "$UNIT_REPO/tools/sdr_gpio_safe/amp_safe",
                         "--timeout", "20", "--state-file", "/var/lib/radar-runner/user/sdr-state"]
             amp_safe_timeout_s = 22.0
EOF
    note "(each hook's limit is longer than its command's own --timeout; a first SDR contact took 17 s)"
}

# (j) Time. chrony takes time from gpsd and, when the internet is up, from
# NTP; fake-hwclock saves the clock every minute and at shutdown and restores
# it at boot (the Pi's clock has no battery). Both replace systemd-timesyncd,
# which apt removes when chrony is installed.
apt_get() {     # never interactive; needrestart's hook is switched off for this call (it is list-only anyway)
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 NEEDRESTART_MODE=l LC_ALL=C apt-get "$@"
}

restore_timesyncd() {   # leave a time daemon behind when chrony cannot be made to run
    log "  RECOVER  $1"
    log "  RECOVER  putting systemd-timesyncd back (this removes chrony)"
    apt_get -y install systemd-timesyncd 2>&1 | sed 's/^/           /' || true
    systemctl start systemd-timesyncd.service 2>/dev/null || true
    if [ "$(systemctl is-active systemd-timesyncd.service 2>/dev/null || true)" = active ]; then
        die "$1. systemd-timesyncd is installed and running again, as before this step."
    fi
    die "$1, and systemd-timesyncd could not be put back: NO TIME DAEMON IS RUNNING.
       By hand, in this order:   sudo dpkg --configure -a
                                 systemctl status chrony        (if it runs now, run setup again and stop here)
                                 sudo apt-get install systemd-timesyncd"
}

install_time_packages() {
    local want=("$@") plan verb pkg rest bad="" line
    # What would apt do? (A simulation: nothing is changed, and it needs no root.)
    if ! plan=$(apt_get -s install --no-install-recommends "${want[@]}" 2>&1); then
        if grep -q 'dpkg --configure -a' <<<"$plan"; then
            die "an earlier package installation was interrupted (a power cut, or this step was stopped half way):
$(tail -3 <<<"$plan" | sed 's/^/       /')
       Finish it with:   sudo dpkg --configure -a
       then run setup again. Check the time daemon meanwhile: systemctl status chrony systemd-timesyncd"
        fi
        die "apt cannot work out how to install ${want[*]}:
$(tail -5 <<<"$plan" | sed 's/^/       /')
       Nothing was changed. If it says a package cannot be found: with internet, apt-get update, then run setup again."
    fi
    while read -r verb pkg rest; do
        case "$verb" in
            Remv) [ "$pkg" = systemd-timesyncd ] || bad="$bad remove:$pkg" ;;
            Inst) case "$rest" in "["*) bad="$bad upgrade:$pkg" ;; esac     # "Inst name [old] (new ...)" is an upgrade
                  log "           apt would install $pkg $(tr -d "()" <<<"${rest%% *}")" ;;
        esac
    done <<<"$plan"
    grep -q '^Remv systemd-timesyncd' <<<"$plan" && log "           apt would remove systemd-timesyncd (replaced by chrony)"
    [ -z "$bad" ] || die "apt would do more than install ${want[*]} in place of systemd-timesyncd:$bad
       Nothing was changed. Sort that out by hand first."
    change "run: apt-get install --no-install-recommends ${want[*]}"
    [ "$DRY_RUN" -eq 0 ] || return 0
    # Everything is downloaded before anything changes, including the package to go back to.
    apt_get -y --download-only install --no-install-recommends "${want[@]}" 2>&1 | sed 's/^/           /' ||
        die "cannot download ${want[*]} (no internet?). Nothing was changed; systemd-timesyncd stays."
    if installed systemd-timesyncd; then
        apt_get -y --download-only --reinstall install systemd-timesyncd 2>&1 | sed 's/^/           /' ||
            die "cannot download systemd-timesyncd, which is the way back if chrony fails. Nothing was changed."
    fi
    apt_get -y install --no-install-recommends "${want[@]}" 2>&1 | sed 's/^/           /' ||
        restore_timesyncd "apt-get install ${want[*]} failed"
    for pkg in "${want[@]}"; do
        installed "$pkg" || restore_timesyncd "$pkg is not installed after apt-get install"
    done
}

step_time() {
    step "Time: chrony (GPS and NTP) and fake-hwclock, in place of systemd-timesyncd"
    local changed=0 want=() fresh=0 unit
    # The settings go in first, so that the packages start with them.
    for file in etc/chrony/conf.d/radar-runner.conf etc/systemd/system/chrony.service.d/radar-runner.conf \
                etc/systemd/system/fake-hwclock-save.timer.d/radar-runner.conf; do
        install_file "$file"
        [ "$CHANGED" -eq 0 ] || { changed=1; RELOAD_NEEDED=1; }
    done
    installed chrony || want+=(chrony)
    installed fake-hwclock || want+=(fake-hwclock)
    if [ "${#want[@]}" -eq 0 ]; then
        ok "chrony and fake-hwclock are installed"
    else
        reload_systemd
        install_time_packages "${want[@]}"
        fresh=1
        [ "$DRY_RUN" -eq 0 ] || return 0
    fi
    if [ "$changed" -eq 1 ] && [ "$fresh" -eq 0 ]; then
        reload_systemd
        run systemctl restart chrony.service
        run systemctl restart fake-hwclock-save.timer
        [ "$DRY_RUN" -eq 0 ] || return 0
    fi
    # chrony must be running now; if not, start it once, and if that fails go back.
    if [ "$(systemctl is-active chrony.service 2>/dev/null || true)" != active ]; then
        log "           chrony.service is not running: starting it"
        systemctl start chrony.service 2>/dev/null || true
        [ "$(systemctl is-active chrony.service 2>/dev/null || true)" = active ] ||
            restore_timesyncd "chrony does not start (journalctl -u chrony)"
    fi
    ok "chrony.service is running"
    for unit in chrony.service fake-hwclock-load.service fake-hwclock-save.service fake-hwclock-save.timer; do
        [ "$(systemctl is-enabled "$unit" 2>/dev/null || true)" = enabled ] || run systemctl enable "$unit"
    done
    if [ "$(systemctl is-active fake-hwclock-save.timer 2>/dev/null || true)" = active ]; then
        ok "fake-hwclock saves the clock every minute and at shutdown"
    else
        run systemctl start fake-hwclock-save.timer
    fi
    note "chrony's sources: chronyc sources   (GPS appears once gpsd has a position fix)"
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
               gpsd gpsd-clients libgps30t64 chrony fake-hwclock tailscale; do
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
step_gpsd
step_telemetry_service
step_manager
[ "$OPT_NVME" -eq 0 ] || optional_nvme_by_label
[ "$OPT_HOLD" -eq 0 ] || optional_hold_packages
step_time           # last: the only step that needs the internet

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
