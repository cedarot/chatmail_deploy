#!/usr/bin/env bash
# Deploy an experimental IP-only Chatmail relay for a private/test network.
# No DNS domain or external TLS certificate is required. Chatmail generates
# and uses a self-signed certificate for IPv4-only relay mode.

set -Eeuo pipefail

PROGRAM=${0##*/}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
INSTALLER=$SCRIPT_DIR/install.sh

MOCK_IP=${MOCK_IP:-}
INSTALL_DIR=${INSTALL_DIR:-/srv/chatmail-relay}
CHATMAIL_IMAGE=${CHATMAIL_IMAGE:-ghcr.m.daocloud.io/chatmail/docker:main}
USE_DAOCLOUD=${USE_DAOCLOUD:-true}
START_SERVICES=${START_SERVICES:-true}
EXTRACT_CONFIGS=${EXTRACT_CONFIGS:-true}
ALLOW_PUBLIC_IP=false
NOTIFICATION_GATEWAY=${NOTIFICATION_GATEWAY:-false}
NOTIFICATION_SNI_WORKAROUND=${NOTIFICATION_SNI_WORKAROUND:-false}
NOTIFICATION_GATEWAY_IMAGE=${NOTIFICATION_GATEWAY_IMAGE:-}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage:
  sudo ./$PROGRAM [--ip PRIVATE_IPV4] [options]

The IP is optional. If omitted, the script detects the IPv4 address used by
the default route. Only private/CGNAT/link-local IPv4 addresses are accepted
unless --allow-public-ip is explicitly provided.

Options:
      --ip IPV4             Private IPv4 address used as the Chatmail identity
      --install-dir DIR     Installation directory (default: /srv/chatmail-relay)
      --image IMAGE         Container image (default: $CHATMAIL_IMAGE)
      --no-daocloud         Disable DaoCloud image acceleration
      --no-start            Prepare and pull without starting Chatmail
      --no-extract          Do not save the generated nginx configuration
      --allow-public-ip     Permit a public IPv4 address in mock/IP-only mode
      --notification-gateway
                            Enable the optional notification gateway (internet required)
      --notification-gateway-image IMAGE
                            Override the Nginx gateway image
      --notification-sni-workaround
                            Permit a proved same-IP SNI compatibility exception
  -h, --help                Show this help

Examples:
  sudo ./$PROGRAM
  sudo ./$PROGRAM --ip 192.168.10.20
  sudo ./$PROGRAM --ip 10.20.0.5 --install-dir /srv/chatmail-mock

Important:
  IP-only mode uses a self-signed TLS certificate and is intended for private
  networks and testing. Clients must allow the self-signed certificate.
EOF
}

is_ipv4() {
  local ip=$1 octet
  local IFS=.
  local -a parts
  read -r -a parts <<<"$ip"
  ((${#parts[@]} == 4)) || return 1
  for octet in "${parts[@]}"; do
    [[ $octet =~ ^[0-9]+$ ]] || return 1
    ((10#$octet >= 0 && 10#$octet <= 255)) || return 1
  done
  [[ $ip != 0.0.0.0 && $ip != 255.255.255.255 ]]
}

is_private_ipv4() {
  local ip=$1 a b
  IFS=. read -r a b _ _ <<<"$ip"
  ((a == 10)) ||
    ((a == 172 && b >= 16 && b <= 31)) ||
    ((a == 192 && b == 168)) ||
    ((a == 100 && b >= 64 && b <= 127)) ||
    ((a == 169 && b == 254))
}

detect_ipv4() {
  local detected
  detected=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i == "src") {print $(i+1); exit}}')
  if [[ -z $detected ]]; then
    detected=$(hostname -I 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i ~ /^[0-9]+\./) {print $i; exit}}')
  fi
  [[ -n $detected ]] || die "could not detect an IPv4 address; provide --ip"
  printf '%s\n' "$detected"
}

while (($#)); do
  case "$1" in
    --ip) MOCK_IP=${2:?missing value for $1}; shift 2 ;;
    --install-dir) INSTALL_DIR=${2:?missing value for $1}; shift 2 ;;
    --image) CHATMAIL_IMAGE=${2:?missing value for $1}; shift 2 ;;
    --no-daocloud) USE_DAOCLOUD=false; shift ;;
    --no-start) START_SERVICES=false; shift ;;
    --no-extract) EXTRACT_CONFIGS=false; shift ;;
    --allow-public-ip) ALLOW_PUBLIC_IP=true; shift ;;
    --notification-gateway) NOTIFICATION_GATEWAY=true; shift ;;
    --notification-gateway-image) NOTIFICATION_GATEWAY_IMAGE=${2:?missing value for $1}; shift 2 ;;
    --notification-sni-workaround) NOTIFICATION_SNI_WORKAROUND=true; shift ;;
    --domain|--cert|--key|--cert-zip|--acme-email)
      die "$1 is not supported in mock/IP-only mode"
      ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -x $INSTALLER ]] || die "installer is missing or not executable: $INSTALLER"

if [[ -z $MOCK_IP ]]; then
  MOCK_IP=$(detect_ipv4)
  printf 'Detected IPv4 address: %s\n' "$MOCK_IP"
fi
is_ipv4 "$MOCK_IP" || die "invalid IPv4 address: $MOCK_IP"
if ! is_private_ipv4 "$MOCK_IP" && [[ $ALLOW_PUBLIC_IP != true ]]; then
  die "$MOCK_IP is not a private/CGNAT/link-local address; use --allow-public-ip to override"
fi

installer_args=(
  --domain "$MOCK_IP"
  --install-dir "$INSTALL_DIR"
  --image "$CHATMAIL_IMAGE"
  --disable-ipv6
)
[[ $USE_DAOCLOUD == true ]] || installer_args+=(--no-daocloud)
[[ $START_SERVICES == true ]] || installer_args+=(--no-start)
[[ $EXTRACT_CONFIGS == true ]] || installer_args+=(--no-extract)
[[ $NOTIFICATION_GATEWAY != true ]] || installer_args+=(--notification-gateway)
[[ $NOTIFICATION_SNI_WORKAROUND != true ]] || installer_args+=(--notification-sni-workaround)
[[ -z $NOTIFICATION_GATEWAY_IMAGE ]] || installer_args+=(--notification-gateway-image "$NOTIFICATION_GATEWAY_IMAGE")

cat <<EOF
Starting experimental Chatmail IP-only deployment:
  IPv4:          $MOCK_IP
  Install dir:   $INSTALL_DIR
  Image:         $CHATMAIL_IMAGE
  TLS:           self-signed (managed by Chatmail)
  DNS checks:    skipped automatically for an IPv4 identity
  DaoCloud:      $USE_DAOCLOUD
EOF

exec "$INSTALLER" "${installer_args[@]}"
