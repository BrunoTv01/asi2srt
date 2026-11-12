#!/bin/sh
# Ponto de entrada do container asi2srt.
#   rodar (padrão)   gateway + painel web
#   qualquer outro   repassado ao asi2srt (ex.: verificar, sysctl, comando)
set -e
CFG="${ASI2SRT_CONFIG:-/etc/asi2srt/config.yaml}"
DIR="$(dirname "$CFG")"
mkdir -p "$DIR"

if [ "${1:-rodar}" = "rodar" ]; then
  # 1ª execução: configuração vazia (canais criados pelo painel), login admin/admin
  if [ ! -f "$CFG" ]; then
    asi2srt init --vazia "$CFG" >/dev/null
    echo "asi2srt: configuração criada em $CFG (painel: usuário admin, senha admin - troque no 1º acesso)"
  fi
  # logo padrão, a não ser que já exista um ou tenha sido removido pelo painel
  if ! ls "$DIR"/logo-painel.* >/dev/null 2>&1 \
     && ! grep -qE "^\s*\"?logo\"?:\s*['\"]?-['\"]?\s*,?\s*$" "$CFG" \
     && [ -f /usr/share/asi2srt/marca/logo-tv-cultura.png ]; then
    cp /usr/share/asi2srt/marca/logo-tv-cultura.png "$DIR/logo-painel.png"
  fi
  # PORTA=9000 muda a porta do painel
  if [ -n "${PORTA:-}" ]; then
    sed -i "s/^\(\s*\"\?status_http\"\?:\s*\).*/\1\"0.0.0.0:${PORTA}\"/" "$CFG"
  fi
  exec asi2srt rodar -c "$CFG"
fi

exec asi2srt "$@"
