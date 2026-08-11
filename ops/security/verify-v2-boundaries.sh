#!/usr/bin/env bash
set -Eeuo pipefail

[[ ${EUID} -eq 0 ]] || {
  printf 'ETORO_V2_BOUNDARY_ERROR=root_required\n' >&2
  exit 1
}

mode=${1:-full}
[[ "$mode" == full || "$mode" == structural-only ]] || {
  printf 'ETORO_V2_BOUNDARY_ERROR=mode_invalid\n' >&2
  exit 1
}
release=${ETORO_V2_RELEASE_PATH:-/opt/etoro-v2/current}
pg_port=${ETORO_V2_POSTGRES_PORT:-5434}

for user in etoro-collector etoro-engine etoro-signer etoro-executor etoro-observer; do
  id "$user" >/dev/null
done

deny_read() {
  local user=$1
  local path=$2
  [[ ! -e "$path" ]] || ! runuser -u "$user" -- test -r "$path"
}

for user in etoro-collector etoro-engine etoro-executor etoro-observer; do
  deny_read "$user" /etc/etoro-agent/v2-risk-signing.key
done
for path in \
  /etc/etoro-agent/etoro-api-key \
  /etc/etoro-agent/etoro-demo-read-user-key \
  /etc/etoro-agent/etoro-demo-write-user-key \
  /etc/etoro-agent/postgres-v2-engine-dsn \
  /etc/etoro-agent/postgres-v2-executor-dsn \
  /etc/etoro-agent/postgres-v2-observer-dsn \
  /etc/etoro-agent/postgres-v2-backup.conf \
  /etc/etoro-agent/postgres-v2-restore.conf; do
  deny_read etoro-signer "$path"
done

[[ "$(systemctl show etoro-v2-market.service -p User --value)" == etoro-collector ]]
[[ "$(systemctl show etoro-v2-signer.service -p User --value)" == etoro-signer ]]
[[ "$(systemctl show etoro-v2-decision-apply-execution.service -p User --value)" == etoro-engine ]]
[[ "$(systemctl show etoro-v2-executor-postgres.service -p User --value)" == etoro-executor ]]
[[ "$(systemctl show etoro-v2-dashboard.service -p User --value)" == etoro-observer ]]
[[ "$(systemctl show etoro-v2-signer.service -p PrivateNetwork --value)" == yes ]]
[[ "$(systemctl show etoro-v2-signer.service -p RestrictAddressFamilies --value)" == AF_UNIX ]]
[[ ! -e /etc/etoro-agent/ENABLE_V2_DEMO_EXECUTION ]]

sudo -u postgres psql -p "$pg_port" -d etoro_v2 -Atqc \
  "SELECT has_table_privilege('etoro-engine','v2_order_commands','INSERT')" | grep -qx t
sudo -u postgres psql -p "$pg_port" -d etoro_v2 -Atqc \
  "SELECT has_table_privilege('etoro-executor','v2_order_commands','INSERT')" | grep -qx f
sudo -u postgres psql -p "$pg_port" -d etoro_v2 -Atqc \
  "SELECT has_table_privilege('etoro-executor','v2_outbox','INSERT')" | grep -qx f
sudo -u postgres psql -p "$pg_port" -d etoro_v2 -Atqc \
  "SELECT has_table_privilege('etoro-executor','v2_outbox','UPDATE')" | grep -qx t
sudo -u postgres psql -p "$pg_port" -d etoro_v2 -Atqc \
  "SELECT has_table_privilege('etoro-executor','v2_events','UPDATE')" | grep -qx f
sudo -u postgres psql -p "$pg_port" -d etoro_v2 -Atqc \
  "SELECT has_table_privilege('etoro-engine','v2_ai_runs','UPDATE')" | grep -qx f
sudo -u postgres psql -p "$pg_port" -d etoro_v2 -Atqc \
  "SELECT has_table_privilege('etoro-observer','v2_events','SELECT')" | grep -qx t
sudo -u postgres psql -p "$pg_port" -d etoro_v2 -Atqc \
  "SELECT has_table_privilege('etoro-observer','v2_events','UPDATE')" | grep -qx f
runuser -u etoro-executor -- psql -p "$pg_port" -d etoro_v2 -Atqc \
  'SELECT state FROM v2_trading_state WHERE singleton=TRUE' | grep -Eq '^(LOCKED|HALT_NEW|REDUCE_ONLY|ACTIVE)$'
runuser -u etoro-observer -- psql -p "$pg_port" -d etoro_v2 -Atqc \
  "SELECT value FROM v2_meta WHERE key='schema_version'" | grep -qx 2

systemctl start etoro-v2-signer.service
[[ "$(systemctl is-active etoro-v2-signer.service)" == active ]]
socket_path=/run/etoro-v2-signer/risk-signer.sock
[[ -S "$socket_path" ]]
if runuser -u etoro-executor -- python3 -c \
  'import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1])' "$socket_path" \
  >/dev/null 2>&1; then
  printf 'ETORO_V2_BOUNDARY_ERROR=executor_reached_signer_socket\n' >&2
  exit 1
fi
runuser -u etoro-engine -- python3 -c \
  'import socket,sys; s=socket.socket(socket.AF_UNIX); s.settimeout(2); s.connect(sys.argv[1]); s.sendall(b"{}\n"); assert s.recv(4096)' \
  "$socket_path"
[[ "$(systemctl is-active etoro-v2-signer.service)" == active ]]
signer_pid=$(systemctl show etoro-v2-signer.service -p MainPID --value)
[[ "$signer_pid" =~ ^[1-9][0-9]*$ ]]
[[ "$(readlink "/proc/$signer_pid/ns/net")" != "$(readlink /proc/1/ns/net)" ]]

if [[ "$mode" == full ]]; then
  [[ -s /etc/etoro-agent/etoro-demo-read-user-key && -s /etc/etoro-agent/etoro-api-key ]]
  systemd-run --wait --pipe --collect --quiet \
    --property=User=etoro-collector \
    --property=Group=etoro-collector \
    --property=LoadCredential=read-key:/etc/etoro-agent/etoro-demo-read-user-key \
    --property=LoadCredential=api-key:/etc/etoro-agent/etoro-api-key \
    "$release/.venv/bin/python" -c \
    'import os; d=os.environ["CREDENTIALS_DIRECTORY"]; os.environ["ETORO_USER_KEY_FILE"]=d+"/read-key"; os.environ["ETORO_API_KEY_FILE"]=d+"/api-key"; from etoro_agent.mcp import EtoroMCPClient; EtoroMCPClient().verify_isolated_demo_read_scope(); print("ETORO_V2_READ_SCOPE_OK")'
  if [[ -s /etc/etoro-agent/etoro-demo-write-user-key ]]; then
    systemd-run --wait --pipe --collect --quiet \
      --property=User=etoro-executor \
      --property=Group=etoro-executor \
      --property=LoadCredential=write-key:/etc/etoro-agent/etoro-demo-write-user-key \
      --property=LoadCredential=api-key:/etc/etoro-agent/etoro-api-key \
      "$release/.venv/bin/python" -c \
      'import os; d=os.environ["CREDENTIALS_DIRECTORY"]; os.environ["ETORO_USER_KEY_FILE"]=d+"/write-key"; os.environ["ETORO_API_KEY_FILE"]=d+"/api-key"; from etoro_agent.etoro_api_current_v2 import EtoroPublicApiDemoClientV2; EtoroPublicApiDemoClientV2().verify_isolated_demo_execution_scope(); print("ETORO_V2_WRITE_SCOPE_OK")'
  fi
fi

printf 'ETORO_V2_BOUNDARIES_OK mode=%s signer_pid=%s executor_gate=absent\n' "$mode" "$signer_pid"
