# Hindsight backend

The bot's long-term memory (Hindsight) runs as a container named
`kimi-hindsight` on any Docker host reachable from the bot. The bot reaches
it at `HINDSIGHT_URL=http://<host>:8890`.

| | value |
|---|---|
| Stack dir | `~/kimi-hindsight/` |
| Container | `kimi-hindsight` |
| Storage | **host bind mount** `./data` → embedded Postgres (`pg0`) |
| API port | `8890` → 8888 |
| Control Plane | `9990` → 9999 |
| Model route | Ignored `.env` next to the compose file; seed from `.env.example` and replace every placeholder |

Storage is a **bind mount** (not a named volume) so the data is plainly visible
on the host filesystem and cannot be lost to `docker volume prune` or
`docker compose down -v`. Back it up by copying `./data`.

This Compose stack does not configure Hindsight's optional authentication
extension or an authenticating reverse proxy. Its published API and Control
Plane ports are therefore reachable by anything that can route to the Docker
host. Bind them to a trusted interface or put the service behind an
authenticating proxy.

## Bring-up

```bash
# On the Docker host:
mkdir -p ~/kimi-hindsight/data
cp docker-compose.yml ~/kimi-hindsight/
cp .env.example ~/kimi-hindsight/.env
# Edit .env: provider mode, base URL, API key, and model ID are required.
cd ~/kimi-hindsight && docker compose up -d
```

The tracked template deliberately contains no usable provider route. Keep the
filled `.env` with the deployment's other private configuration and back it up
separately from the public source checkout.

## Verify

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8890/docs   # 200 on the host
curl -s -o /dev/null -w "%{http_code}\n" http://<host>:8890/docs      # 200 from the bot's network
curl -s http://<host>:8890/v1/default/banks | head -c 400             # bank listing
# Control Plane UI: http://<host>:9990
```

## Day-2

```bash
docker compose logs --tail=50
docker compose pull && docker compose up -d   # deliberate upgrade
```

`pull_policy: missing` prevents an accidental image bump on a plain `up`;
upgrade deliberately so an upstream Postgres major bump never silently breaks
the `pg0` data.
