#!/usr/bin/env bash
set -euo pipefail

INSTANCES_JSON="/home/claude/twitchdrops/instances.json"
NGINX_CONF="/etc/nginx/sites-available/tdm.simpliaj.xyz"
WORK_DIR="/home/claude/twitchdrops"
PYTHON="/home/claude/twitchdrops/venv/bin/python"

_update_nginx() {
    python3 - <<'PYEOF'
import json, sys

with open("/home/claude/twitchdrops/instances.json") as f:
    data = json.load(f)

instances = sorted(data["instances"], key=lambda x: x["n"])

location_blocks = ""
for inst in instances:
    n = inst["n"]
    port = inst["port"]
    if n == 1:
        loc_path = "/"
        proxy_pass = f"http://127.0.0.1:{port}"
    else:
        loc_path = f"/acc{n}/"
        proxy_pass = f"http://127.0.0.1:{port}/"

    location_blocks += f"""
    location {loc_path} {{
        proxy_pass {proxy_pass};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
    }}
"""

config = f"""server {{
    server_name tdm.simpliaj.xyz;
{location_blocks}
    listen 443 ssl; # managed by Certbot
    ssl_certificate /etc/letsencrypt/live/tdm.simpliaj.xyz/fullchain.pem; # managed by Certbot
    ssl_certificate_key /etc/letsencrypt/live/tdm.simpliaj.xyz/privkey.pem; # managed by Certbot
    include /etc/letsencrypt/options-ssl-nginx.conf; # managed by Certbot
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem; # managed by Certbot

}}
server {{
    if ($host = tdm.simpliaj.xyz) {{
        return 301 https://$host$request_uri;
    }} # managed by Certbot


    listen 80;
    server_name tdm.simpliaj.xyz;
    return 404; # managed by Certbot


}}
"""

print(config, end="")
PYEOF
}

cmd_create() {
    # Find next n
    local next_n
    next_n=$(python3 -c "
import json
with open('$INSTANCES_JSON') as f:
    data = json.load(f)
ns = [i['n'] for i in data['instances']]
print(max(ns) + 1 if ns else 1)
")

    local port=$((8080 + (next_n - 1) * 2))
    local pm2_name
    local data_dir

    if [ "$next_n" -eq 1 ]; then
        pm2_name="twitchdrops"
        data_dir="data"
    else
        pm2_name="twitchdrops${next_n}"
        data_dir="data${next_n}"
    fi

    local abs_data_dir="${WORK_DIR}/${data_dir}"

    echo "Creating instance ${next_n} on port ${port} (${pm2_name}, ${data_dir})..."

    mkdir -p "$abs_data_dir"

    cd "$WORK_DIR"
    TDM_PORT="$port" TDM_DATA_DIR="$abs_data_dir" pm2 start main.py \
        --name "$pm2_name" \
        --interpreter "$PYTHON" \
        --cwd "$WORK_DIR"

    pm2 save

    # Add to instances.json
    python3 - <<PYEOF
import json
with open("$INSTANCES_JSON") as f:
    data = json.load(f)
data["instances"].append({
    "n": $next_n,
    "port": $port,
    "data_dir": "$data_dir",
    "pm2_name": "$pm2_name",
    "label": "Account $next_n"
})
with open("$INSTANCES_JSON", "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PYEOF

    _update_nginx | sudo tee "$NGINX_CONF" > /dev/null
    sudo nginx -s reload

    echo "Created instance ${next_n} on port ${port}"
}

cmd_remove() {
    local n="${1:-}"
    if [ -z "$n" ]; then
        echo "Usage: $0 remove N" >&2
        exit 1
    fi
    if [ "$n" -lt 2 ]; then
        echo "Error: cannot remove instance 1" >&2
        exit 1
    fi

    local pm2_name
    pm2_name=$(python3 -c "
import json, sys
with open('$INSTANCES_JSON') as f:
    data = json.load(f)
for inst in data['instances']:
    if inst['n'] == $n:
        print(inst['pm2_name'])
        sys.exit(0)
print('', end='')
sys.exit(1)
" 2>/dev/null) || {
        echo "Error: instance ${n} not found in ${INSTANCES_JSON}" >&2
        exit 1
    }

    if [ -z "$pm2_name" ]; then
        echo "Error: instance ${n} not found" >&2
        exit 1
    fi

    echo "Removing instance ${n} (${pm2_name})..."

    pm2 delete "$pm2_name"
    pm2 save

    # Remove from instances.json
    python3 - <<PYEOF
import json
with open("$INSTANCES_JSON") as f:
    data = json.load(f)
data["instances"] = [i for i in data["instances"] if i["n"] != $n]
with open("$INSTANCES_JSON", "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PYEOF

    _update_nginx | sudo tee "$NGINX_CONF" > /dev/null
    sudo nginx -s reload

    echo "Removed instance ${n}"
}

case "${1:-}" in
    create)
        cmd_create
        ;;
    remove)
        cmd_remove "${2:-}"
        ;;
    *)
        echo "Usage: $0 {create|remove N}" >&2
        exit 1
        ;;
esac
