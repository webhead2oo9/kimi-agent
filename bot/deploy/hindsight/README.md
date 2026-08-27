# Hindsight backend

The bot's long-term memory (Hindsight) runs as a container named
`kimi-hindsight`. When they share a host, the bot reaches it at
`HINDSIGHT_URL=http://127.0.0.1:8890`. A bot on another host uses the Docker
host's trusted LAN/VPN address after the API bind is explicitly configured.

| | value |
|---|---|
| Stack dir | `~/kimi-hindsight/` |
| Container | `kimi-hindsight` |
| Storage | **host bind mount** `./data` → embedded Postgres (`pg0`) |
| API port | `127.0.0.1:8890` → 8888 by default |
| Control Plane | `127.0.0.1:9990` → 9999 by default |
| Model route | Ignored `.env` next to the compose file; seed from `.env.example` and replace every placeholder |

Storage is a **bind mount** (not a named volume) so the data is visible
on the host filesystem and cannot be lost to `docker volume prune` or
`docker compose down -v`. Back it up by copying `./data`.

This Compose stack does not configure Hindsight's optional authentication
extension or an authenticating reverse proxy, so both ports bind to loopback
by default. If the bot runs elsewhere, set `HINDSIGHT_API_BIND_ADDRESS` to the
Docker host's trusted LAN/VPN address and use the host firewall to allow only
the bot. Leave `HINDSIGHT_CONTROL_BIND_ADDRESS` on loopback unless the Control
Plane is behind an authenticating proxy.

## Bring-up

```bash
# On the Docker host:
mkdir -p ~/kimi-hindsight/data
cp docker-compose.yml ~/kimi-hindsight/
cp .env.example ~/kimi-hindsight/.env
# Edit .env: provider mode, base URL, API key, and model ID are required.
# For a remote bot, also set HINDSIGHT_API_BIND_ADDRESS to this host's trusted
# LAN/VPN address; keep HINDSIGHT_CONTROL_BIND_ADDRESS on 127.0.0.1.
cd ~/kimi-hindsight && docker compose up -d
```

The tracked template contains no usable provider route. Keep the
filled `.env` with the deployment's other private configuration and back it up
separately from the public source checkout.

## Verify

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8890/docs   # 200 on the host
curl -s http://localhost:8890/v1/default/banks | head -c 400           # bank listing
# From a remote bot, after configuring the trusted API bind:
curl -s -o /dev/null -w "%{http_code}\n" http://<trusted-host>:8890/docs
# Control Plane UI on the Docker host: http://localhost:9990
```

## Day-2

```bash
docker compose logs --tail=50
docker compose pull && docker compose up -d   # upgrade
```

`pull_policy: missing` prevents an accidental image bump on a plain `up`;
upgrade deliberately so an upstream Postgres major bump never silently breaks
the `pg0` data.
