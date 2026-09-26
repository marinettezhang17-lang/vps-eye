#!/usr/bin/env bash
# vps-eye 一键安装脚本（Ubuntu / Debian）
#
# 用法：
#   sudo bash install.sh                 # 交互式安装
#   sudo DOMAIN=eye.example.com EMAIL=you@example.com bash install.sh   # 免交互
#   sudo bash install.sh --rotate-token  # 只换暗号（旧暗号立刻作废）
#
# 它会做这些事：
#   1. 装 python3 / nginx / certbot
#   2. 把 eye.py 放到 /opt/vps-eye，生成暗号到 /etc/vps-eye/token（只有 root 能读）
#   3. 注册 systemd 服务 vps-eye，开机自启、挂了自动重启
#   4. 配 nginx 反代 + Let's Encrypt HTTPS 证书
#   5. 最后在终端里打印连接地址和暗号（只打印在你自己的终端里）

set -euo pipefail

APP_DIR=/opt/vps-eye
CONF_DIR=/etc/vps-eye
LOG_DIR=/var/log/vps-eye
SERVICE=vps-eye
PORT="${PORT:-8787}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 如果 eye.py 不在脚本旁边，就从这里下载（发布到 GitHub 后改成你自己的仓库地址）
RAW_URL="${RAW_URL:-https://raw.githubusercontent.com/marinettezhang17-lang/vps-eye/main/eye.py}"

say()  { printf '\033[1;32m[vps-eye]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[注意]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[失败]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 运行：sudo bash install.sh"
command -v apt-get >/dev/null || die "这个脚本只支持 Ubuntu / Debian（需要 apt-get）"

new_token() { openssl rand -hex 32; }

print_connect_info() {
  local domain="$1"
  echo
  echo "================================================================"
  say "装好了！去 Claude 里添加自定义连接器："
  echo
  echo "  名称：   随便起，比如「我的VPS」"
  echo "  地址：   https://${domain}/mcp"
  echo "  登录方式：选 No sign-in（不需要登录）"
  echo "  请求头：  名称填  Authorization"
  echo "           值填    Bearer $(cat "$CONF_DIR/token")"
  echo "                   （Bearer 和暗号之间有一个空格）"
  echo
  warn "暗号 = 你服务器的最高权限钥匙。"
  warn "只在这个终端里复制，不要截图、不要拍照、不要发到任何聊天或群里。"
  warn "以后想再看：sudo cat $CONF_DIR/token     想换新的：sudo bash install.sh --rotate-token"
  echo "================================================================"
}

# ---------- 只换暗号 ----------
if [ "${1:-}" = "--rotate-token" ]; then
  [ -f "$CONF_DIR/token" ] || die "还没安装过，先正常运行 install.sh"
  cp "$CONF_DIR/token" "$CONF_DIR/token.bak-$(date +%Y%m%d-%H%M%S)"
  new_token > "$CONF_DIR/token"
  chmod 600 "$CONF_DIR/token"
  systemctl restart "$SERVICE"
  say "暗号已更换，旧暗号立刻失效。记得去 Claude 连接器里把请求头的值也改成新的。"
  DOMAIN_SAVED="$(grep -oP '^DOMAIN=\K.*' "$CONF_DIR/eye.env" 2>/dev/null || echo '你的域名')"
  print_connect_info "$DOMAIN_SAVED"
  exit 0
fi

# ---------- 收集信息 ----------
if [ -z "${DOMAIN:-}" ]; then
  echo "需要一个已经解析到这台服务器的域名（子域名就行，比如 eye.example.com）。"
  read -rp "域名: " DOMAIN
fi
[ -n "$DOMAIN" ] || die "域名不能为空"
if [ -z "${EMAIL:-}" ]; then
  read -rp "邮箱（申请 HTTPS 证书用，证书快过期时会发提醒）: " EMAIL
fi
[ -n "$EMAIL" ] || die "邮箱不能为空"

# ---------- 检查域名解析 ----------
say "检查域名解析……"
MY_IP="$(curl -4 -s --max-time 8 https://api.ipify.org || true)"
DNS_IP="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}' || true)"
if [ -z "$DNS_IP" ]; then
  die "$DOMAIN 还解析不到任何 IP。先去域名服务商那里加一条 A 记录指向这台服务器，等几分钟再来。"
elif [ -n "$MY_IP" ] && [ "$MY_IP" != "$DNS_IP" ]; then
  warn "$DOMAIN 解析到 $DNS_IP，但这台服务器的公网 IP 看起来是 $MY_IP。"
  warn "如果用了 CDN 代理（比如 Cloudflare 的橙色云朵），请先关掉代理（改成灰色）再装。"
  read -rp "确定继续吗？[y/N] " yn; [ "${yn:-N}" = "y" ] || exit 1
fi

# ---------- 检查端口 ----------
if ss -ltn "( sport = :$PORT )" | grep -q ":$PORT" && ! systemctl is-active --quiet "$SERVICE"; then
  die "端口 $PORT 已经被别的程序占用了。换一个：sudo PORT=8788 bash install.sh"
fi

# ---------- 装依赖 ----------
say "安装 python3 / nginx / certbot ……"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 nginx certbot python3-certbot-nginx openssl curl >/dev/null
python3 -c 'import sys; assert sys.version_info >= (3, 8)' || die "需要 Python 3.8 以上"

# ---------- 放程序 ----------
say "安装程序到 $APP_DIR ……"
mkdir -p "$APP_DIR" "$CONF_DIR" "$LOG_DIR"
if [ -f "$SCRIPT_DIR/eye.py" ]; then
  install -m 755 "$SCRIPT_DIR/eye.py" "$APP_DIR/eye.py"
else
  curl -fsSL "$RAW_URL" -o "$APP_DIR/eye.py" || die "下载 eye.py 失败：$RAW_URL"
  chmod 755 "$APP_DIR/eye.py"
fi
chmod 700 "$CONF_DIR" "$LOG_DIR"

if [ ! -s "$CONF_DIR/token" ]; then
  new_token > "$CONF_DIR/token"
  say "已生成新暗号"
else
  say "沿用已有暗号"
fi
chmod 600 "$CONF_DIR/token"

cat > "$CONF_DIR/eye.env" <<EOF
DOMAIN=$DOMAIN
EYE_HOST=127.0.0.1
EYE_PORT=$PORT
EYE_TOKEN_FILE=$CONF_DIR/token
EYE_LOG_FILE=$LOG_DIR/audit.log
EYE_MAX_OUTPUT=60000
EOF
chmod 600 "$CONF_DIR/eye.env"

# ---------- systemd ----------
say "注册 systemd 服务 $SERVICE ……"
cat > /etc/systemd/system/$SERVICE.service <<EOF
[Unit]
Description=vps-eye MCP server (let Claude operate this VPS)
After=network-online.target

[Service]
EnvironmentFile=$CONF_DIR/eye.env
ExecStart=/usr/bin/python3 $APP_DIR/eye.py
Restart=on-failure
RestartSec=3
User=root
WorkingDirectory=/root

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"
sleep 1
curl -fs "http://127.0.0.1:$PORT/health" >/dev/null || die "服务没起来，看日志：journalctl -u $SERVICE -n 50"

# ---------- nginx ----------
say "配置 nginx ……"
NGX=/etc/nginx/conf.d/vps-eye.conf
[ -f "$NGX" ] && cp "$NGX" "$CONF_DIR/nginx.conf.bak-$(date +%Y%m%d-%H%M%S)"
cat > "$NGX" <<EOF
server {
    listen 80;
    server_name $DOMAIN;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header Authorization \$http_authorization;
        proxy_buffering off;
        proxy_read_timeout 1900s;
        proxy_send_timeout 1900s;
        client_max_body_size 20m;
    }
}
EOF
nginx -t >/dev/null 2>&1 || { nginx -t; die "nginx 配置检查没通过（上面是原因）"; }
systemctl reload nginx

# ---------- HTTPS ----------
SCHEME=https
if [ "${SKIP_TLS:-0}" = "1" ]; then
  # 高级用法：HTTPS 由别的东西负责（比如 Cloudflare Tunnel），这里只配 80 端口
  warn "SKIP_TLS=1：跳过证书申请。请确认对外一定是 HTTPS，否则暗号会明文在网上跑！"
  SCHEME=http
else
  say "申请 HTTPS 证书（Let's Encrypt）……"
  certbot --nginx -d "$DOMAIN" -m "$EMAIL" --agree-tos --non-interactive --redirect >/dev/null \
    || die "证书申请失败。常见原因：域名没解析到这台机器、80 端口被云厂商防火墙挡住。"
fi

# ---------- 最终自检 ----------
say "自检……"
code_no="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$SCHEME://$DOMAIN/mcp" -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"ping"}')"
code_ok="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$SCHEME://$DOMAIN/mcp" -H 'Content-Type: application/json' -H "Authorization: Bearer $(cat "$CONF_DIR/token")" -d '{"jsonrpc":"2.0","id":1,"method":"ping"}')"
[ "$code_no" = "401" ] || warn "不带暗号访问返回了 $code_no（应该是 401），请检查！"
[ "$code_ok" = "200" ] || die "带暗号访问返回了 $code_ok（应该是 200），请检查 nginx 和服务日志"
say "不带暗号 → 401 拒绝 ✓   带暗号 → 200 通过 ✓"

print_connect_info "$DOMAIN"
