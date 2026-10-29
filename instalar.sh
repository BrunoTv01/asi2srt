#!/usr/bin/env bash
# Instalação completa do asi2srt com um comando (Raspberry Pi, Ubuntu, Debian):
#
#   curl -fsSL https://raw.githubusercontent.com/BrunoTv01/asi2srt/main/instalar.sh | sudo bash
#
# Variáveis opcionais:
#   COMPILAR=1   compila o TSDuck do código-fonte se não houver pacote pronto (demora no Raspberry)
#   PORTA=8088   porta do painel web
set -euo pipefail

URL="https://raw.githubusercontent.com/BrunoTv01/asi2srt/main/asi2srt.py"
CONFIG=/etc/asi2srt/config.yaml

if [ "$(id -u)" -ne 0 ]; then
  echo "rode como root:  curl -fsSL ${URL%/*}/instalar.sh | sudo bash"; exit 1
fi

echo "==> Dependências"
if command -v apt-get >/dev/null; then
  apt-get update -qq
  apt-get install -y -qq python3 python3-yaml curl ca-certificates >/dev/null
elif command -v dnf >/dev/null; then
  dnf install -y -q python3 python3-pyyaml curl
fi

echo "==> Baixando o asi2srt"
curl -fsSL -o /usr/local/bin/asi2srt "$URL"
chmod 755 /usr/local/bin/asi2srt
asi2srt --versao

echo "==> TSDuck"
if ! command -v tsp >/dev/null; then
  if ! asi2srt instalar-tsduck; then
    if [ "${COMPILAR:-0}" = "1" ]; then
      asi2srt instalar-tsduck --compilar
    else
      echo
      echo "Não há pacote pronto do TSDuck para este sistema."
      echo "Para compilar do código-fonte (no Raspberry leva de 1 a 3 horas), rode:"
      echo "  curl -fsSL ${URL%/*}/instalar.sh | sudo COMPILAR=1 bash"
      exit 1
    fi
  fi
else
  echo "TSDuck já instalado: $(tsp --version 2>&1 | head -1)"
fi

echo "==> Serviço"
asi2srt instalar-servico -c "$CONFIG" >/dev/null
if [ -n "${PORTA:-}" ]; then
  sed -i "s/^\(\s*status_http:\).*/\1 \"0.0.0.0:${PORTA}\"/" "$CONFIG"
fi
systemctl enable --now asi2srt >/dev/null 2>&1 || systemctl restart asi2srt
systemctl restart asi2srt

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
PORTA_REAL=$(grep -oP 'status_http:\s*"?[\d.]*:\K\d+' "$CONFIG" 2>/dev/null || echo 8088)
echo
asi2srt verificar || true
cat <<EOF

Pronto! asi2srt instalado e rodando.

  Painel:   http://${IP:-IP-DO-EQUIPAMENTO}:${PORTA_REAL}
  Login:    admin / admin   (troque em ${CONFIG} -> senha_painel)
  Logs:     journalctl -u asi2srt -f
  Atualizar: rode este mesmo comando de novo

EOF
