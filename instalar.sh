#!/usr/bin/env bash
# Instalação completa do asi2srt com um comando (Raspberry Pi, Ubuntu, Debian):
#
#   curl -fsSL https://raw.githubusercontent.com/BrunoTv01/asi2srt/main/instalar.sh | sudo bash
#
# Faz tudo: dependências, asi2srt, TSDuck, driver da placa DekTec (se houver placa) e serviço.
#
# Variáveis opcionais:
#   DEKTEC_SDK=/caminho/LinuxSDK_vXXXX.tar.gz   SDK da DekTec (se não estiver em Downloads ou /tmp)
#   COMPILAR=1   compila o TSDuck do código-fonte se não houver pacote pronto (demora no Raspberry)
#   PORTA=8088   porta do painel web
#
# Ex.: curl -fsSL .../instalar.sh | sudo DEKTEC_SDK=/home/user/Downloads/LinuxSDK_v2025.01.0.tar.gz bash
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
  # ffmpeg: usado pelas saídas RTMP/RTMPS
  apt-get install -y -qq --no-install-recommends ffmpeg >/dev/null || echo "aviso: ffmpeg não instalado (saídas RTMP indisponíveis)"
elif command -v dnf >/dev/null; then
  dnf install -y -q python3 python3-pyyaml curl
  dnf install -y -q ffmpeg 2>/dev/null || echo "aviso: ffmpeg não instalado (saídas RTMP indisponíveis; habilite RPM Fusion)"
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

echo "==> Placa DekTec"
DEKTEC_PENDENTE=0
if ! asi2srt instalar-dektec --se-houver-placa; then
  DEKTEC_PENDENTE=1
fi

echo "==> Logo do painel"
mkdir -p /etc/asi2srt
if ls /etc/asi2srt/logo-painel.* >/dev/null 2>&1; then
  echo "logo já configurado"
elif [ -f "$CONFIG" ] && grep -qE "^\s*\"?logo\"?:\s*['\"]?-['\"]?\s*,?\s*$" "$CONFIG"; then
  echo "logo removido pelo painel - mantido sem logo"
else
  curl -fsSL -o /etc/asi2srt/logo-painel.png "${URL%/*}/marca/logo-tv-cultura.png" && echo "logo instalado" \
    || echo "aviso: não foi possível baixar o logo (pode ser enviado depois pelo botão ⚙ do painel)"
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
if [ "$DEKTEC_PENDENTE" = "1" ]; then
  cat <<AVISO
ATENÇÃO: há placa DekTec neste servidor, mas o driver ainda não foi instalado.
  1. Em https://www.dektec.com/downloads/SDK/ (DekTec SDKs for Linux), baixe o primeiro:
     "DTAPI - C++ API for all DTA, DTU, DTE devices" e deixe na pasta Downloads
  2. Rode:  sudo asi2srt instalar-dektec --sdk /caminho/LinuxSDK_vXXXX.tar.gz
  3. Reinicie se for pedido e confira com:  asi2srt verificar

AVISO
fi
