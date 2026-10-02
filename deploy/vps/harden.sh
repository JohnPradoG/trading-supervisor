#!/usr/bin/env bash
# Endurecimiento inicial del VPS (Ubuntu 24.04). Ejecutar como root, una vez, ANTES de
# desplegar el supervisor. Es idempotente: se puede volver a ejecutar sin romper nada.
#
#   sudo bash deploy/vps/harden.sh                      # cierra xrdp (3389) a internet
#   RDP_ALLOW_IP=203.0.113.7 sudo -E bash deploy/vps/harden.sh   # xrdp solo desde esa IP
#
# Lo que NO hace (a propósito): cambiar la contraseña de root. Hazlo tú con `passwd`,
# directamente en el VPS, y no la escribas en ningún chat.
#
# Qué hace:
#   1. Comprueba que root tiene una llave SSH autorizada (si no, se detiene: evita quedarte fuera).
#   2. SSH: solo llave (sin contraseñas), root solo con llave, máx. 3 intentos.
#   3. Firewall ufw: entra solo 22, 80, 443 (y 3389 si RDP_ALLOW_IP está definida).
#   4. fail2ban para SSH y actualizaciones de seguridad automáticas.
#   5. Zona horaria UTC y sincronización de hora.
#   6. Swap de 2 GB (sustituye al de 512 MB si es más pequeño).
set -euo pipefail

log() { printf '\n==> %s\n' "$*"; }
[[ $EUID -eq 0 ]] || { echo "Ejecuta como root (sudo)." >&2; exit 1; }

# 1. Salvaguarda contra el bloqueo -----------------------------------------------------------
log "Comprobando llave SSH de root"
if ! grep -qE '^(ssh-(ed25519|rsa)|ecdsa-sha2)' /root/.ssh/authorized_keys 2>/dev/null; then
  echo "ERROR: /root/.ssh/authorized_keys no tiene ninguna llave. No desactivo las contraseñas" >&2
  echo "porque te quedarías sin acceso. Copia primero tu llave pública y vuelve a ejecutar." >&2
  exit 1
fi
echo "OK: hay $(grep -cE '^(ssh-|ecdsa-)' /root/.ssh/authorized_keys) llave(s) autorizada(s)."

# 2. SSH -------------------------------------------------------------------------------------
log "Configurando SSH (solo llave)"
cat > /etc/ssh/sshd_config.d/10-trading-supervisor.conf <<'EOF'
# Gestionado por trading-supervisor/deploy/vps/harden.sh
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
MaxAuthTries 3
X11Forwarding no
EOF
# Ubuntu cloud-init puede reactivar contraseñas en 50-cloud-init.conf; los archivos se leen en
# orden y gana el primero, por eso el nuestro empieza por 10-.
sshd -t
systemctl reload ssh

# 3. Firewall --------------------------------------------------------------------------------
log "Configurando firewall ufw"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ufw fail2ban unattended-upgrades >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'SSH'
ufw allow 80/tcp comment 'HTTP (Caddy, redirige a HTTPS)'
ufw allow 443/tcp comment 'HTTPS (API y dashboard)'
# Elimina cualquier regla previa abierta para 3389.
while ufw status numbered | grep -q '3389'; do
  n=$(ufw status numbered | grep '3389' | head -1 | sed -E 's/^\[ *([0-9]+)\].*/\1/')
  yes | ufw delete "$n" >/dev/null
done
if [[ -n "${RDP_ALLOW_IP:-}" ]]; then
  ufw allow from "$RDP_ALLOW_IP" to any port 3389 proto tcp comment 'xrdp solo desde IP de John'
  echo "xrdp (3389) permitido solo desde $RDP_ALLOW_IP"
else
  echo "xrdp (3389) cerrado a internet. Para usarlo: túnel SSH  ssh -L 3389:localhost:3389 vps3645261"
fi
ufw --force enable
ufw status verbose

# 4. fail2ban y actualizaciones ----------------------------------------------------------------
log "Activando fail2ban y actualizaciones de seguridad"
cat > /etc/fail2ban/jail.d/sshd.local <<'EOF'
[sshd]
enabled = true
maxretry = 5
findtime = 10m
bantime = 1h
EOF
systemctl enable --now fail2ban >/dev/null
systemctl restart fail2ban
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

# 5. Hora ------------------------------------------------------------------------------------
log "Zona horaria UTC y sincronización de hora"
timedatectl set-timezone UTC
timedatectl set-ntp true
timedatectl | sed -n '1,6p'

# 6. Swap ------------------------------------------------------------------------------------
log "Swap de 2 GB"
current_kb=$(awk '/SwapTotal/ {print $2}' /proc/meminfo)
if (( current_kb < 1900000 )); then
  if [[ ! -f /swapfile2g ]]; then
    fallocate -l 2G /swapfile2g
    chmod 600 /swapfile2g
    mkswap /swapfile2g >/dev/null
  fi
  swapon /swapfile2g 2>/dev/null || true
  grep -q '^/swapfile2g ' /etc/fstab || echo '/swapfile2g none swap sw 0 0' >> /etc/fstab
fi
sysctl -qw vm.swappiness=10
echo 'vm.swappiness=10' > /etc/sysctl.d/99-trading-supervisor.conf
free -h

log "Listo. Pendiente manual: cambiar la contraseña de root con 'passwd'."
echo "Antes de cerrar esta sesión, abre OTRA terminal y comprueba que 'ssh vps3645261' sigue entrando."
