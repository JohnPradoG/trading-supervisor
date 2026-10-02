# Instalar el Trading Supervisor en el VPS, paso a paso

El código ya está copiado en el VPS, en `/opt/trading-supervisor`. Copia y pega cada bloque en
orden. No compartas en el chat ninguna contraseña ni clave que aparezca en pantalla.

## 1. Entrar al VPS

En tu PC abre **PowerShell** y escribe:

```
ssh vps3645261
```

## 2. Asegurar el VPS

```
cd /opt/trading-supervisor
bash deploy/vps/harden.sh
```

Al terminar, **sin cerrar esa ventana**, abre otra PowerShell y vuelve a escribir
`ssh vps3645261`. Si entra, todo va bien: escribe `exit` en esa segunda ventana y sigue en la
primera.

## 3. Cambiar la contraseña de root

```
passwd
```

Escribe la nueva contraseña dos veces (no se ve mientras escribes). Guárdala en tu gestor de
contraseñas.

## 4. Instalar Docker

```
bash deploy/vps/install-docker.sh
```

## 5. Crear la configuración (las claves se generan solas)

```
cd /opt/trading-supervisor/deploy
cp .env.example .env
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env
sed -i "s/^SUPERVISOR_ADMIN_TOKEN=.*/SUPERVISOR_ADMIN_TOKEN=$(openssl rand -hex 32)/" .env
sed -i "s/^SUPERVISOR_DOMAIN=.*/SUPERVISOR_DOMAIN=johntrading.duckdns.org/" .env
chmod 600 .env
```

## 6. Arrancar el supervisor

```
docker compose up -d --build
```

Tarda unos minutos la primera vez. Después comprueba:

```
docker compose ps
curl -s https://johntrading.duckdns.org/health
```

Debe responder `{"status":"ok","database":"up",...}`.

## 7. Activar el backup diario

```
( crontab -l 2>/dev/null; echo "15 3 * * * /opt/trading-supervisor/scripts/backup.sh >> /var/log/ts-backup.log 2>&1" ) | crontab -
bash /opt/trading-supervisor/scripts/backup.sh
```

La última línea debe decir `backup OK`.

## 8. Avísame

Pega en el chat lo que respondieron `docker compose ps` y el `curl` del paso 6 (no contienen
secretos). Si algo falla, pega el mensaje de error.
