# asi2srt em Docker
#
#   docker compose up -d --build        (ver docker-compose.yml e o README, seção "Docker")
#
# A imagem traz: asi2srt + TSDuck (com plugins srt, ip, dektec) + ffmpeg + reportlab (PDF).
# O driver da placa DekTec é módulo do kernel: fica instalado no HOST (sudo asi2srt instalar-dektec);
# o container só recebe o dispositivo /dev/Dta*.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    ASI2SRT_DOCKER=1 \
    ASI2SRT_CONFIG=/etc/asi2srt/config.yaml \
    TZ=America/Sao_Paulo

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-yaml python3-reportlab ffmpeg \
      ca-certificates curl iproute2 pciutils tini tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY asi2srt.py /usr/local/bin/asi2srt
RUN chmod 755 /usr/local/bin/asi2srt \
 && asi2srt instalar-tsduck \
 && rm -rf /var/lib/apt/lists/* /tmp/* \
 && tsp --version

COPY marca/ /usr/share/asi2srt/marca/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh

# configuração, usuários, sessões, relatórios e logo ficam todos aqui
VOLUME /etc/asi2srt
EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/favicon.ico' % os.environ.get('PORTA','8088'), timeout=4)" || exit 1

STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["rodar"]
