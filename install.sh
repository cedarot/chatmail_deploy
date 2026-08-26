#!/usr/bin/env bash
# Install a Dockerized Chatmail relay on a fresh Debian/Ubuntu or RPM-family server.
# Upstream: https://github.com/chatmail/docker

set -Eeuo pipefail
umask 027

PROGRAM=${0##*/}
DEFAULT_INSTALL_DIR=/srv/chatmail-relay
DEFAULT_IMAGE=ghcr.m.daocloud.io/chatmail/docker:main
COMPOSE_URL=https://raw.githubusercontent.com/chatmail/docker/main/docker-compose.yaml
DAOCLOUD_REGISTRY_MIRROR=${DAOCLOUD_REGISTRY_MIRROR:-https://docker.m.daocloud.io}

MAIL_DOMAIN=${MAIL_DOMAIN:-}
ACME_EMAIL=${ACME_EMAIL:-}
INSTALL_DIR=${INSTALL_DIR:-$DEFAULT_INSTALL_DIR}
CHATMAIL_IMAGE=${CHATMAIL_IMAGE:-$DEFAULT_IMAGE}
CERT_FILE=${CERT_FILE:-}
KEY_FILE=${KEY_FILE:-}
CERT_ZIP=${CERT_ZIP:-}
DISABLE_IPV6=${DISABLE_IPV6:-false}
START_SERVICES=${START_SERVICES:-true}
EXTRACT_CONFIGS=${EXTRACT_CONFIGS:-true}
USE_DAOCLOUD=${USE_DAOCLOUD:-true}

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage:
  sudo ./$PROGRAM --domain chat.example.org [options]

Required:
  -d, --domain DOMAIN       Fully qualified Chatmail domain

Certificate options (choose one method):
      --cert FILE           PEM/full-chain certificate file
      --key FILE            Unencrypted PEM private-key file
      --cert-zip FILE       Nginx certificate ZIP containing a PEM/CRT and KEY

Other options:
      --acme-email EMAIL    ACME contact address when no external cert is given
      --install-dir DIR     Installation directory (default: $DEFAULT_INSTALL_DIR)
      --image IMAGE         Container image (default: $DEFAULT_IMAGE)
      --no-daocloud         Disable DaoCloud mirror configuration
      --disable-ipv6        Configure Chatmail without IPv6
      --no-start            Prepare files and pull the image without starting
      --no-extract          Do not extract generated config files after startup
  -h, --help                Show this help

Environment variables with the same names are also accepted, for example:
  MAIL_DOMAIN=chat.example.org CERT_ZIP=/tmp/cert.zip sudo -E ./$PROGRAM

Examples:
  sudo ./$PROGRAM -d chat.example.org --cert-zip /tmp/example_nginx.zip
  sudo ./$PROGRAM -d chat.example.org --cert /root/fullchain.pem --key /root/privkey.pem
  sudo ./$PROGRAM -d chat.example.org --acme-email admin@example.org
EOF
}

while (($#)); do
  case "$1" in
    -d|--domain) MAIL_DOMAIN=${2:?missing value for $1}; shift 2 ;;
    --acme-email) ACME_EMAIL=${2:?missing value for $1}; shift 2 ;;
    --install-dir) INSTALL_DIR=${2:?missing value for $1}; shift 2 ;;
    --image) CHATMAIL_IMAGE=${2:?missing value for $1}; shift 2 ;;
    --no-daocloud) USE_DAOCLOUD=false; shift ;;
    --cert) CERT_FILE=${2:?missing value for $1}; shift 2 ;;
    --key) KEY_FILE=${2:?missing value for $1}; shift 2 ;;
    --cert-zip) CERT_ZIP=${2:?missing value for $1}; shift 2 ;;
    --disable-ipv6) DISABLE_IPV6=true; shift ;;
    --no-start) START_SERVICES=false; shift ;;
    --no-extract) EXTRACT_CONFIGS=false; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "run this script as root"
[[ -n $MAIL_DOMAIN ]] || { usage >&2; die "--domain is required"; }
[[ $MAIL_DOMAIN =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || die "invalid domain: $MAIL_DOMAIN"
[[ $MAIL_DOMAIN == *.* ]] || die "MAIL_DOMAIN must be a fully qualified domain"
MAIL_DOMAIN=${MAIL_DOMAIN,,}

for bool_value in "$DISABLE_IPV6" "$START_SERVICES" "$EXTRACT_CONFIGS" "$USE_DAOCLOUD"; do
  [[ $bool_value == true || $bool_value == false ]] || die "boolean values must be true or false"
done

if [[ -n $CERT_ZIP && ( -n $CERT_FILE || -n $KEY_FILE ) ]]; then
  die "use either --cert-zip or --cert/--key, not both"
fi
if [[ -n $CERT_FILE || -n $KEY_FILE ]]; then
  [[ -n $CERT_FILE && -n $KEY_FILE ]] || die "--cert and --key must be supplied together"
fi

install_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    log "Docker and Compose v2 are already installed"
    return
  fi

  [[ -r /etc/os-release ]] || die "cannot identify the operating system"
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}" in
    debian|ubuntu)
      log "Installing Docker Engine from Docker's official APT repository"
      apt-get update
      DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl gnupg openssl unzip jq iproute2
      install -d -m 0755 /etc/apt/keyrings
      curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
      chmod 0644 /etc/apt/keyrings/docker.asc
      local arch codename
      arch=$(dpkg --print-architecture)
      codename=${VERSION_CODENAME:?VERSION_CODENAME is unavailable}
      printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' \
        "$arch" "$ID" "$codename" >/etc/apt/sources.list.d/docker.list
      apt-get update
      DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
      ;;
    rhel)
      install_docker_rpm rhel
      ;;
    fedora)
      install_docker_rpm fedora
      ;;
    centos|rocky|almalinux|ol)
      install_docker_rpm centos
      ;;
    *) die "automatic Docker installation supports Debian, Ubuntu, RHEL, CentOS, Rocky, AlmaLinux, Oracle Linux and Fedora (found: ${ID:-unknown})" ;;
  esac
  systemctl enable --now docker
  docker compose version >/dev/null
}

install_docker_rpm() {
  local repo_family=$1
  local pkg_manager
  if command -v dnf >/dev/null 2>&1; then
    pkg_manager=dnf
  elif command -v yum >/dev/null 2>&1; then
    pkg_manager=yum
  else
    die "neither dnf nor yum is available"
  fi

  log "Installing Docker Engine from Docker's official $repo_family RPM repository"
  "$pkg_manager" install -y ca-certificates curl openssl unzip jq iproute
  install -d -m 0755 /etc/yum.repos.d
  curl -fsSL "https://download.docker.com/linux/$repo_family/docker-ce.repo" \
    -o /etc/yum.repos.d/docker-ce.repo
  "$pkg_manager" install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}

ensure_tools() {
  local missing=()
  local tool
  for tool in curl openssl unzip jq ss; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
  done
  if ((${#missing[@]})); then
    if command -v apt-get >/dev/null 2>&1; then
      apt-get update
      DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl openssl unzip jq iproute2
    elif command -v dnf >/dev/null 2>&1; then
      dnf install -y ca-certificates curl openssl unzip jq iproute
    elif command -v yum >/dev/null 2>&1; then
      yum install -y ca-certificates curl openssl unzip jq iproute
    else
      die "cannot install required commands: ${missing[*]}"
    fi
    missing=()
    for tool in curl openssl unzip jq ss; do
      command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
    done
    ((${#missing[@]} == 0)) || die "required commands are still missing: ${missing[*]}"
  fi
}

configure_docker_daocloud() {
  [[ $USE_DAOCLOUD == true ]] || return

  local daemon_file=/etc/docker/daemon.json
  local stage
  install -d -m 0755 /etc/docker
  stage=$(mktemp /tmp/docker-daemon.XXXXXX.json)

  if [[ -s $daemon_file ]]; then
    jq empty "$daemon_file" >/dev/null 2>&1 || die "$daemon_file is not valid JSON"
    jq --arg mirror "$DAOCLOUD_REGISTRY_MIRROR" \
      '. + {"registry-mirrors": (((.["registry-mirrors"] // []) + [$mirror]) | unique)}' \
      "$daemon_file" >"$stage"
    cp -a "$daemon_file" "$daemon_file.bak.$(date +%Y%m%d%H%M%S)"
  else
    jq -n --arg mirror "$DAOCLOUD_REGISTRY_MIRROR" '{"registry-mirrors": [$mirror]}' >"$stage"
  fi

  install -o root -g root -m 0644 "$stage" "$daemon_file"
  rm -f -- "$stage"
  if command -v dockerd >/dev/null 2>&1 && dockerd --help 2>&1 | grep -q -- '--validate'; then
    dockerd --validate --config-file="$daemon_file" >/dev/null
  fi
  systemctl restart docker
  log "Configured DaoCloud Docker Hub mirror: $DAOCLOUD_REGISTRY_MIRROR"
}

configure_kernel() {
  log "Configuring host inotify limits"
  cat >/etc/sysctl.d/99-chatmail-inotify.conf <<'EOF'
fs.inotify.max_user_instances=65536
fs.inotify.max_user_watches=65536
EOF
  sysctl --system >/dev/null
}

check_ports() {
  local occupied
  occupied=$(ss -H -lntup 2>/dev/null | awk '$5 ~ /:(25|53|80|143|443|465|587|993|3340|8443)$/ {print}' || true)
  if [[ -n $occupied ]]; then
    printf 'WARNING: Chatmail ports are already in use:\n%s\n' "$occupied" >&2
    printf 'The container uses host networking and may fail to start.\n' >&2
  fi
}

prepare_certificate() {
  local cert_dir=$INSTALL_DIR/data/certs/external
  local stage cert_src key_src
  mkdir -p "$cert_dir"

  if [[ -z $CERT_ZIP && -z $CERT_FILE ]]; then
    TLS_MODE=acme
    return
  fi

  stage=$(mktemp -d /tmp/chatmail-cert.XXXXXX)

  if [[ -n $CERT_ZIP ]]; then
    [[ -r $CERT_ZIP ]] || die "cannot read certificate ZIP: $CERT_ZIP"
    unzip -Z1 "$CERT_ZIP" | grep -Ev '(^|/)\.\.?(/|$)' >"$stage/files"
    cert_src=$(grep -Ei '\.(pem|crt|cer)$' "$stage/files" | head -n1 || true)
    key_src=$(grep -Ei '\.key$' "$stage/files" | head -n1 || true)
    [[ -n $cert_src && -n $key_src ]] || die "ZIP must contain a PEM/CRT certificate and a KEY file"
    unzip -p "$CERT_ZIP" "$cert_src" >"$stage/fullchain.pem"
    unzip -p "$CERT_ZIP" "$key_src" >"$stage/privkey.pem"
  else
    [[ -r $CERT_FILE ]] || die "cannot read certificate: $CERT_FILE"
    [[ -r $KEY_FILE ]] || die "cannot read private key: $KEY_FILE"
    cp "$CERT_FILE" "$stage/fullchain.pem"
    cp "$KEY_FILE" "$stage/privkey.pem"
  fi

  openssl x509 -in "$stage/fullchain.pem" -noout >/dev/null || die "invalid X.509 certificate"
  openssl pkey -in "$stage/privkey.pem" -passin pass: -noout >/dev/null 2>&1 || \
    die "invalid or encrypted private key; Chatmail requires an unencrypted PEM key"
  openssl x509 -in "$stage/fullchain.pem" -checkhost "$MAIL_DOMAIN" -noout >/dev/null 2>&1 || \
    die "certificate does not cover $MAIL_DOMAIN"
  openssl x509 -in "$stage/fullchain.pem" -checkend 0 -noout >/dev/null || die "certificate is expired"

  local cert_pub key_pub
  cert_pub=$(openssl x509 -in "$stage/fullchain.pem" -pubkey -noout | openssl sha256)
  key_pub=$(openssl pkey -in "$stage/privkey.pem" -pubout 2>/dev/null | openssl sha256)
  [[ $cert_pub == "$key_pub" ]] || die "certificate and private key do not match"

  install -o root -g root -m 0644 "$stage/fullchain.pem" "$cert_dir/fullchain.pem"
  install -o root -g root -m 0600 "$stage/privkey.pem" "$cert_dir/privkey.pem"
  rm -rf -- "$stage"
  TLS_MODE=external
  log "External certificate installed in $cert_dir"
  openssl x509 -in "$cert_dir/fullchain.pem" -noout -subject -issuer -dates
}

generate_chatmail_config() {
  local config=$INSTALL_DIR/config/chatmail.ini
  if [[ -s $config ]]; then
    log "Keeping existing $config"
    return
  fi

  log "Generating an editable host-side chatmail.ini"
  docker run --rm \
    --entrypoint /opt/cmdeploy/bin/cmdeploy \
    -v "$INSTALL_DIR/config:/config" \
    "$CHATMAIL_IMAGE" init --config /config/chatmail.ini "$MAIL_DOMAIN"

  [[ -s $config ]] || die "the image did not generate $config"
  if [[ $DISABLE_IPV6 == true ]]; then
    sed -i 's/^disable_ipv6 = False/disable_ipv6 = True/' "$config"
  fi
  if [[ $TLS_MODE == external ]] && ! grep -q '^tls_external_cert_and_key' "$config"; then
    printf '\ntls_external_cert_and_key = %s %s\n' \
      /var/lib/acme/external/fullchain.pem /var/lib/acme/external/privkey.pem >>"$config"
  fi
  if [[ -n $ACME_EMAIL ]] && ! grep -q '^acme_email' "$config"; then
    printf '\nacme_email = %s\n' "$ACME_EMAIL" >>"$config"
  fi
  chmod 0640 "$config"
}

write_env() {
  {
    printf 'MAIL_DOMAIN=%s\n' "$MAIL_DOMAIN"
    printf 'CHATMAIL_IMAGE=%s\n' "$CHATMAIL_IMAGE"
    [[ -n $ACME_EMAIL ]] && printf 'ACME_EMAIL=%s\n' "$ACME_EMAIL"
  } >"$INSTALL_DIR/.env"
  chmod 0600 "$INSTALL_DIR/.env"

  {
    printf 'MAIL_DOMAIN=%q\n' "$MAIL_DOMAIN"
    printf 'INSTALL_DIR=%q\n' "$INSTALL_DIR"
    printf 'CHATMAIL_IMAGE=%q\n' "$CHATMAIL_IMAGE"
    printf 'USE_DAOCLOUD=%q\n' "$USE_DAOCLOUD"
    printf 'DAOCLOUD_REGISTRY_MIRROR=%q\n' "$DAOCLOUD_REGISTRY_MIRROR"
    printf 'TLS_MODE=%q\n' "$TLS_MODE"
    printf 'CERTIFICATE_HOST_PATH=%q\n' "$INSTALL_DIR/data/certs/external/fullchain.pem"
    printf 'PRIVATE_KEY_HOST_PATH=%q\n' "$INSTALL_DIR/data/certs/external/privkey.pem"
    printf 'CERTIFICATE_CONTAINER_PATH=%q\n' /var/lib/acme/external/fullchain.pem
    printf 'PRIVATE_KEY_CONTAINER_PATH=%q\n' /var/lib/acme/external/privkey.pem
  } >"$INSTALL_DIR/install.conf"
  chmod 0600 "$INSTALL_DIR/install.conf"
}

write_override() {
  local with_config=${1:-false}
  cat >"$INSTALL_DIR/docker-compose.override.yaml" <<EOF
services:
  chatmail:
    image: \${CHATMAIL_IMAGE:-$DEFAULT_IMAGE}
    volumes:
      - ./data/mail:/home/vmail
      - ./data/dkim:/etc/dkimkeys
      - ./data/certs:/var/lib/acme
      - ./data/www:/opt/chatmail-www
EOF
  if [[ $with_config == true ]]; then
    printf '      - ./config/chatmail.ini:/etc/chatmail/chatmail.ini\n' >>"$INSTALL_DIR/docker-compose.override.yaml"
  fi
  if [[ $TLS_MODE == external ]]; then
    cat >>"$INSTALL_DIR/docker-compose.override.yaml" <<'EOF'
    environment:
      TLS_EXTERNAL_CERT_AND_KEY: "/var/lib/acme/external/fullchain.pem /var/lib/acme/external/privkey.pem"
EOF
  fi
}

extract_configs() {
  local deadline=$((SECONDS + 900))
  log "Waiting for Chatmail's health check to pass"
  while ((SECONDS < deadline)); do
    if [[ $(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' chatmail 2>/dev/null || true) == healthy ]]; then
      if docker exec chatmail test -s /etc/nginx/nginx.conf 2>/dev/null; then
        docker cp chatmail:/etc/nginx/nginx.conf "$INSTALL_DIR/config/nginx.generated.conf"
        chmod 0640 "$INSTALL_DIR/config/nginx.generated.conf"
      fi
      log "Saved the generated nginx configuration as $INSTALL_DIR/config/nginx.generated.conf"
      return
    fi
    sleep 5
  done
  printf 'WARNING: Chatmail did not become healthy within 15 minutes; inspect: docker logs chatmail\n' >&2
}

main() {
  install_docker
  ensure_tools
  configure_docker_daocloud
  configure_kernel
  check_ports

  log "Preparing $INSTALL_DIR"
  install -d -m 0750 "$INSTALL_DIR" \
    "$INSTALL_DIR/data/mail" "$INSTALL_DIR/data/dkim" "$INSTALL_DIR/data/certs" \
    "$INSTALL_DIR/data/www" "$INSTALL_DIR/config"

  if [[ -f $INSTALL_DIR/docker-compose.yaml ]]; then
    cp -a "$INSTALL_DIR/docker-compose.yaml" "$INSTALL_DIR/docker-compose.yaml.bak.$(date +%Y%m%d%H%M%S)"
  fi
  curl -fsSL "$COMPOSE_URL" -o "$INSTALL_DIR/docker-compose.yaml"
  prepare_certificate
  write_env
  write_override false

  (cd "$INSTALL_DIR" && docker compose config --quiet)
  log "Pulling $CHATMAIL_IMAGE"
  (cd "$INSTALL_DIR" && docker compose pull chatmail)
  generate_chatmail_config
  write_override true
  (cd "$INSTALL_DIR" && docker compose config --quiet)

  if [[ $START_SERVICES == false ]]; then
    log "Preparation complete; start later with: cd $INSTALL_DIR && docker compose up -d --no-build"
    return
  fi

  log "Starting Chatmail"
  (cd "$INSTALL_DIR" && docker compose up -d --no-build)
  if [[ $EXTRACT_CONFIGS == true ]]; then
    extract_configs
  fi

  cat <<EOF

Chatmail deployment finished.
  Domain:       $MAIL_DOMAIN
  Image:        $CHATMAIL_IMAGE
  Directory:    $INSTALL_DIR
  TLS mode:     $TLS_MODE
  DaoCloud:     $USE_DAOCLOUD
  Settings:     $INSTALL_DIR/install.conf

Useful commands:
  cd $INSTALL_DIR && docker compose ps
  cd $INSTALL_DIR && docker compose logs -f chatmail
  docker exec chatmail cmdeploy status --ssh-host @local
  docker exec chatmail cmdeploy dns --ssh-host @local
EOF
}

main "$@"
