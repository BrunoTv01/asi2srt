# asi2srt — Gateway DVB-ASI → SRT

Um único programa que captura o transport stream de uma entrada **DVB-ASI** e o
encapsula em **SRT** (caller ou listener, com criptografia AES opcional).
Usa o [TSDuck](https://tsduck.io) como motor e adiciona supervisão, reinício
automático, monitoração e painel web.

```
 Placa ASI ──► tsp: entrada ──► continuity ──► filter (nulos) ──► bitrate_monitor ──► SRT ──► rede
 (DekTec /         ▲                                                                   │
  DVEO)            └──── asi2srt: supervisiona, reinicia, painel em :8088 ◄─────────────┘
```

## Instalação

**Opção 1 — binário pronto (não precisa de Python):** baixe o `asi2srt-linux-amd64` em
[Releases](../../releases).

```bash
chmod +x asi2srt-linux-amd64 && sudo mv asi2srt-linux-amd64 /usr/local/bin/asi2srt
```

**Opção 2 — script Python (3.8+):**

```bash
curl -LO https://raw.githubusercontent.com/BrunoTv01/asi2srt/main/asi2srt.py
chmod +x asi2srt.py && sudo mv asi2srt.py /usr/local/bin/asi2srt
pip install pyyaml   # opcional; sem ele use configuração em JSON
```

Depois instale o TSDuck e confira:

```bash
sudo asi2srt instalar-tsduck
asi2srt verificar
```

## Uso rápido (sem arquivo de configuração)

```bash
# Placa DekTec 0, porta 0 -> envia para um receptor SRT (caller)
asi2srt rodar -e dektec:0:0 -s "srt://203.0.113.10:9000?latency=300"

# Driver Linux ASI (DVEO) -> aguarda o receptor conectar (listener), com AES
asi2srt rodar -e linuxasi:/dev/asirx0 -s "srt://:9000?mode=listener&passphrase=minhasenha123"

# Para servidores com stream id (SRT Live Server, Haivision, etc.)
asi2srt rodar -e dektec:0:1 -s "srt://srt.exemplo.com:8080?streamid=publish/live/canal1"
```

| Entrada | Formato |
|---|---|
| DekTec (DTA-2145, DTA-2138, DTU-245…) | `dektec:PLACA:CANAL` |
| Driver Linux ASI (DVEO / Computer Modules) | `linuxasi:/dev/asirxN` |
| UDP / multicast | `udp:239.1.1.1:1234[@IP_DA_NIC]` |
| Arquivo .ts em loop (teste) | `arquivo:caminho.ts` |

Parâmetros da URL SRT: `mode` (caller/listener), `latency` (ms), `passphrase`,
`pbkeylen` (16/24/32), `streamid`, `oheadbw` (% de banda extra).

## Uso com configuração (vários canais)

```bash
asi2srt init                     # cria asi2srt.yaml comentado
nano asi2srt.yaml
asi2srt comando -c asi2srt.yaml  # mostra os comandos tsp sem executar
asi2srt rodar -c asi2srt.yaml
```

Cada item em `canais:` é uma porta ASI com seu próprio destino SRT.

## Serviço (inicia no boot)

```bash
sudo asi2srt instalar-servico      # cria /etc/asi2srt/config.yaml e a unidade systemd
sudo nano /etc/asi2srt/config.yaml
sudo systemctl enable --now asi2srt
journalctl -u asi2srt -f
```

## Monitoração

- Painel: `http://<ip>:8088/`
- JSON (Zabbix, Grafana, Prometheus via exporter): `http://<ip>:8088/status.json`

Para cada canal: estado, bitrate, erros de continuidade (CC), alarmes de taxa,
número de reinícios e uptime.

## Teste sem hardware

```bash
asi2srt teste
```

Gera um TS de teste com ffmpeg, publica como SRT listener na porta 9000 e abre o ffplay.

## Subcomandos

| Comando | Função |
|---|---|
| `rodar` | executa o gateway |
| `comando` | mostra os comandos tsp (dry-run) |
| `init` | gera configuração de exemplo |
| `verificar` | confere TSDuck, plugins, placas DekTec e dispositivos ASI |
| `instalar-tsduck` | baixa e instala o TSDuck |
| `instalar-servico` | instala como serviço systemd |
| `teste` | teste ponta a ponta sem placa |

## Dicas

- **Latência SRT:** 3–4× o RTT do link. Internet pública: 500–2000 ms. LAN/MPLS: 120–200 ms.
- **Pacotes nulos:** o ASI costuma vir completado com stuffing até a taxa nominal. Removê-los
  (padrão) economiza banda. Use `--manter-nulos` se o receptor exigir CBR.
- **Banda necessária:** `bitrate × (1 + oheadbw/100)`.
- **DekTec:** precisa do driver DekTec e do TSDuck com plugin `dektec`.
- **DVEO:** configure o driver para pacotes de 188 bytes.

## Gerar uma versão (binário)

```bash
git tag v1.0.0 && git push origin v1.0.0
```

O GitHub Actions compila o binário e o publica em Releases.

## Licença

MIT
