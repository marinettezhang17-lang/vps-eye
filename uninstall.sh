#!/usr/bin/env bash
# vps-eye 卸载脚本
#   sudo bash uninstall.sh          # 停服务、删程序和 nginx 配置，保留暗号和审计日志
#   sudo bash uninstall.sh --purge  # 连暗号、审计日志一起删
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行：sudo bash uninstall.sh"; exit 1; }

DOMAIN="$(grep -oP '^DOMAIN=\K.*' /etc/vps-eye/eye.env 2>/dev/null || true)"

systemctl disable --now vps-eye 2>/dev/null || true
rm -f /etc/systemd/system/vps-eye.service
systemctl daemon-reload
rm -f /etc/nginx/conf.d/vps-eye.conf
nginx -t >/dev/null 2>&1 && systemctl reload nginx || true
rm -rf /opt/vps-eye

if [ "${1:-}" = "--purge" ]; then
  rm -rf /etc/vps-eye /var/log/vps-eye
  echo "已彻底删除（包括暗号和审计日志）。"
else
  echo "已卸载。暗号和审计日志还在 /etc/vps-eye 和 /var/log/vps-eye，想一起删就加 --purge。"
fi
[ -n "$DOMAIN" ] && echo "HTTPS 证书没动。不需要的话：sudo certbot delete --cert-name $DOMAIN"
echo "别忘了去 Claude 里把这个连接器也删掉。"
